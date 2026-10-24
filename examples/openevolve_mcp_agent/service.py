"""Caller-owned projects and a fair, bounded native OpenEvolve queue."""
import ast,base64,hashlib,hmac,json,os,re,secrets,shutil,subprocess,threading,time,uuid
from pathlib import Path
from file_bridge import Bridges,save,sha256
from broker import Broker,IMAGE,docker_flags
from job_events import Journal,Delivery
ROOT=Path(__file__).resolve().parent;DATA=ROOT/'data';CONTROL=ROOT/'control'
COMMIT='04a37f191b2eb367daef455057a0470c0ac853d8'
S={'type':'string'};AUTH={k:S for k in ('workspace_id','workspace_key')}
def tool(n,d,p=None,r=(),auth=True):return {'name':n,'description':d,'inputSchema':{'type':'object','properties':{**(AUTH if auth else {}),**(p or {})},'required':[* (AUTH if auth else {}),*r],'additionalProperties':False}}
CATALOG=[
 tool('get_openevolve_status','Inspect native OpenEvolve readiness, revision, CPU sandbox limits and queue. A model authorization is required for optimization.',auth=False),
 tool('openevolve_tool_help','Get the full description and schema of a tool.',{'tool_name':S},('tool_name',),False),
 tool('authorize_openevolve_provider','Request caller-owned model authorization or check an authorization_id. Nexus supports selecting your Router; OpenAI-compatible providers use a secure page or explicit MCP preauthorization. Never send an API key in tool arguments or chat.',{'provider':{'type':'string','enum':['nexus','openai','custom_openai'],'default':'nexus'},'model':S,'base_url':S,'authorization_id':S,'force_new':{'type':'boolean','default':False}},auth=False),
 tool('create_openevolve_workspace','Create a private project bound to verified caller identity. Same Nexus API key means the same person. Keep workspace_id and workspace_key. Two projects/caller, eight total, 24-hour idle lifetime.',auth=False),
 tool('load_demo_project','Load a small deterministic numerical optimization problem and evaluator. Demonstrates the pipeline, not general performance.'),
 tool('put_project_file','Upload initial.py or evaluator.py as UTF-8 source, up to 64 KiB each. Evaluator must define evaluate(program_path) returning finite numeric metrics including combined_score (higher is better). Both execute only in a separate networkless CPU sandbox. Python stdlib and NumPy available.',{'name':{'type':'string','enum':['initial.py','evaluator.py']},'content':{'type':'string','maxLength':65536}},('name','content')),
 tool('validate_project','Check required files and Python syntax without executing uploaded code. No guarantee of evaluator quality or candidate correctness.'),
 tool('start_evolution','Start native OpenEvolve with your approved model grant. Returns background job_id and console view_url. Requires a fresh UUID request_id; reuse it on transport retries. FIFO, one active/queued job per caller. Model calls are billed by your provider; at most iterations+2 requests with 4096 output tokens requested per call. Evaluation score is caller-defined, not independent quality certification.',{'authorization_id':S,'request_id':S,'goal':{'type':'string','maxLength':4000},'iterations':{'type':'integer','minimum':1,'maximum':100,'default':5},'seed':{'type':'integer','minimum':0,'maximum':2147483647,'default':42},'evaluation_seconds':{'type':'integer','minimum':1,'maximum':60,'default':20},'timeout_seconds':{'type':'integer','minimum':60,'maximum':3300,'default':1800}},('authorization_id','request_id','goal')),
 tool('job_status','Read job state, bounded logs, model/evaluation counters and final report. Background console receives pushed state and evaluation events.',{'job_id':S},('job_id',)),
 tool('cancel_job','Cancel your queued or running optimization, including evaluator containers; preserves partial artifacts.',{'job_id':S},('job_id',)),
 tool('list_artifacts','List generated best_program.py, report, evolution traces, checkpoints and evaluation metrics. Provider credentials are never stored in artifacts.',{'job_id':S},('job_id',)),
 tool('read_artifact','Read up to 24576 bytes as base64 with SHA256 on completed artifacts.',{'job_id':S,'path':S,'offset':{'type':'integer','minimum':0,'default':0}},('job_id','path')),
 tool('get_artifact_download','Get a one-time authenticated HTTPS artifact link, ten-minute redemption, one-hour session, Range/ETag/SHA256. Completed jobs only.',{'job_id':S,'path':S},('job_id','path')),
 tool('revoke_file_link','Revoke your artifact link.',{'bridge_id':S},('bridge_id',)),
 tool('close_openevolve_workspace','Revoke links and delete this workspace. Cancel active/queued jobs first.'),
]
DEMO_INITIAL='''"""Optimize solution() to minimize (x-3)**2 + (y+2)**2. Return a pair of finite numbers."""\ndef solution():\n    return (0.0, 0.0)\n'''
DEMO_EVALUATOR='''import importlib.util, math\ndef evaluate(program_path):\n    spec=importlib.util.spec_from_file_location("candidate",program_path)\n    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)\n    x,y=mod.solution()\n    if not math.isfinite(x) or not math.isfinite(y):raise ValueError("nonfinite")\n    loss=(x-3)**2+(y+2)**2\n    return {"combined_score":1/(1+loss),"loss":loss}\n'''
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
   if name not in ('get_openevolve_status','openevolve_tool_help'):
    if not isinstance(identity,dict) or identity.get('version')!=1 or identity.get('principal_type') not in ('api_key','user','service_account') or any(not re.fullmatch('[0-9a-f]{64}',str(identity.get(k,''))) for k in ('owner_scope','quota_scope')):raise ValueError('VERIFIED_CALLER_REQUIRED')
   with self.lock:return result(self.dispatch(name,a,identity,ctx))
  except Exception as e:
   code=str(e) if isinstance(e,ValueError) and re.fullmatch('[A-Z_0-9]+',str(e)) else 'REQUEST_FAILED'
   return result({'error':code,'message':'Check inputs, caller authorization and job status.'},True)
 def validate(self,w):
  for n in ('initial.py','evaluator.py'):
   p=w['folder']/'inputs'/n
   if not p.is_file():raise ValueError('MISSING_PROJECT_FILES')
   ast.parse(p.read_text())
  return {'valid':True,'execution':'separate networkless CPU containers','evaluator_contract':'evaluate(program_path) -> finite numeric metrics with combined_score'}
 def dispatch(self,name,a,identity,ctx):
  if name=='get_openevolve_status':return {'ready':(ROOT/'runtime-ready.json').is_file(),'upstream':'algorithmicsuperintelligence/openevolve','commit':COMMIT,'tools':len(CATALOG),'requires_model_authorization':True,'queue':'global FIFO, one pending/active per caller','runtime':'CPU Python stdlib and NumPy; networkless evaluator2GB/2CPU/128PID; trusted controller4GB/2CPU','supported':'Python program evolution with custom Python evaluator; no GPU or user package installation'}
  if name=='openevolve_tool_help':return next(t for t in CATALOG if t['name']==a.get('tool_name'))
  if name=='create_openevolve_workspace':
   if len(self.workspaces)>=8 or sum(x['quota']==identity['quota_scope'] for x in self.workspaces.values())>=2:raise ValueError('WORKSPACE_LIMIT')
   wid=uuid.uuid4().hex;key=secrets.token_urlsafe(32);folder=DATA/wid;folder.mkdir(mode=0o700);(folder/'inputs').mkdir()
   w={'id':wid,'owner':identity['owner_scope'],'quota':identity['quota_scope'],'key_hash':hashlib.sha256(key.encode()).hexdigest(),'folder':folder,'closed':False,'touched':time.time(),'jobs':{}}
   w['bridges']=Bridges(w);self.workspaces[wid]=w;self.save_workspace(w);return {'workspace_id':wid,'workspace_key':key,'idle_seconds':86400}
  w=self.authorize(a,identity)
  if name=='load_demo_project':
   for n,s in [('initial.py',DEMO_INITIAL),('evaluator.py',DEMO_EVALUATOR)]: (w['folder']/'inputs'/n).write_text(s)
   return self.validate(w)
  if name=='put_project_file':
   if a.get('name') not in ('initial.py','evaluator.py') or not isinstance(a.get('content'),str) or not 1<=len(a['content'].encode())<=65536:raise ValueError('INVALID_FILE')
   (w['folder']/'inputs'/a['name']).write_text(a['content']);return {'saved':a['name'],'sha256':hashlib.sha256(a['content'].encode()).hexdigest()}
  if name=='validate_project':return self.validate(w)
  if name=='revoke_file_link':w['bridges'].revoke(a['bridge_id']);return {'revoked':True}
  if name=='close_openevolve_workspace':
   if any(j['state'] in ('running','queued') for j in w['jobs'].values()):raise ValueError('JOB_BUSY')
   self.remove(w);return {'closed':True,'files_deleted':True}
  if name=='start_evolution':
   rid=str(uuid.UUID(a['request_id']));aid=str(uuid.UUID(a['authorization_id']))
   for j in w['jobs'].values():
    if j['request_id']==rid:return {'job_id':j['id'],'state':j['state'],'view_url':j.get('view_url'),'idempotent':True}
   if not (ROOT/'runtime-ready.json').is_file():raise ValueError('RUNTIME_NOT_READY')
   self.validate(w)
   cfg={k:a.get(k,v) for k,v in {'iterations':5,'seed':42,'evaluation_seconds':20,'timeout_seconds':1800}.items()}
   for k,lo,hi in [('iterations',1,100),('seed',0,2147483647),('evaluation_seconds',1,60),('timeout_seconds',60,3300)]:
    if type(cfg[k])!=int or not lo<=cfg[k]<=hi:raise ValueError('INVALID_LIMIT')
   if not isinstance(a.get('goal'),str) or not 1<=len(a['goal'])<=4000:raise ValueError('INVALID_GOAL')
   if len(w['jobs'])>=16 or len(self.pending)>=8 or any(j['quota']==w['quota'] for j in self.pending) or (self.active and self.active['quota']==w['quota']):raise ValueError('CALLER_BUSY_OR_QUEUE_FULL')
   from nexus_agent.credentials import ProviderCredentials
   from nexus_agent.background_jobs import BackgroundJobs
   credentials=ProviderCredentials(ctx);grant=credentials.resolve_for_runtime(aid)
   if grant.get('provider') not in ('nexus','openai','custom_openai') or not grant.get('model'):grant.clear();raise ValueError('OPENAI_COMPATIBLE_MODEL_REQUIRED')
   try:delivery=BackgroundJobs(ctx).register_for_runtime(aid,rid)
   except Exception:grant.clear();raise
   jid=uuid.uuid4().hex;folder=w['folder']/'jobs'/jid/'work';folder.mkdir(parents=True)
   private=CONTROL/jid;private.mkdir(mode=0o700);shutil.copyfile(w['folder']/'inputs/evaluator.py',private/'evaluator.py');shutil.copyfile(w['folder']/'inputs/initial.py',folder/'initial.py')
   cfg.update(goal=a['goal'],model=grant['model']);save(folder/'request.json',cfg)
   j={'id':jid,'quota':w['quota'],'request_id':rid,'authorization_id':aid,'folder':folder,'state':'queued','config':cfg,'created':time.time(),'container':'nexus-openevolve-'+jid,'view_url':delivery['view_url']}
   journal=Journal(private,delivery);cancel=threading.Event();d=Delivery(journal,cancel);self.deliveries[jid]=d;d.start()
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
    j=self.pending.pop(0);self.active=j;j['state']='running';j['started']=time.time();self.save_job(j)
   live=self.live[j['id']];journal=live['journal'];broker=None
   try:
    if live['cancel'].is_set():j['state']='cancelled';continue
    broker=Broker(j,CONTROL/j['id'],live['grant'],lambda:live['credentials'].status(j['authorization_id']),lambda stage,value:journal.emit('log',{'text':json.dumps({'stage':stage,**value})}))
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
