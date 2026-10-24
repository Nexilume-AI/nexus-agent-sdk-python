"""Caller-owned projects and a fair, bounded native KernelFoundry queue."""
import ast,base64,hashlib,hmac,json,os,re,secrets,shutil,subprocess,threading,time,uuid
from pathlib import Path
from file_bridge import Bridges,save,sha256
from broker import Broker,IMAGE,docker_flags
from job_events import Journal,Delivery
ROOT=Path(__file__).resolve().parent;DATA=ROOT/'data';CONTROL=ROOT/'control'
COMMIT='0b2338891b7b4f34e0d3a1ab930c7543046a06c9'
S={'type':'string'};AUTH={k:S for k in ('workspace_id','workspace_key')}
def tool(n,d,p=None,r=(),auth=True):return {'name':n,'description':d,'inputSchema':{'type':'object','properties':{**(AUTH if auth else {}),**(p or {})},'required':[* (AUTH if auth else {}),*r],'additionalProperties':False}}
CATALOG=[
 tool('get_kernelfoundry_status','Inspect native KernelFoundry revision, CUDA Pascal GPU readiness and queue. Autonomous optimization requires your own model authorization.',auth=False),
 tool('kernelfoundry_tool_help','Get full tool schema and description.',{'tool_name':S},('tool_name',),False),
 tool('authorize_kernelfoundry_provider','Request caller-owned Nexus/OpenAI-compatible model authorization or inspect authorization_id. Never put API keys in tool arguments; use secure approval or explicit MCP preauthorization.',{'provider':{'type':'string','enum':['nexus','openai','custom_openai'],'default':'nexus'},'model':S,'base_url':S,'authorization_id':S,'force_new':{'type':'boolean','default':False}},auth=False),
 tool('create_kernelfoundry_workspace','Create a private task workspace bound to the verified caller. Same API key means same caller. Keep both workspace_id and workspace_key. Two workspaces per caller, eight overall;24h idle expiry.',auth=False),
 tool('load_demo_project','Load a genuine CUDA ReLU task with reference, correctness tests and GPU timing benchmarks.'),
 tool('put_project_file','Upload task.py or kernel.cu, at most64KiB each. task.py must use KernelFoundry TestBase and define reference/tests/build; kernel.cu must contain EVOLVE markers. CUDA12.4, Torch2.5.1, NumPy; Pascal SM60. Server supplies fixed config/conftest, not arbitrary YAML, packages or host paths.',{'name':{'type':'string','enum':['task.py','kernel.cu']},'content':{'type':'string','maxLength':65536}},('name','content')),
 tool('validate_project','Statically validate file presence, Python syntax and required task markers. Does not execute code or certify tests.'),
 tool('build_and_test','Compile and run your task through native KernelFoundry validation without model calls. Returns background job_id; read job_status and artifacts. Fixed isolated CUDA Pascal runtime. A fresh request UUID deduplicates transport retries.',{'request_id':S,'timeout_seconds':{'type':'integer','minimum':60,'maximum':3300,'default':900}},('request_id',)),
 tool('submit_task','Run native KernelFoundry autonomous kernel optimization with your authorized model. One branch,1-20iterations; iterations+2 model calls maximum, requesting4096output tokens/call. Billed by your provider, not a monetary cap. Returns job_id and private console view_url. Use the same request UUID only for retries of the same submission.',{'authorization_id':S,'request_id':S,'goal':{'type':'string','maxLength':4000},'iterations':{'type':'integer','minimum':1,'maximum':20,'default':2},'timeout_seconds':{'type':'integer','minimum':60,'maximum':3300,'default':1800}},('authorization_id','request_id','goal')),
 tool('job_status','Read task state, bounded compiler/test logs, actual timing report and model/evaluation counts. Correctness is evaluated against caller-provided tests.',{'job_id':S},('job_id',)),
 tool('cancel_job','Cancel your running or queued task; stop evaluation containers and preserve partial artifacts.',{'job_id':S},('job_id',)),
 tool('list_artifacts','List native report, best kernel task, SQLite results, evaluations and logs.',{'job_id':S},('job_id',)),
 tool('read_artifact','Read up to24576bytes as base64, with SHA256 when the job is finished.',{'job_id':S,'path':S,'offset':{'type':'integer','minimum':0,'default':0}},('job_id','path')),
 tool('get_artifact_download','Create an authenticated HTTPS download; single-use10min redemption,1h session,Range/ETag/SHA256. Completed jobs only.',{'job_id':S,'path':S},('job_id','path')),
 tool('revoke_file_link','Revoke your download link.',{'bridge_id':S},('bridge_id',)),
 tool('close_kernelfoundry_workspace','Delete your workspace and revoke downloads. Cancel active/queued jobs first.'),
]
from demo import TASK,KERNEL,write_task
class QuietJournal:
 def emit(self,*a,**k):pass

