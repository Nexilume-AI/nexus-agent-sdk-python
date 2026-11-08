import base64,hashlib,json,time,uuid
from pathlib import Path
from service import Service,ROOT,CATALOG
identity={'version':1,'principal_type':'api_key','owner_scope':'a'*64,'quota_scope':'b'*64}
other={**identity,'owner_scope':'c'*64,'quota_scope':'d'*64}
service=Service();auth={};proof={}
def call(name,a=None,who=identity,error=None):
    r=service.call(name,{**auth,**(a or {})},who);v=r.get('structuredContent')
    if error:assert r['isError'] and v['error']==error,(name,v)
    else:assert not r['isError'],(name,v)
    return v
def job(mode,**a):
    rid=str(uuid.uuid4());j=call('start_verisure_job',{'mode':mode,'request_id':rid,**a})
    assert call('start_verisure_job',{'mode':mode,'request_id':rid,**a})['job_id']==j['job_id']
    deadline=time.monotonic()+650
    while time.monotonic()<deadline:
        v=call('job_status',{'job_id':j['job_id']})
        if v['state'] not in ('queued','running'):break
        time.sleep(1)
    assert v['state']=='succeeded',v
    proof[mode+('_bad' if a.get('rtl_path') else '')]=v['report.json'];print('PASS',mode,json.dumps(v['report.json']),flush=True)
    return j,v['report.json']
try:
    assert len(CATALOG)==15
    auth=call('create_verisure_workspace');auth={k:auth[k] for k in ('workspace_id','workspace_key')}
    call('load_demo_project');call('load_demo_project',who=other,error='WORKSPACE_NOT_FOUND')
    call('put_project_file',{'path':'../escape.sv','content':'x'},error='INVALID_PATH')
    call('put_project_file',{'path':'chunk.txt','content':'abc'})
    call('put_project_file',{'path':'chunk.txt','content':'abc'})
    call('put_project_file',{'path':'chunk.txt','offset':3,'content':'def'})
    call('put_project_file',{'path':'chunk.txt','offset':0,'content':'bad'},error='CHUNK_CONFLICT')
    assert call('list_benchmarks',{'limit':5})['total']==209
    _,r=job('contract_lint');assert r['passed']
    _,r=job('simulate');assert r['passed'] and r['mismatches']==0
    _,r=job('simulate',rtl_path='bad_rtl.sv');assert not r['passed'] and r['mismatches']>0
    proof['passed']=True
finally:
    if not proof.get('passed'):
        proof['diagnostics']=[{'path':str(p.relative_to(ROOT)), 'tail':p.read_text(errors='replace')[-7000:]} for p in (ROOT/'data').glob('*/jobs/*/work/worker-error.log')]
    if auth:
        try:call('close_verisure_workspace')
        except Exception:pass
    service.close();(ROOT/'local-evidence.json').write_text(json.dumps(proof,indent=2))
