"""Native AVA and tool invocations run only inside a resource-limited container."""
import asyncio,base64,contextlib,dataclasses,hashlib,http.client,json,os,re,socket,subprocess,sys,time,types
from pathlib import Path
ROOT=Path('/work');os.chdir(ROOT)
request=json.loads(sys.stdin.buffer.read(12*1024*1024))
inputs=ROOT/'inputs';inputs.mkdir()
for name,content in request['files'].items():
    p=inputs/name
    if not p.resolve().is_relative_to(inputs) or not isinstance(content,str):raise ValueError('INVALID_INPUT')
    p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content)
output=ROOT/'output';output.mkdir()
cfg=request['request'];mode=cfg['mode'];start=time.monotonic()
model_calls=0

class Conn(http.client.HTTPConnection):
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(200);self.sock.connect('/broker/run.sock')

def chat(**kw):
    global model_calls
    conn=Conn('localhost',timeout=200)
    try:
        messages=list(kw['messages'])
        if cfg.get('instructions'):
            messages.append({'role':'user','content':'Additional analysis context (retain the requested JSON schema): '+cfg['instructions']})
        conn.request('POST','/model',json.dumps({'messages':messages}).encode(),{'Content-Type':'application/json'})
        res=conn.getresponse();raw=res.read(262145)
        if res.status!=200 or len(raw)>262144:raise RuntimeError('AUTHORIZED_MODEL_FAILED')
        text=json.loads(raw)['text'];model_calls+=1
        (output/'model-response.txt').write_text(text)
        return {'message':{'content':text}}
    finally:conn.close()

def run(cmd,name,timeout=180):
    with (output/(name+'.log')).open('w') as log:
        try:r=subprocess.run(cmd,stdout=log,stderr=log,timeout=timeout);return {'exit_code':r.returncode,'log':name+'.log'}
        except subprocess.TimeoutExpired:return {'exit_code':None,'timed_out':True,'log':name+'.log'}

with contextlib.redirect_stdout(sys.stderr):
    report={'upstream_commit':'d247b29c8399fa51828b184652b0b34c41d5d888','mode':mode,
        'limitation':'Completion is not chip sign-off. Simulation checks only the supplied testbench; matching logs do not prove a complete design.'}
    if mode in ('pipeline','model_pipeline'):
        if mode=='model_pipeline':sys.modules['ollama']=types.SimpleNamespace(chat=chat)
        import ava_patched as native
        ava=native.AVA(enable_llm=mode=='model_pipeline',model_name=cfg.get('model') or 'authorized',
            timeout=90,target_coverage=50,run_base_dir=str(output/'campaign'),enable_database=False,
            report_formats=['json','csv','html'],rtl_sources=[str(inputs/cfg['rtl_path'])])
        rule_used=[]
        original_rule=ava._rule_based_semantic_analysis
        def tracked_rule(text):
            rule_used.append(True)
            return original_rule(text)
        ava._rule_based_semantic_analysis=tracked_rule
        value=asyncio.run(ava.generate_suite(rtl_spec=(inputs/cfg['rtl_path']).read_text(),microarch='in_order',seed=cfg['seed'],save_results=True))
        (output/'native-report.json').write_text(json.dumps(value,default=str,indent=2))
        report.update(native_status=value.get('status'),verification_status='partial_unless_real_tandem_evidence',
            semantic_map=value.get('semantic_map'),model_requests=model_calls,
            semantic_source='rule_based' if rule_used else 'authorized_model',
            gaps=['Native generated testbench is not automatically built into a DUT simulation binary. Missing RTL/ISS traces remain no-data; no full RISC-V compliance claim.'])
        if mode=='model_pipeline' and model_calls<1:raise RuntimeError('MODEL_DID_NOT_RUN')
    elif mode=='compare':
        from AGENT_D.compare_commitlogs import compare_logs
        value=compare_logs(str(inputs/cfg['rtl_log']),str(inputs/cfg['iss_log']),seed=cfg['seed'],stop_on_first=False)
        (output/'comparison.json').write_text(json.dumps(value.to_bug_report(),indent=2))
        (output/'comparison.md').write_text(value.to_markdown())
        (output/'comparison.xml').write_text(value.to_junit_xml())
        report.update(verification_status='matched_supplied_logs' if value.passed else 'mismatch',passed=value.passed,stats=value.stats.to_dict())
    elif mode=='generate':
        value=run([sys.executable,'/opt/ava/AGENT_G/generate_tests.py','--outdir',str(output/'tests'),'--seed',str(cfg['seed']),'--random','2','--directed','2','--length','20','--verify-spike'],'generation',240)
        manifests=list((output/'tests').rglob('*manifest*.json'))
        manifest=json.loads((output/'tests/manifest.json').read_text()) if (output/'tests/manifest.json').exists() else {}
        tests=manifest.get('directed',[])+manifest.get('random',[])
        actual=[{'name':t.get('name'),'elf_exists':bool(t.get('elf') and Path(t['elf']).is_file()),'spike_exit':t.get('spike')} for t in tests]
        passed=value['exit_code']==0 and len(actual)==4 and all(t['elf_exists'] and t['spike_exit']==0 for t in actual)
        report.update(verification_status='generated_tests_passed_spike' if passed else 'incomplete_or_failed_tests',passed=passed,
            generation=value,tests=actual,manifests=[str(p.relative_to(output)) for p in manifests],elf_count=len(list(output.rglob('*.elf'))),harness_isa='rv32im_zicsr')
    else:
        from AGENT_H.rtl_graph import RTLGraphAnalyzer
        graph=RTLGraphAnalyzer.from_dir(str(inputs)).run()
        (output/'rtl-graph.json').write_text(json.dumps(graph,default=str,indent=2))
        files=[str(p) for p in sorted(inputs.rglob('*')) if p.suffix in ('.sv','.v') and p.name!='tb.sv']
        if mode=='rtl_check':
            lint=run(['verilator','--lint-only','--timing','-Wno-fatal','--top-module',cfg['top'],*files],'verilator')
            # File names are validated before interpolation into the Yosys command.
            ys='read_verilog -sv '+' '.join(files)+'; hierarchy -check -top '+cfg['top']+'; proc; opt; check -assert; stat; write_json '+str(output/'netlist.json')
            synth=run(['yosys','-p',ys],'yosys')
            report.update(verification_status='structural_checks_only',verilator=lint,yosys=synth,passed=lint['exit_code']==0 and synth['exit_code']==0)
        elif mode=='simulate':
            compile=run(['verilator','--binary','--timing','-Wno-fatal','--top-module','tb','--Mdir','/tmp/obj','-j','2',*files,str(inputs/'tb.sv')],'compile')
            sim=run(['/tmp/obj/Vtb'],'simulation',30) if compile['exit_code']==0 else {'exit_code':None,'skipped':True}
            report.update(verification_status='supplied_testbench_only',compile=compile,simulation=sim,passed=sim['exit_code']==0)
        else:raise ValueError('INVALID_MODE')
    report['seconds']=round(time.monotonic()-start,3)
    (output/'report.json').write_text(json.dumps(report,indent=2))
artifacts={};size=0
for path in sorted(output.rglob('*')):
    if not path.is_file() or path.is_symlink():continue
    size+=path.stat().st_size
    if size>16*1024*1024:raise RuntimeError('ARTIFACT_LIMIT')
    artifacts[path.relative_to(output).as_posix()]=base64.b64encode(path.read_bytes()).decode()
print(json.dumps({'artifacts':artifacts}))