def gpu_idle():
 try:
  p=subprocess.run(['nvidia-smi','-i','0','--query-gpu=memory.used,utilization.gpu','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=10)
  values=[int(x.strip()) for x in p.stdout.strip().split(',')]
  return p.returncode==0 and len(values)==2 and values[0]<=128 and values[1]<=5
 except (ValueError,subprocess.SubprocessError):return False

def result(v,error=False):return {'content':[{'type':'text','text':json.dumps(v)}],'structuredContent':v,'isError':error}
class Service:
 def __init__(self):
  DATA.mkdir(mode=0o700,exist_ok=True);CONTROL.mkdir(mode=0o700,exist_ok=True)
  self.lock=threading.RLock();self.stop=threading.Event();self.wake=threading.Event();self.workspaces={};self.pending=[];self.active=None;self.live={};self.deliveries={}
  for p in DATA.glob('*/workspace.json'):
   w=json.loads(p.read_text());w['folder']=p.parent;w['jobs']={}
   if w['closed']:continue
   for f in p.parent.glob('jobs/*/job.json'):
    j=json.loads(f.read_text());j['folder']=f.parent/'work'
    if j['state'] in ('running','queued'):
     subprocess.run(['docker','rm','-f',j['container']],capture_output=True,timeout=20);j['state']='interrupted';self.save_job(j)
    w['jobs'][j['id']]=j
   w['bridges']=Bridges(w);self.workspaces[w['id']]=w
  self.thread=threading.Thread(target=self.queue,daemon=True);self.thread.start()
 def save_workspace(self,w):save(w['folder']/'workspace.json',{k:v for k,v in w.items() if k not in ('folder','jobs','bridges')})
 def save_job(self,j):save(j['folder'].parent/'job.json',{k:v for k,v in j.items() if k!='folder'})
 def authorize(self,a,identity):
  w=self.workspaces.get(a.get('workspace_id'));key=a.get('workspace_key')
  if not w or w['owner']!=identity['owner_scope'] or not isinstance(key,str) or not hmac.compare_digest(w['key_hash'],hashlib.sha256(key.encode()).hexdigest()):raise ValueError('WORKSPACE_NOT_FOUND')
  w['touched']=time.time();self.save_workspace(w);return w
 def call(self,name,a,identity=None,ctx=None):
  try:
   if name not in ('get_kernelfoundry_status','kernelfoundry_tool_help'):
    if not isinstance(identity,dict) or identity.get('version')!=1 or identity.get('principal_type') not in ('api_key','user','service_account') or any(not re.fullmatch('[0-9a-f]{64}',str(identity.get(k,''))) for k in ('owner_scope','quota_scope')):raise ValueError('VERIFIED_CALLER_REQUIRED')
   with self.lock:return result(self.dispatch(name,a,identity,ctx))
  except Exception as e:
   code=str(e) if isinstance(e,ValueError) and re.fullmatch('[A-Z_0-9]+',str(e)) else 'REQUEST_FAILED'
   return result({'error':code,'message':'Check inputs, caller authorization and job status.'},True)
 def validate(self,w):
  for n in ('task.py','kernel.cu'):
   p=w['folder']/'inputs'/n
   if not p.is_file():raise ValueError('MISSING_PROJECT_FILES')
   if n=='task.py':ast.parse(p.read_text())
  if '[EVOLVE_START]' not in (w['folder']/'inputs/kernel.cu').read_text() or '[EVOLVE_END]' not in (w['folder']/'inputs/kernel.cu').read_text():raise ValueError('EVOLVE_MARKERS_REQUIRED')
  return {'valid':True,'execution':'separate networkless CPU containers','task_contract':'KernelFoundry TestBase task.py and CUDA kernel.cu with EVOLVE markers'}
 def dispatch(self,name,a,identity,ctx):
  if name=='get_kernelfoundry_status':return {'ready':(ROOT/'runtime-ready.json').is_file(),'upstream':'isl-org/kernelfoundry','commit':COMMIT,'tools':len(CATALOG),'requires_model_authorization':True,'queue':'global FIFO, one pending/active per caller','runtime':'CUDA12.4, Torch2.5.1, Pascal SM60; isolated12GB/4CPU GPU evaluator; trusted4GB/2CPU controller','supported':'CUDA task validation and native autonomous optimization; profiler disabled on Pascal'}
  if name=='kernelfoundry_tool_help':return next(t for t in CATALOG if t['name']==a.get('tool_name'))
  if name=='create_kernelfoundry_workspace':
   if len(self.workspaces)>=8 or sum(x['quota']==identity['quota_scope'] for x in self.workspaces.values())>=2:raise ValueError('WORKSPACE_LIMIT')
   wid=uuid.uuid4().hex;key=secrets.token_urlsafe(32);folder=DATA/wid;folder.mkdir(mode=0o700);(folder/'inputs').mkdir()
   w={'id':wid,'owner':identity['owner_scope'],'quota':identity['quota_scope'],'key_hash':hashlib.sha256(key.encode()).hexdigest(),'folder':folder,'closed':False,'touched':time.time(),'jobs':{}}
   w['bridges']=Bridges(w);self.workspaces[wid]=w;self.save_workspace(w);return {'workspace_id':wid,'workspace_key':key,'idle_seconds':86400}
  w=self.authorize(a,identity)
  if name=='load_demo_project':
   for n,s in [('task.py',TASK),('kernel.cu',KERNEL)]: (w['folder']/'inputs'/n).write_text(s)
   return self.validate(w)
  if name=='put_project_file':
   if a.get('name') not in ('task.py','kernel.cu') or not isinstance(a.get('content'),str) or not 1<=len(a['content'].encode())<=65536:raise ValueError('INVALID_FILE')
   (w['folder']/'inputs'/a['name']).write_text(a['content']);return {'saved':a['name'],'sha256':hashlib.sha256(a['content'].encode()).hexdigest()}
  if name=='validate_project':return self.validate(w)
  if name=='revoke_file_link':w['bridges'].revoke(a['bridge_id']);return {'revoked':True}
  if name=='close_kernelfoundry_workspace':
   if any(j['state'] in ('running','queued') for j in w['jobs'].values()):raise ValueError('JOB_BUSY')
   self.remove(w);return {'closed':True,'files_deleted':True}
  if name in ('build_and_test','submit_task'):
   rid=str(uuid.UUID(a['request_id']));aid=str(uuid.UUID(a['authorization_id'])) if name=='submit_task' else None
   for j in w['jobs'].values():
    if j['request_id']==rid:return {'job_id':j['id'],'state':j['state'],'view_url':j.get('view_url'),'idempotent':True}
   if not (ROOT/'runtime-ready.json').is_file():raise ValueError('RUNTIME_NOT_READY')
   self.validate(w)
   cfg={k:a.get(k,v) for k,v in {'iterations':2 if name=='submit_task' else 0,'evaluation_seconds':600,'timeout_seconds':1800}.items()}
   for k,lo,hi in [('iterations',1 if name=='submit_task' else 0,20),('evaluation_seconds',600,600),('timeout_seconds',60,3300)]:
    if type(cfg[k])!=int or not lo<=cfg[k]<=hi:raise ValueError('INVALID_LIMIT')
   a={**a,'goal':a.get('goal','Validate the supplied CUDA task.')}
   if not isinstance(a.get('goal'),str) or not 1<=len(a['goal'])<=4000:raise ValueError('INVALID_GOAL')
   if len(w['jobs'])>=16 or len(self.pending)>=8 or any(j['quota']==w['quota'] for j in self.pending) or (self.active and self.active['quota']==w['quota']):raise ValueError('CALLER_BUSY_OR_QUEUE_FULL')
   grant={};credentials=None;delivery=None
   if aid:
    from nexus_agent.credentials import ProviderCredentials
    from nexus_agent.background_jobs import BackgroundJobs
    credentials=ProviderCredentials(ctx);grant=credentials.resolve_for_runtime(aid)
    if grant.get('provider') not in ('nexus','openai','custom_openai') or not grant.get('model'):grant.clear();raise ValueError('OPENAI_COMPATIBLE_MODEL_REQUIRED')
    try:delivery=BackgroundJobs(ctx).register_for_runtime(aid,rid)
    except Exception:grant.clear();raise
   jid=uuid.uuid4().hex;folder=w['folder']/'jobs'/jid/'work';folder.mkdir(parents=True)
   private=CONTROL/jid;private.mkdir(mode=0o700)
   cfg.update(goal=a['goal'],model=grant.get('model','none'),mode='optimize' if aid else 'validate');save(folder/'request.json',cfg)
   write_task(folder/'task',cfg,w['folder']/'inputs')
   j={'id':jid,'quota':w['quota'],'request_id':rid,'authorization_id':aid,'folder':folder,'state':'queued','config':cfg,'created':time.time(),'container':'nexus-kernelfoundry-'+jid,'view_url':delivery['view_url'] if delivery else None}
   journal=Journal(private,delivery) if delivery else QuietJournal();cancel=threading.Event()
   if delivery:
    d=Delivery(journal,cancel);self.deliveries[jid]=d;d.start()
   self.live[jid]={'grant':grant,'credentials':credentials,'journal':journal,'cancel':cancel}
   journal.emit('state',{'job_id':jid,'status':'queued'});self.save_job(j);w['jobs'][jid]=j;self.pending.append(j);self.wake.set()
   return {'job_id':jid,'state':'queued','view_url':j['view_url']}
  j=w['jobs'].get(a.get('job_id'))
  if not j:raise ValueError('JOB_NOT_FOUND')
  if name=='cancel_job':
   if j['state'] in ('running','queued'):self.live[j['id']]['cancel'].set()
   return {'cancellation_requested':True,'state':j['state']}
  if name=='job_status':
   out={k:j.get(k) for k in ('id','state','created','started','ended','view_url','error')}
   for n in ('report.json','broker-metrics.json'):
    if (j['folder']/n).exists():out[n.split('.')[0]]=json.loads((j['folder']/n).read_text())
   p=j['folder']/'run.log'
   if p.exists():
    with p.open('rb') as f:f.seek(max(0,p.stat().st_size-10000));out['log_tail']=f.read().decode(errors='replace')
   return out
  if name=='list_artifacts':return {'files':[{'path':str(p.relative_to(j['folder'])),'size':p.stat().st_size} for p in j['folder'].rglob('*') if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(j['folder'])][:500]}
  path=a.get('path')
  if not isinstance(path,str) or len(path)>240:raise ValueError('INVALID_PATH')
  p=(j['folder']/path).resolve()
  if not p.is_relative_to(j['folder']) or not p.is_file():raise ValueError('ARTIFACT_NOT_FOUND')
  if name=='get_artifact_download':
   if j['state'] in ('running','queued'):raise ValueError('JOB_BUSY')
   return w['bridges'].create(ctx,p,'download',sha256(p),p.stat().st_size)
  if name=='read_artifact':
   offset=a.get('offset',0)
   if type(offset)!=int or not 0<=offset<=p.stat().st_size:raise ValueError('INVALID_OFFSET')
   with p.open('rb') as f:f.seek(offset);raw=f.read(24576)
   return {'data_base64':base64.b64encode(raw).decode(),'next_offset':offset+len(raw),'eof':offset+len(raw)>=p.stat().st_size,'sha256':sha256(p) if j['state'] not in ('running','queued') else None}
  raise ValueError('UNKNOWN_TOOL')
 def remove(self,w):
  w['closed']=True;w['bridges'].close();self.workspaces.pop(w['id']);assert w['folder'].parent==DATA;shutil.rmtree(w['folder'])
  for jid in w['jobs']:
   d=self.deliveries.pop(jid,None)
   if d:d.stop.set()
   private=CONTROL/jid
   assert private.parent==CONTROL and re.fullmatch('[0-9a-f]{32}',jid)
   if private.exists():shutil.rmtree(private)
 def queue(self):
  while not self.stop.is_set():
   self.wake.wait(2);self.wake.clear()
   with self.lock:
    for w in list(self.workspaces.values()):
     if time.time()-w['touched']>86400 and not any(j['state'] in ('running','queued') for j in w['jobs'].values()):self.remove(w)
    if not self.pending:continue
    if not self.live[self.pending[0]['id']]['cancel'].is_set() and not gpu_idle():continue
    j=self.pending.pop(0);self.active=j;j['state']='running';j['started']=time.time();self.save_job(j)
   live=self.live[j['id']];journal=live['journal'];broker=None
   try:
    if live['cancel'].is_set():j['state']='cancelled';continue
    broker=Broker(j,CONTROL/j['id'],live['grant'],lambda:live['credentials'].status(j['authorization_id']) if live['credentials'] else {},lambda stage,value:journal.emit('log',{'text':json.dumps({'stage':stage,**value})}))
    journal.emit('state',{'job_id':j['id'],'status':'running'})
    cmd=docker_flags(j['container'],'4g')+['--mount',f'type=bind,src={j["folder"]},dst=/work','--mount',f'type=bind,src={CONTROL/j["id"]/"ipc"},dst=/broker,readonly',IMAGE]
    with (j['folder']/'run.log').open('wb') as f:
     p=subprocess.Popen(cmd,stdout=f,stderr=f)
     while p.poll() is None:
      forced='interrupted' if self.stop.is_set() else 'cancelled' if live['cancel'].is_set() else 'timed_out' if time.time()-j['started']>j['config']['timeout_seconds'] else None
      if f.tell()>16777216:forced='output_limit'
      if forced:
       j['state']=forced;broker.cancel.set();subprocess.run(['docker','rm','-f',j['container']],capture_output=True,timeout=20)
      time.sleep(.3)
    if j['state']=='running':j['state']='succeeded' if p.returncode==0 and (j['folder']/'report.json').exists() else 'failed'
    save(j['folder']/'broker-metrics.json',{'model_requests':broker.model_count,'evaluations':broker.eval_count})
   except Exception as e:j['state']='failed';j['error']=type(e).__name__
   finally:
    if broker:broker.close()
    live['grant'].clear();j['ended']=time.time();self.save_job(j)
    save(CONTROL/j['id']/'job.json',{'artifacts':[]});journal.emit('state',{'job_id':j['id'],'status':j['state']})
    self.live.pop(j['id'],None)
    with self.lock:self.active=None
    self.wake.set()
 def close(self):self.stop.set();self.wake.set();self.thread.join(timeout=60)
