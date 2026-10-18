"""Run real GPU/compiler/evaluator checks under worker isolation before enabling jobs."""
import json
from pathlib import Path
import subprocess
import sys
import time
from broker import atomic

CODE = r'''
import importlib.util,json,os,pathlib,shutil,subprocess,torch
os.environ.update(HOME='/work/home',TORCH_EXTENSIONS_DIR='/work/extensions',CUDA_CACHE_PATH='/work/cuda-cache',TORCH_CUDA_ARCH_LIST='6.0',MAX_JOBS='2')
for p in ('/work/home','/work/output'):pathlib.Path(p).mkdir()
assert torch.cuda.is_available() and torch.cuda.device_count()==1
x=torch.arange(1024,device='cuda',dtype=torch.float32)
assert torch.equal(x+x,2*x)
assert torch.cuda.get_device_capability()==(6,0)
source=pathlib.Path('/work/check.cu')
source.write_text('#include <cuda_runtime.h>\n#include <stdio.h>\n__global__ void k(int*p){*p=7+5;}\nint main(){int*p; if(cudaMallocManaged(&p,4))return 1;k<<<1,1>>>(p);if(cudaDeviceSynchronize())return 2;int v=*p;cudaFree(p);printf("CUDA result=%d\\n",v);return v==12?0:3;}')
subprocess.run(['nvcc','-arch=sm_60',str(source),'-o','/work/check'],check=True)
subprocess.run(['/work/check'],check=True)
from torch.utils.cpp_extension import load_inline
module=load_inline(name='preflight_extension',cpp_sources='torch::Tensor identity(torch::Tensor x){return x;}',functions=['identity'],extra_cflags=['-O0'],verbose=False)
assert torch.equal(module.identity(x),x)
subprocess.run(['codex','--version'],check=True)
subprocess.run(['claude','--version'],check=True)
subprocess.run(['python','--version'],check=True)
subprocess.run(['python3','-m','orchestrator.optimize','--help'],cwd='/opt/atrex',stdout=subprocess.DEVNULL,check=True)
shutil.copytree('/sample','/work/operator')
spec=importlib.util.spec_from_file_location('worker','/opt/nexus-worker.py');worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
try:worker.measure_reference(pathlib.Path('/work/operator'))
except Exception:
 print(pathlib.Path('/work/output/reference_baseline.log').read_text()[-14000:])
 for p in pathlib.Path('/work/output/reference_baseline').rglob('eval_result.json'):print(p.read_text()[-24000:])
 raise
print('PREFLIGHT_RESULT '+json.dumps({'cuda_test_passed':True,'gpu_name':torch.cuda.get_device_name(),'compute_capability':'6.0','visible_gpu_count':torch.cuda.device_count(),'torch_version':torch.__version__,'cuda_version':torch.version.cuda,'compiler_test_passed':True,'reference_evaluator_passed':True}))
'''


def main():
    root=Path(__file__).resolve().parent
    image=sys.argv[1] if len(sys.argv)>1 else 'nexus-atrex-worker:20261018'
    image_id=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',image],text=True).strip()
    args=['docker','run','--rm','--name','nexus-atrex-preflight','--network','none','--read-only',
          '--user','1000:1000','--cap-drop','ALL','--security-opt','no-new-privileges',
          '--memory','12g','--cpus','4','--pids-limit','512','--shm-size','512m',
          '--tmpfs','/tmp:rw,exec,nosuid,nodev,size=1g,mode=1777',
          '--tmpfs','/work:rw,exec,nosuid,nodev,size=8g,uid=1000,gid=1000,mode=0700',
          '--mount',f'type=bind,src={root / "sample_vector_add"},dst=/sample,readonly',
          '--device','/dev/nvidia0','--device','/dev/nvidiactl','--device','/dev/nvidia-uvm',
          '--env','CUDA_VISIBLE_DEVICES=0']
    for library in ('libcuda.so.1','libnvidia-ml.so.1','libnvidia-ptxjitcompiler.so.1','libnvidia-gpucomp.so.580.178.04'):
        path=Path('/lib/x86_64-linux-gnu')/library
        if path.exists():args+=['--mount',f'type=bind,src={path.resolve()},dst=/driver/{library},readonly']
    args+=['--entrypoint','python3',image,'-u','-c',CODE]
    try:result=subprocess.run(args,capture_output=True,text=True,timeout=600)
    except subprocess.TimeoutExpired:
        subprocess.run(['docker','rm','-f','nexus-atrex-preflight'],capture_output=True)
        raise
    print(result.stdout,flush=True)
    if result.returncode:
        print(result.stderr,flush=True);raise SystemExit(result.returncode)
    record=json.loads(next(line.removeprefix('PREFLIGHT_RESULT ') for line in result.stdout.splitlines() if line.startswith('PREFLIGHT_RESULT ')))
    record.update(image_id=image_id,checked_at=time.time())
    atomic(root/'broker-state/gpu-preflight.json',record)
    print('GPU preflight recorded for '+image_id)


if __name__=='__main__':main()
