"""Trusted native KernelFoundry controller. User code runs only via GPU broker."""
import ast,asyncio,json,hashlib,os
from pathlib import Path
from ipc import request

def static_tests(root):
 tree=ast.parse((Path(root)/'task.py').read_text());all_tests=[];perf=[]
 for cls in tree.body:
  if not isinstance(cls,ast.ClassDef):continue
  for f in cls.body:
   if isinstance(f,(ast.FunctionDef,ast.AsyncFunctionDef)) and f.name.startswith('test_'):
    name=cls.name+'::'+f.name;all_tests.append(name)
    if any('performance' in ast.unparse(d) for d in f.decorator_list):perf.append(name)
 return {'all_tests':all_tests,'correctness_tests':[n for n in all_tests if n not in perf],'profile_tests':perf}

def main():
 Path('/work/logs').mkdir(exist_ok=True);Path('/work/results').mkdir(exist_ok=True)
 from kernelfoundry.eval_pipeline.utils import custom_task_helper
 custom_task_helper.get_test_names=static_tests
 from kernelfoundry.algorithm.evaluator import Evaluator
 from kernelfoundry.algorithm.schemas import EvalResult
 from kernelfoundry.eval_pipeline.task import TestResult
 from kernelfoundry.algorithm.inference_server import InferenceServer
 def remote_eval(self,task):
  # Never return evaluator artifacts/executable modules to this controller.
  answer=request('/evaluate',{'task':task.encode()})
  task.test_result_reference=TestResult.decode(answer.get('reference'))
  return EvalResult(**answer['result']),task
 Evaluator.run=remote_eval
 def init(self,model_name='gpt-6-sol',**kwargs):self.model=model_name
 def infer(self,messages,**kwargs):
  answer=request('/model',{'messages':messages})
  usage=answer.get('usage',{})
  return [answer['text']],{'model':self.model,'input_tokens':usage.get('prompt_tokens',0),'output_tokens':usage.get('completion_tokens',0),'truncated_completions':[]}
 InferenceServer.__init__=init;InferenceServer.__call__=infer
 from kernelfoundry.mcp_server.server import _run_locally
 class Context:
  async def report_progress(self,**value):print('PROGRESS',json.dumps(value),flush=True)
 cfg=json.loads(Path('/work/request.json').read_text())
 result=asyncio.run(_run_locally(Path('/work/task'),Context(),run_optimization=cfg['mode']=='optimize'))
 # Upstream success=True means orchestration completed, not correctness passed.
 import kernelfoundry.eval_pipeline.database as db
 from sqlalchemy import select
 with db.SessionRO() as session:
  kernels=session.execute(select(db.Kernel).where(db.Kernel.job_id==result['job_id'])).scalars().all()
  rows=[{'id':k.id,'compiled':k.status in ('compiled','correct'),'correctness':k.status=='correct','score':k.score,'runtime':k.runtime,'runtime_stats':k.runtime_stats,'speedup':k.improve_over_native,'code_sha256':hashlib.sha256((k.output_code or '').encode()).hexdigest()} for k in kernels]
 result.update(engine='native KernelFoundry',mode=cfg['mode'],kernels=rows,hardware='Pascal SM60',profiler_enabled=False)
 Path('/work/report.json').write_text(json.dumps(result,indent=2,default=str))
 if not result.get('success') or not any(k['correctness'] for k in rows):raise RuntimeError('NO_CORRECT_KERNEL')
 print('KERNELFOUNDRY_COMPLETE',flush=True)
if __name__=='__main__':main()
