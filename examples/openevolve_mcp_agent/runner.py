"""Native pinned OpenEvolve controller; never imports caller or generated code."""
import asyncio,json,hashlib,time
from pathlib import Path
from openevolve import OpenEvolve
from openevolve.config import Config,LLMConfig,LLMModelConfig
from ipc_client import init_client,request
async def main():
 cfg=json.loads(Path('/work/request.json').read_text());started=time.monotonic()
 c=Config(max_iterations=cfg['iterations'],checkpoint_interval=1,random_seed=cfg['seed'],language='python',diff_based_evolution=False,max_code_length=65536)
 c.llm=LLMConfig(models=[LLMModelConfig(name=cfg['model'],init_client=init_client)],max_tokens=4096,timeout=180,retries=0,temperature=None)
 c.evaluator.cascade_evaluation=False;c.evaluator.max_retries=0;c.evaluator.timeout=150;c.evaluator.parallel_evaluations=1;c.evaluator.use_llm_feedback=False
 c.database.population_size=50;c.database.archive_size=20;c.database.num_islands=2
 c.prompt.system_message='You optimize Python programs. Preserve the interface. Return a complete Python implementation in a python code block. Use evaluation feedback. '+cfg['goal']
 c.evolution_trace.enabled=True;c.evolution_trace.include_code=True;c.evolution_trace.include_prompts=False;c.evolution_trace.output_path='/work/output/evolution.jsonl'
 engine=OpenEvolve('/work/initial.py','/opt/nexus/evaluator_bridge.py',c,'/work/output')
 best=await engine.run(iterations=cfg['iterations'])
 if best is None:raise RuntimeError('No valid program produced')
 Path('/work/best_program.py').write_text(best.code)
 metrics=request('/evaluate',{'code':best.code})['metrics']
 programs=list(engine.database.programs.values())
 report={'upstream_commit':'04a37f191b2eb367daef455057a0470c0ac853d8','engine':'native OpenEvolve','iterations_requested':cfg['iterations'],'programs_evaluated':len(programs),'best_metrics':metrics,'best_sha256':hashlib.sha256(best.code.encode()).hexdigest(),'changed_from_initial':best.code!=Path('/work/initial.py').read_text(),'wall_seconds':round(time.monotonic()-started,2),'quality_certified':False}
 Path('/work/report.json').write_text(json.dumps(report,indent=2));print('EVOLUTION_COMPLETE',flush=True)
if __name__=='__main__':asyncio.run(main())
