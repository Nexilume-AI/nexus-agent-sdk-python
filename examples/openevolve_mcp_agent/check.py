"""Deterministic native-engine and isolation gate, separate from real-model acceptance."""
import json,subprocess,tempfile,hashlib
from pathlib import Path
from service import Service,DEMO_INITIAL,DEMO_EVALUATOR,ROOT,DATA
from broker import Broker,IMAGE,docker_flags
def main():
 with tempfile.TemporaryDirectory(prefix='native-check-',dir=ROOT) as tmp:
  base=Path(tmp);work=base/'work';private=base/('private-'+ 'x'*80);work.mkdir();private.mkdir()
  (work/'initial.py').write_text(DEMO_INITIAL);(private/'evaluator.py').write_text(DEMO_EVALUATOR)
  cfg={'iterations':2,'seed':42,'evaluation_seconds':10,'timeout_seconds':180,'model':'deterministic-test-backend','goal':'Optimize solution to return (3,-2).'}
  (work/'request.json').write_text(json.dumps(cfg));job={'id':'check','folder':work,'config':cfg}
  b=Broker(job,private,stub=True)
  try:
   cmd=docker_flags('nexus-oe-check','4g')+['--mount',f'type=bind,src={work},dst=/work','--mount',f'type=bind,src={private/"ipc"},dst=/broker,readonly',IMAGE]
   p=subprocess.run(cmd,capture_output=True,timeout=180)
   if p.returncode:print(p.stdout.decode(errors='replace')[-7000:]);print(p.stderr.decode(errors='replace')[-7000:]);raise RuntimeError('NATIVE_ENGINE_FAILED')
   report=json.loads((work/'report.json').read_text());assert report['changed_from_initial'] and report['best_metrics']['combined_score']==1 and b.model_count>=1
   # Caller code cannot see controller work, broker socket, secrets or network.
   (private/'evaluator.py').write_text('''import os,socket\ndef evaluate(program_path):\n assert not os.path.exists("/broker/run.sock")\n assert not os.path.exists("/work/request.json")\n assert not any(k in os.environ for k in ("OPENAI_API_KEY","ANTHROPIC_API_KEY","NEXUS_API_KEY","NEXUS_CONTEXT_TOKEN"))\n s=socket.socket();s.settimeout(.2)\n try:s.connect(("1.1.1.1",443))\n except OSError:pass\n else:raise AssertionError("network reachable")\n return {"combined_score":1.0}\n''')
   assert b.evaluate({'code':'pass'})['metrics']['combined_score']==1
   (private/'evaluator.py').write_text('def evaluate(program_path):\n while True:pass\n')
   job['config']['evaluation_seconds']=1
   try:b.evaluate({'code':'pass'});raise AssertionError('timeout failed')
   except ValueError as e:assert str(e)=='EVALUATION_CANCELLED_OR_LIMIT'
  finally:b.close()
  s=Service();i={'version':1,'principal_type':'api_key','owner_scope':'a'*64,'quota_scope':'b'*64}
  try:
   w=s.call('create_openevolve_workspace',{},i)['structuredContent']
   assert not s.call('load_demo_project',w,i)['isError']
   assert s.call('validate_project',w,{**i,'owner_scope':'c'*64})['isError']
   assert s.call('put_project_file',{**w,'name':'../escape','content':'x'},i)['isError']
   assert s.call('validate_project',{**w,'workspace_key':'wrong'},i)['isError']
   assert not s.call('close_openevolve_workspace',w,i)['isError']
  finally:s.close()
  proof={'native_report':report,'checks':['native OpenEvolve with deterministic backend','separate evaluator no credentials/socket/network','evaluator timeout','caller and capability isolation','upload path boundary'],'real_model_test':False}
  (ROOT/'native-evidence.json').write_text(json.dumps(proof,indent=2));(ROOT/'runtime-ready.json').write_text(json.dumps({'native_engine_tested':True,'real_model_tested':False}))
  print('NATIVE_ENGINE_AND_ISOLATION_PASSED')
if __name__=='__main__':main()
