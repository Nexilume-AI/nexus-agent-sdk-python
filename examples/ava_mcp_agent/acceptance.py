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
    rid=str(uuid.uuid4());j=call('start_verification_job',{'mode':mode,'request_id':rid,**a})
    assert call('start_verification_job',{'mode':mode,'request_id':rid,**a})['job_id']==j['job_id']
    deadline=time.monotonic()+650
    while time.monotonic()<deadline:
        v=call('job_status',{'job_id':j['job_id']})
        if v['state'] not in ('queued','running'):break
        time.sleep(1)
    assert v['state']=='succeeded',v
    proof[mode+('_bad' if a.get('rtl_log') else '')]=v['report.json'];print('PASS',mode,json.dumps(v['report.json']),flush=True)
    return j,v['report.json']
try:
    assert len(CATALOG)==14
    auth=call('create_ava_workspace');auth={k:auth[k] for k in ('workspace_id','workspace_key')}
    call('load_demo_project');call('load_demo_project',who=other,error='WORKSPACE_NOT_FOUND')
    call('put_project_file',{'path':'../escape.sv','content':'x'},error='INVALID_PATH')
    call('put_project_file',{'path':'chunk.txt','content':'abc'})
    call('put_project_file',{'path':'chunk.txt','content':'abc'})
    call('put_project_file',{'path':'chunk.txt','offset':3,'content':'def'})
    call('put_project_file',{'path':'chunk.txt','offset':0,'content':'bad'},error='CHUNK_CONFLICT')
    _,r=job('compare');assert r['passed'] and r['stats']['total_steps']>0
    _,r=job('compare',rtl_log='bad.jsonl');assert not r['passed'] and r['stats']['total_mismatches']>0
    _,r=job('rtl_check');assert r['passed']
    j,r=job('simulate');assert r['passed']
    chunk=call('read_artifact',{'job_id':j['job_id'],'path':'simulation.log'});data=base64.b64decode(chunk['data_base64']);assert b'AVA_DEMO_SIM_PASS' in data and hashlib.sha256(data).hexdigest()==chunk['sha256']
    _,r=job('generate');assert r['passed'] and r['elf_count']==4
    _,r=job('pipeline');assert r['native_status'] in ('completed','incomplete','failed')
    proof['passed']=True
finally:
    if not proof.get('passed'):
        proof['diagnostics']=[{'path':str(p.relative_to(ROOT)), 'tail':p.read_text(errors='replace')[-7000:]} for p in (ROOT/'data').glob('*/jobs/*/work/worker-error.log')]
    if auth:
        try:call('close_ava_workspace')
        except Exception:pass
    service.close();(ROOT/'local-evidence.json').write_text(json.dumps(proof,indent=2))
