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
w=call('create_sst_workspace');auth={k:w[k] for k in ('workspace_id','workspace_key')}
def wait(j):
 for _ in range(180):
  r=call('job_status',{**auth,'job_id':j['job_id']})
  if r['state']!='running':return r
  time.sleep(1)
 raise AssertionError('Job wait exceeded')
def run(args):
 r=wait(call('start_simulation',{**auth,**args}))
 print('JOB',args,r['state'],r.get('summary'),flush=True)
 if r['state']!='succeeded':print(r['log_tail'],flush=True)
 assert r['state']=='succeeded';return r
def upload(name,data):
 for offset in range(0,len(data),1024):call('upload_model',{**auth,'filename':name,'offset':offset,'data_base64':base64.b64encode(data[offset:offset+1024]).decode()})
try:
 assert len(s.catalog)==14
 assert len(call('list_examples',auth)['files'])==3
 assert 'eventsToSend' in call('component_info',{**auth,'component':'simpleElementExample.example0'})['text']
 events=run({});assert events['summary']['simulated_time']=='52 ns',events
 threaded=run({'threads':2});assert threaded['summary']['simulated_time']=='52 ns'
 memory=run({'model':'example:memory.py'});assert 'Test Completed Successfuly' in memory['log_tail']
 network=run({'model':'example:network.py'});assert network['summary']['simulated_time'] and network['log_tail'].count('received all packets')==4
 src=call('read_example',{**auth,'path':'events.py'})['text'].replace('50,','75,').encode()
 upload('custom.py',src)
 custom=run({'model':'file:custom.py'});assert custom['summary']['simulated_time']=='77 ns'
 path='jobs/'+memory['job_id']+'/stats.csv';data=b'';offset=0
 while True:
  r=call('read_artifact',{**auth,'path':path,'offset':offset});data+=base64.b64decode(r['data_base64']);offset=r['next_offset']
  if r['eof']:break
 assert hashlib.sha256(data).hexdigest()==r['sha256'] and b'CacheHits' in data and b'CacheMisses' in data,(len(data),data[:500])
 checksum=r['sha256']
 call('list_artifacts',auth,owner=other,error=True)
 call('list_artifacts',{**auth,'workspace_key':'wrong'},error=True)
 call('read_artifact',{**auth,'path':'/etc/passwd'},error=True)
 call('read_example',{**auth,'path':'../../etc/passwd'},error=True)
 call('create_sst_workspace',owner=None,error=True)
 upload('bad.py',b'not valid python !!!')
 bad=wait(call('start_simulation',{**auth,'model':'file:bad.py'}));assert bad['state']=='failed'
 upload('slow.py',b'import time; time.sleep(60)')
 cancel=call('start_simulation',{**auth,'model':'file:slow.py'});call('cancel_simulation',{**auth,'job_id':cancel['job_id']})
 assert wait(cancel)['state']=='cancelled'
 timeout=wait(call('start_simulation',{**auth,'model':'file:slow.py','timeout_seconds':1}));assert timeout['state']=='timed_out'
 stopped=run({'stop_at':'10ns'});assert stopped['summary']['stopped_at_limit'],stopped
 Path('native-evidence.json').write_text(json.dumps({'events':events,'threads2':threaded,'memory':memory,'network':network,'custom':custom,'stopped':stopped,'artifact_sha256':checksum,'checks':['14 tools','native events','memory statistics','network','2 threads','custom Python','download SHA256','caller isolation','key boundary','paths','invalid model','cancel','wall timeout','simulation stop']},indent=2))
 print('PASS native SST simulation and isolation acceptance',flush=True)
finally:
 call('close_sst_workspace',auth);s.close()
