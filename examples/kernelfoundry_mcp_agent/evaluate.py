"""Untrusted task execution inside an offline GPU container, with no broker mount."""
import json,logging
from pathlib import Path
def main():
 from omegaconf import OmegaConf
 from kernelfoundry.eval_pipeline.task import Task
 from kernelfoundry.eval_pipeline.tasks.task_runner import TaskRunner
 from kernelfoundry.algorithm.evaluator import Evaluator
 from kernelfoundry.algorithm.problem_logger import ProblemLogger
 from kernelfoundry.algorithm.controller import setup_logging
 task=Task.decode(json.loads(Path('/input/request.json').read_text())['task'])
 cfg=OmegaConf.create(task.config);Path('/tmp/logs').mkdir(exist_ok=True)
 setup_logging('/tmp/logs');TaskRunner.init(use_queue=False,gpu_arch='Pascal')
 evaluator=Evaluator(cfg,ProblemLogger(0,'evaluation','/tmp/logs',0),0)
 result,task=evaluator.run(task)
 from dataclasses import asdict
 out={'result':result.model_dump(),'reference':asdict(task.test_result_reference) if task.test_result_reference else None}
 Path('/output/response.json').write_text(json.dumps(out,default=str))
if __name__=='__main__':main()
