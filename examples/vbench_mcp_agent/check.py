"""Real upstream GPU metric acceptance plus caller, path and job boundaries."""
import base64,hashlib,json,time
from pathlib import Path
from service import Service,DIMENSIONS
owner={'version':1,'principal_type':'api_key','owner_scope':'a'*64,'quota_scope':'a'*64}
other={**owner,'owner_scope':'b'*64,'quota_scope':'b'*64}
s=Service()
def call(n,a=None,identity=owner,error=False):
 r=s.call(n,a or {},identity)
 assert bool(r.get('isError'))==error,(n,r)
 return r['structuredContent']
def wait(j,auth):
 for _ in range(1200):
  r=call('job_status',{**auth,'job_id':j['job_id']})
  if r['state'] not in ('running','queued'):return r
  time.sleep(2)
 raise AssertionError('Acceptance timed out')
auth=None
try:
 call('create_vbench_workspace',identity=None,error=True)
 w=call('create_vbench_workspace');auth={k:w[k] for k in ('workspace_id','workspace_key')}
 call('list_videos',auth,identity=other,error=True)
 call('list_videos',{**auth,'workspace_key':'wrong'},error=True)
 call('create_demo_video',auth)
 j=call('start_evaluation',{**auth,'videos':['demo.mp4'],'dimensions':DIMENSIONS,'timeout_seconds':1800})
 call('start_evaluation',{**auth,'videos':['demo.mp4']},error=True)
 r=wait(j,auth)
 if r['state']!='succeeded':print(r.get('log_tail'),flush=True)
 assert r['state']=='succeeded',r['state']
 assert set(r['report']['scores'])==set(DIMENSIONS)
 call('read_artifact',{**auth,'job_id':j['job_id'],'path':'../../workspace.json'},error=True)
 data=call('read_artifact',{**auth,'job_id':j['job_id'],'path':'report.json'})
 blob=base64.b64decode(data['data_base64']);assert data['eof'] and hashlib.sha256(blob).hexdigest()==data['sha256']
 cancelled=call('start_evaluation',{**auth,'videos':['demo.mp4'],'dimensions':DIMENSIONS})
 call('cancel_job',{**auth,'job_id':cancelled['job_id']});assert wait(cancelled,auth)['state']=='cancelled'
 Path('native-evidence.json').write_text(json.dumps({'report':r['report'],'report_sha256':data['sha256'],'checks':['real seven native metrics','caller isolation','capability boundary','path traversal denied','same caller queue bound','cancel','report hash']},indent=2))
 print('PASS',json.dumps(r['report']['scores']),flush=True)
finally:
 if auth:
  for j in list(s.workspaces[auth['workspace_id']]['jobs'].values()):
   if j['state'] in ('running','queued'):call('cancel_job',{**auth,'job_id':j['id']})
  time.sleep(2);call('close_vbench_workspace',auth)
 s.close()
