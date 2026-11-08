"""Execute pinned Veri-Sure inside a non-root networkless container."""
import asyncio,base64,contextlib,json,os,shutil,sys,time
from dataclasses import asdict
from pathlib import Path
COMMIT='aab5bed60f04157a4be8581b53f9e913287ba328'
root=Path('/work');inp=root/'inputs';out=root/'result';inp.mkdir();out.mkdir();os.chdir(out)
value=json.load(sys.stdin);cfg=value['request']
for name,text in value['files'].items():
 p=inp/name
 if not p.resolve().is_relative_to(inp):raise ValueError('INVALID_PATH')
 p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding='utf-8')
mode=cfg['mode'];started=time.monotonic()
report={'upstream_commit':COMMIT,'mode':mode,'limitation':'Simulation checks supplied tests. Formal evidence covers only selected outputs against the supplied or model-generated reference; no silicon sign-off.'}
def write(name,v):(out/name).write_text(json.dumps(v,indent=2,default=str),encoding='utf-8')
def setup_model():
 import httpx
 import eda_agent.model as m
 def make(c):
  return m._OpenAIChatModel(model_name=c.model,api_key='scoped-local-broker',stream=False,
    client_args={'base_url':'http://model/v1','max_retries':0,'http_client':httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds='/broker/run.sock'),timeout=190)},
    generate_kwargs={'max_completion_tokens':4096})
 m._make_openai_model=make
with (out/'native.log').open('w',encoding='utf-8') as log,contextlib.redirect_stdout(log):
 from eda_agent.config import LLMConfig
 native_cfg=LLMConfig(provider='openai',model=cfg.get('model') or 'not-used',api_key='scoped-local-broker',stream=False)
 rtl=inp/cfg.get('rtl_path','rtl.sv')
 if mode in ('generate','benchmark'):
  setup_model()
  from eda_agent.top_agent import TopAgent,TopAgentConfig
  if mode=='benchmark':
   bench=Path('/opt/verisure/benchmarks/verilogeval-v2-ext/dataset_spec-to-rtl');problem=cfg['benchmark']
   if '/' in problem or '..' in problem or not (bench/(problem+'_prompt.txt')).is_file():raise ValueError('INVALID_BENCHMARK')
   spec=(bench/(problem+'_prompt.txt')).read_text();tb=bench/(problem+'_test.sv');ref=bench/(problem+'_ref.sv')
  else:
   spec=(inp/'spec.txt').read_text();tb=inp/'golden_tb.sv';ref=inp/'golden_ref.sv'
  (out/'spec.txt').write_text(spec)
  agent=TopAgent(native_cfg,config=TopAgentConfig(sim_max_retry=2,debug_max_trials=3))
  result=asyncio.run(agent.run(spec=spec,output_dir_per_run=out,golden_tb_path=str(tb) if tb.exists() else None,golden_rtl_blackbox_path=str(ref) if ref.exists() else None))
  write('native-result.json',asdict(result));report.update(passed=result.is_sim_pass and not result.error,error=result.error,input_tokens=result.input_tokens,output_tokens=result.output_tokens,verification_status='native_generation_and_testbench')
  # Independently re-run the generated RTL against the supplied golden benchmark.
  if tb.exists() and ref.exists() and (out/'rtl.sv').exists():
   from eda_agent.verilator_judge import run_verilator_testbench_sync
   check=run_verilator_testbench_sync(run_dir=out/'golden-check',top='tb',verilog_files=[out/'rtl.sv',tb,ref],timeout_s=20)
   write('golden-result.json',asdict(check));report['golden_passed']=check.passed;report['passed']=report['passed'] and check.passed
 elif mode=='simulate':
  from eda_agent.verilator_judge import run_verilator_testbench_sync
  from eda_agent.trace_report import build_trace_report
  from eda_agent.bash_tools import CommandResult
  shutil.copyfile(rtl,out/'rtl.sv');shutil.copyfile(inp/'tb.sv',out/'tb.sv')
  result=run_verilator_testbench_sync(run_dir=out,top=cfg.get('top','tb'),verilog_files=[out/'rtl.sv',out/'tb.sv'],timeout_s=20)
  write('simulation-result.json',asdict(result));(out/'simulation.log').write_text(result.log)
  trace,blocks=build_trace_report(rtl_path=out/'rtl.sv',sim_log_json=CommandResult(stdout=result.log,stderr='').model_dump_json(),output_dir=out)
  write('trace_report.json',trace);write('slices.json',[asdict(x) for x in blocks])
  report.update(passed=result.passed,mismatches=result.mismatches,compile_returncode=result.compile_returncode,run_returncode=result.run_returncode,verification_status='supplied_testbench',slices=len(blocks))
 elif mode=='formal':
  from eda_agent.boolean_proofer import BooleanProofer
  result=asyncio.run(BooleanProofer(native_cfg,sby_timeout_s=120).prove(contract_json=(inp/'contract.json').read_text(),rtl_path=str(rtl),output_dir=str(out),spec_body_override=(inp/'spec_body.sv').read_text()))
  write('formal-result.json',asdict(result));report.update(passed=result.status=='pass',formal_status=result.status,asserted_outputs=result.asserted_outputs,error=result.error,verification_status='supplied_reference_equivalence')
 elif mode=='contract_lint':
  from eda_agent.contract_linter import lint_contract_json,render_contract_issues
  issues,_=lint_contract_json((inp/'contract.json').read_text());write('contract-issues.json',[asdict(i) for i in issues]);(out/'contract_lint.txt').write_text(render_contract_issues(issues))
  report.update(passed=not any(i.severity=='error' for i in issues),errors=sum(i.severity=='error' for i in issues),warnings=sum(i.severity=='warning' for i in issues))
 elif mode=='trace':
  from eda_agent.trace_report import build_trace_report
  from eda_agent.bash_tools import CommandResult
  shutil.copyfile(rtl,out/'rtl.sv')
  for n in ('tb.sv','wave.vcd','contract.json'):
   if (inp/n).exists():shutil.copyfile(inp/n,out/n)
  trace,blocks=build_trace_report(rtl_path=out/'rtl.sv',sim_log_json=CommandResult(stdout=(inp/'simulation.log').read_text(),stderr='').model_dump_json(),output_dir=out)
  write('trace_report.json',trace);write('slices.json',[asdict(x) for x in blocks]);report.update(slices=len(blocks),verification_status='diagnostic_only')
 else:raise ValueError('INVALID_MODE')
report['seconds']=round(time.monotonic()-started,3);write('report.json',report)
artifacts={};total=0;omitted=[]
for p in sorted(out.rglob('*')):
 if not p.is_file() or p.is_symlink() or not p.resolve().is_relative_to(out):continue
 relative=p.relative_to(out);name=relative.as_posix()
 if 'obj_dir' in relative.parts or p.name in ('sim.bin','core'):continue
 size=p.stat().st_size
 if size>8388608 or total+size>15*1024*1024:omitted.append({'path':name,'bytes':size});continue
 raw=p.read_bytes();total+=len(raw);artifacts[name]=base64.b64encode(raw).decode()
artifacts['export-manifest.json']=base64.b64encode(json.dumps({'bytes':total,'omitted':omitted,'limit':16*1024*1024}).encode()).decode()
print(json.dumps({'artifacts':artifacts}))
