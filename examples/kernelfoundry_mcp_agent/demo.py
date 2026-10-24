"""Small genuine CUDA task with correctness and GPU-event benchmarks."""
TASK='''import pytest,torch
from pathlib import Path
from kernelfoundry import TestBase
# [REFERENCE_START]
def reference_kernel(x):
    return torch.relu(x)
# [REFERENCE_END]
@pytest.fixture(scope="session")
def device():return torch.device("cuda")
@pytest.fixture(scope="session")
def kernel(use_reference):
    if use_reference:return reference_kernel
    import kf_kernel
    return kf_kernel.forward
class TestReLU(TestBase):
    def build(self,gpu_arch):
        return self.compile_torch_extension(extension_name="kf_kernel",src="kernel.cu",output_dir=Path(__file__).parent,gpu_arch="6.0",timeout=180,backend="torch")
    @pytest.mark.parametrize("n",[1,257,65536])
    def test_correctness(self,n,kernel,device):
        torch.manual_seed(12);x=torch.randn(n,device=device)
        assert torch.equal(kernel(x),reference_kernel(x))
    @pytest.mark.performance
    @pytest.mark.parametrize("n",[65536,1048576])
    def test_benchmark(self,n,kernel,device,torch_profile):
        x=torch.randn(n,device=device);torch_profile(kernel,device,args=(x,))
'''
KERNEL='''#include <torch/extension.h>
#include <ATen/cuda/CUDAContext.h>
// [EVOLVE_START]
__global__ void relu_kernel(const float* x,float* y,int64_t n){
    int64_t i=blockIdx.x*blockDim.x+threadIdx.x;
    if(i<n)y[i]=fmaxf(x[i],0.0f);
}
torch::Tensor forward(torch::Tensor x){
    TORCH_CHECK(x.is_cuda() && x.is_contiguous() && x.scalar_type()==torch::kFloat32);
    auto y=torch::empty_like(x);int64_t n=x.numel();
    if(n)relu_kernel<<<(n+127)/128,128,0,at::cuda::getCurrentCUDAStream()>>>(x.data_ptr<float>(),y.data_ptr<float>(),n);
    return y;
}
// [EVOLVE_END]
PYBIND11_MODULE(TORCH_EXTENSION_NAME,m){m.def("forward",&forward);}
'''
def write_task(folder,cfg,initial=None):
 import json
 folder.mkdir(parents=True,exist_ok=True)
 if initial:
  for n in ('task.py','kernel.cu'):(folder/n).write_bytes((initial/n).read_bytes())
 else:
  (folder/'task.py').write_text(TASK);(folder/'kernel.cu').write_text(KERNEL)
 (folder/'conftest.py').write_text('from kernelfoundry.conftest import *\n')
 (folder/'instructions.txt').write_text('[USER_INSTRUCTIONS_START]\n'+cfg.get('goal','Optimize this CUDA kernel while preserving correctness.')+'\n[USER_INSTRUCTIONS_END]\n')
 config={'task_name':'custom_cuda','job_name':'nexus_job','task_origin':'custom','gpu_arch':'Pascal','language':'CUDA','use_queue':False,'use_container':False,'has_reference_build_step':False,'max_iters':cfg['iterations'],'branches_per_iteration':1,'build_timeout':180,'test_timeout':120,'test_output_inactivity_timeout':120,'build_output_inactivity_timeout':180,'inference':{'servers':[{'_target_':'kernelfoundry.algorithm.inference_server.InferenceServer','server_type':'openai','model_name':cfg.get('model','gpt-6-sol'),'max_tokens':4096,'num_completions':1,'verbose':False}]},'eval_config':{'profile_custom_model':False,'profile_original_model':False,'num_perf_trials':10,'warmup_min_iters':3,'warmup_min_time':0.01,'inner_loop_min_time':0.002},'paths':{'kernels_db_path':'sqlite:////work/kernels.sqlite3'},'logdir':'/work/logs','results_dir':'/work/results','verbose':False}
 (folder/'config.yaml').write_text(json.dumps(config))
