import base64,json,hashlib,time
from pathlib import Path
from service import Service
identity={'version':1,'principal_type':'api_key','owner_scope':'a'*64,'quota_scope':'a'*64}
other={**identity,'owner_scope':'b'*64,'quota_scope':'b'*64}
s=Service()
def call(name,args=None,owner=identity,error=False):
 r=s.call(name,args or {},owner)
 assert bool(r.get('isError'))==error,(name,r)
 return r.get('structuredContent',{})
w=call('create_accelforge_workspace');auth={k:w[k] for k in ('workspace_id','workspace_key')}
def job(args):
 j=call('start_analysis',{**auth,**args});last=''
 deadline=time.monotonic()+320
 while time.monotonic()<deadline:
  r=call('job_status',{**auth,'job_id':j['job_id']})
  if r['state']!='running':
   print('JOB',args.get('mode','map'),r['state'],r.get('summary',{}),flush=True)
   if r['state']!='succeeded':print(r['log_tail'],flush=True)
   assert r['state']=='succeeded';return r
  time.sleep(2)
 raise AssertionError('Job wait exceeded')
try:
 assert len(s.catalog)==13
 assert call('list_examples',auth)['files']
 mapped=job({})
 assert mapped['summary']['mapping_count']>0 and mapped['summary']['selected_energy_j']>0 and mapped['summary']['selected_latency_s']>0
 evaluated=job({'mode':'evaluate','workload':'example:workloads/basic/matmuls.yaml','mapping':'example:mappings/unfused_matmuls_to_simple.yaml'})
 assert evaluated['summary']['mapping_count']==1
 # Reuse an uploaded architecture through the actual JSON/YAML pipeline.
 src=call('read_example',{**auth,'path':'arches/simple.yaml'})['text'].encode()
 for offset in range(0,len(src),1024):call('upload_spec',{**auth,'filename':'custom.yaml','offset':offset,'data_base64':base64.b64encode(src[offset:offset+1024]).decode()})
 validated=job({'mode':'validate','architecture':'file:custom.yaml'})
 assert validated['summary']['validated']
 costs=job({'mode':'costs','architecture':'example:arches/eyeriss.yaml'})
 assert costs['summary']['area_m2']>0
 j=mapped['job_id'];path='jobs/'+j+'/results.json';data=b'';offset=0
 while True:
  r=call('read_artifact',{**auth,'path':path,'offset':offset});data+=base64.b64decode(r['data_base64']);offset=r['next_offset']
  if r['eof']:break
 assert hashlib.sha256(data).hexdigest()==r['sha256'] and json.loads(data)
 checksum=r['sha256']
 call('list_artifacts',auth,owner=other,error=True)
 call('list_artifacts',{**auth,'workspace_key':'wrong'},error=True)
 call('read_artifact',{**auth,'path':'/etc/passwd'},error=True)
 call('read_example',{**auth,'path':'../../etc/passwd'},error=True)
 call('upload_spec',{**auth,'filename':'bad.yaml','offset':0,'data_base64':base64.b64encode(b'arch: [').decode()})
 bad=call('start_analysis',{**auth,'mode':'validate','architecture':'file:bad.yaml'})
 for _ in range(60):
  r=call('job_status',{**auth,'job_id':bad['job_id']})
  if r['state']!='running':break
  time.sleep(1)
 assert r['state']=='failed'
 # Immediate cancellation exercises native process-group cleanup.
 cancel=call('start_analysis',{**auth,'parameters':{'N_EINSUMS':4,'M':256,'KN':256}})
 call('cancel_analysis',{**auth,'job_id':cancel['job_id']})
 time.sleep(1);assert call('job_status',{**auth,'job_id':cancel['job_id']})['state']=='cancelled'
 Path('native-evidence.json').write_text(json.dumps({'mapping':mapped,'evaluation':evaluated,'validation':validated,'costs':costs,'artifact_sha256':checksum,'checks':['13 tools','native map','evaluate','custom YAML','component models','download SHA256','caller isolation','key boundary','paths','invalid YAML','cancel']},indent=2))
 print('PASS native AccelForge and isolation acceptance',flush=True)
finally:
 call('close_accelforge_workspace',auth);s.close()
