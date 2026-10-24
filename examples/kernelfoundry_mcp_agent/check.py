import json,subprocess,tempfile
from pathlib import Path
from broker import Broker,IMAGE,docker_flags
from demo import write_task
ROOT=Path(__file__).resolve().parent
def main():
 with tempfile.TemporaryDirectory(prefix='native-',dir=ROOT) as tmp:
  folder=Path(tmp);work=folder/'work';private=folder/'private';work.mkdir();private.mkdir()
  cfg={'mode':'validate','iterations':0,'evaluation_seconds':600,'timeout_seconds':900}
  (work/'request.json').write_text(json.dumps(cfg));write_task(work/'task',cfg)
  task=work/'task/task.py'
  task.write_text('import os,socket\nassert not os.path.exists("/broker/run.sock")\nassert not os.path.exists("/work/request.json")\nassert not any(k in os.environ for k in ("OPENAI_API_KEY","NEXUS_API_KEY","NEXUS_CONTEXT_TOKEN"))\ns=socket.socket();s.settimeout(.2)\ntry:s.connect(("1.1.1.1",443))\nexcept OSError:pass\nelse:raise AssertionError("network reachable")\ns.close()\n'+task.read_text())
  job={'id':'check','folder':work,'config':cfg};broker=Broker(job,private)
  try:
   cmd=docker_flags('nexus-kf-check','4g')+['--mount',f'type=bind,src={work},dst=/work','--mount',f'type=bind,src={private/"ipc"},dst=/broker,readonly',IMAGE]
   with (ROOT/'native-check.log').open('wb') as log:
    result=subprocess.run(cmd,stdout=log,stderr=log,timeout=900)
   if result.returncode:
    diagnostic=[]
    for log in (work/'logs').rglob('*'):
     if log.is_file():diagnostic.append(log.read_text(errors='replace')[-24000:])
    for log in private.glob('evaluations/*/run.log'):diagnostic.append(log.read_text(errors='replace')[-24000:])
    if (work/'report.json').exists():diagnostic.append((work/'report.json').read_text())
    (ROOT/'native-failure.txt').write_text('\n'.join(diagnostic))
    for log in (work/'logs').rglob('*.log'):print(log.read_text()[-14000:])
    if (work/'report.json').exists():print((work/'report.json').read_text())
    print((ROOT/'native-check.log').read_text()[-12000:])
    for p in private.glob('evaluations/*/run.log'):print(p.read_text()[-12000:])
    raise RuntimeError('NATIVE_VALIDATION_FAILED')
   report=json.loads((work/'report.json').read_text());assert report['success'] and report['kernels'][0]['correctness']
   assert broker.model_count==0 and broker.eval_count==1
   (ROOT/'native-evidence.json').write_text(json.dumps({'report':report,'model_calls':0,'gpu':'Pascal SM60','real_model_tested':False},indent=2))
   (ROOT/'runtime-ready.json').write_text(json.dumps({'native_cuda_validation':True,'real_model_tested':False}))
   print('NATIVE_CUDA_VALIDATION_PASSED')
  finally:broker.close()
if __name__=='__main__':main()
