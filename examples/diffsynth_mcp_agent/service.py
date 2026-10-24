"""Authenticated DiffSynth workspaces, bounded uploads and one fair GPU queue."""
import math, base64, copy, hashlib, hmac, json, os, re, secrets, shutil, subprocess, threading, time, uuid
from pathlib import Path
from file_bridge import Bridges, sha256, save

ROOT=Path(__file__).resolve().parent;DATA=ROOT/'data'
IMAGE='nexus-diffsynth-mcp:d188f01d0901';COMMIT='d188f01d09013688dea27cc28f2e8dc0953c2543'
RECIPE={'model_id':'wan2.1-t2v-1.3b','base_model':'Wan-AI/Wan2.1-T2V-1.3B','method':'Native FlowMatch SFT LoRA','target_modules':['q','k','v','o','ffn.0','ffn.2'],'rank_options':[4,8,16,32],'max_steps':1000,'max_frames':17,'max_side':256,'quality_validated':False}
AUTH={'workspace_id':{'type':'string'},'workspace_key':{'type':'string','description':'Private capability returned by create_diffsynth_workspace; use only with the same authenticated caller.'}}
S={'type':'string'}
def result(v,error=False):return {'content':[{'type':'text','text':json.dumps(v)}],'structuredContent':v,'isError':error}
def tool(name,desc,props=None,required=(),auth=True):return {'name':name,'description':desc,'inputSchema':{'type':'object','properties':{**(AUTH if auth else {}),**(props or {})},'required':[* (AUTH if auth else {}),*required],'additionalProperties':False}}
CATALOG=[
 tool('get_diffsynth_status','Inspect installed DiffSynth revision, tested LoRA recipe, readiness and limits. No model-provider API key required.',auth=False),
 tool('list_recipes','List installed and tested LoRA recipes. Only Wan 2.1 T2V 1.3B is installed; other upstream models and full-model training are not exposed.',auth=False),
 tool('diffsynth_tool_help','Return full instructions and JSON input schema for one tool.',{'tool_name':S},('tool_name',),auth=False),
 tool('create_diffsynth_workspace','Create isolated video/report storage bound to the server-verified caller. Same API key means one caller. Save workspace_id/key. Two workspaces per caller, eight total; 24-hour idle expiry. Close deletes files and revokes links.',auth=False),
 tool('create_demo_video','Copy a small bundled moving-pattern MP4 into your workspace for pipeline testing. It is synthetic and not evidence of generation quality.'),
 tool('begin_video_upload','Reserve an MP4 using exact size and SHA256; returns a one-time authenticated browser upload link with resumable chunks. Max 128 MiB/file and 8 videos/workspace. Renew using the same metadata. No arbitrary URL fetch.',{'name':S,'size_bytes':{'type':'integer','minimum':1,'maximum':134217728},'sha256':S},('name','size_bytes','sha256')),
 tool('upload_video_chunk','Programmatic MCP upload alternative: reserve with begin_video_upload, then sequential base64 chunks up to 1024 decoded bytes. Exact retries supported; never overwrite a sealed video.',{'name':S,'offset':{'type':'integer','minimum':0},'data_base64':S},('name','offset','data_base64')),
 tool('finalize_video_upload','Revoke upload links, check complete size and SHA256, and seal video for jobs. Video codec/dimensions are checked inside the training sandbox.',{'name':S},('name',)),
 tool('list_videos','List only your uploaded/sealed videos and resumable byte offsets.'),
 tool('start_lora_training','Train a native DiffSynth Wan 1.3B LoRA from sealed MP4 clips and captions. Returns background job_id; globally FIFO, one active/pending job per caller. Exports LoRA safetensors, finite-loss report, exact adapter reload check and generated preview. Samples are resized and uniformly sampled. No arbitrary model/code/URL accepted.',{
  'samples':{'type':'array','minItems':1,'maxItems':8,'items':{'type':'object','properties':{'video':S,'caption':{'type':'string','minLength':1,'maxLength':1500}},'required':['video','caption'],'additionalProperties':False}},
  'steps':{'type':'integer','minimum':1,'maximum':1000,'default':20},'rank':{'type':'integer','enum':[4,8,16,32],'default':8},
  'learning_rate':{'type':'number','minimum':0.000001,'maximum':0.001,'default':0.0001},
  'height':{'type':'integer','minimum':64,'maximum':256,'default':128},'width':{'type':'integer','minimum':64,'maximum':256,'default':128},
  'num_frames':{'type':'integer','enum':[5,9,13,17],'default':9},'seed':{'type':'integer','minimum':0,'maximum':2147483647,'default':42},
  'preview_prompt':{'type':'string','minLength':1,'maxLength':1500,'default':'A small colorful object moves smoothly across a table.'},
  'preview_steps':{'type':'integer','minimum':4,'maximum':30,'default':8},
  'timeout_seconds':{'type':'integer','minimum':60,'maximum':21600,'default':7200}},('samples',)),
 tool('job_status','Read job state, actual training progress, bounded logs and completed training report.',{'job_id':S},('job_id',)),
 tool('cancel_job','Cancel your queued/running training and retain partial artifacts.',{'job_id':S},('job_id',)),
 tool('list_artifacts','List your LoRA safetensors, training report, metrics, preview and inputs.',{'job_id':S},('job_id',)),
 tool('read_artifact','Download artifact as base64 pages up to 24576 bytes. Completed files include SHA256; concatenate and verify.',{'job_id':S,'path':S,'offset':{'type':'integer','minimum':0,'default':0}},('job_id','path')),
 tool('get_artifact_download','Create one-time authenticated HTTPS download link. Redeem within 10 minutes; session lasts one hour and supports Range/ETag/SHA256. Completed jobs only.',{'job_id':S,'path':S},('job_id','path')),
 tool('revoke_file_link','Revoke one of your upload/download capabilities.',{'bridge_id':S},('bridge_id',)),
 tool('close_diffsynth_workspace','Revoke links and permanently remove this workspace and its videos/reports. Cancel running or queued jobs first.'),
]

class Service:
 def __init__(self):
  DATA.mkdir(mode=0o700,exist_ok=True);self.lock=threading.RLock();self.stop=threading.Event();self.wake=threading.Event()
  self.catalog=copy.deepcopy(CATALOG);self.workspaces={};self.pending=[];self.active=None
  for p in DATA.glob('*/workspace.json'):
   w=json.loads(p.read_text());w['folder']=p.parent;w['jobs']={}
   for f in p.parent.glob('jobs/*/job.json'):
    j=json.loads(f.read_text());j['folder']=f.parent
    if j['state'] in ('running','queued'):
     subprocess.run(['docker','rm','-f',j['container']],capture_output=True,timeout=30);j['state']='interrupted';self.save_job(j)
    w['jobs'][j['id']]=j
   if not w['closed']:w['bridges']=Bridges(w);self.workspaces[w['id']]=w
  self.thread=threading.Thread(target=self.run_queue,daemon=True);self.thread.start()
 def save_workspace(self,w):save(w['folder']/'workspace.json',{k:v for k,v in w.items() if k not in ('folder','jobs','bridges')})
 def save_job(self,j):save(j['folder']/'job.json',{k:v for k,v in j.items() if k!='folder'})
 def authorize(self,a,identity):
  wid=a.get('workspace_id');w=self.workspaces.get(wid) if isinstance(wid,str) else None;key=a.get('workspace_key')
  if not w or w['owner']!=identity['owner_scope'] or not isinstance(key,str) or not hmac.compare_digest(w['key_hash'],hashlib.sha256(key.encode()).hexdigest()):raise ValueError('WORKSPACE_NOT_FOUND')
  w['touched']=time.time();self.save_workspace(w);return w
 def call(self,name,args,identity=None,context=None):
  try:
   if name not in ('get_diffsynth_status','list_recipes','diffsynth_tool_help'):
    if not isinstance(identity,dict) or identity.get('version')!=1 or identity.get('principal_type') not in ('api_key','user','service_account') or any(not isinstance(identity.get(k),str) or not re.fullmatch('[0-9a-f]{64}',identity[k]) for k in ('owner_scope','quota_scope')):raise ValueError('VERIFIED_CALLER_REQUIRED')
   with self.lock:return result(self.dispatch(name,args,identity,context))
  except (ValueError,KeyError,OSError) as e:return result({'error':'DIFFSYNTH_REQUEST_FAILED','message':str(e)[:700]},True)
 def dispatch(self,name,a,identity,ctx):
  if name=='get_diffsynth_status':return {'upstream':'modelscope/DiffSynth-Studio','commit':COMMIT,'ready':(ROOT/'runtime-ready.json').is_file(),'tools':len(CATALOG),'recipe':RECIPE,'gpu':'dedicated physical GPU 2','queue':'One active job globally; one running/queued per verified caller; FIFO','transport':'Nexus Cloud MCP over OpenWrt Relay'}
  if name=='list_recipes':return {'recipes':[RECIPE],'ready':(ROOT/'runtime-ready.json').is_file(),'not_supported':['arbitrary model architectures','user Python/YAML execution','full-model training','training quality certification']}
  if name=='diffsynth_tool_help':
   row=next((t for t in CATALOG if t['name']==a.get('tool_name')),None)
   if row is None:raise ValueError('Unknown tool')
   return row
  if name=='create_diffsynth_workspace':
   if len(self.workspaces)>=8 or sum(w['quota']==identity['quota_scope'] for w in self.workspaces.values())>=2:raise ValueError('WORKSPACE_LIMIT; close an unused workspace')
   if shutil.disk_usage(DATA).free<5*1024**3:raise ValueError('INSUFFICIENT_STORAGE')
   wid=uuid.uuid4().hex;key=secrets.token_urlsafe(32);folder=DATA/wid;folder.mkdir(mode=0o700);(folder/'inputs').mkdir()
   w={'id':wid,'folder':folder,'owner':identity['owner_scope'],'quota':identity['quota_scope'],'key_hash':hashlib.sha256(key.encode()).hexdigest(),'closed':False,'videos':{},'jobs':{},'touched':time.time()}
   w['bridges']=Bridges(w);self.workspaces[wid]=w;self.save_workspace(w)
   return {'workspace_id':wid,'workspace_key':key,'idle_seconds':86400,'next_step':'begin_video_upload or create_demo_video, then start_lora_training with samples/video/caption'}
  w=self.authorize(a,identity)
  if name=='create_demo_video':
   p=w['folder']/'inputs/demo.mp4'
   if 'demo.mp4' not in w['videos']:
    if len(w['videos'])>=8:raise ValueError('VIDEO_LIMIT')
    shutil.copyfile(ROOT/'demo.mp4',p);w['videos']['demo.mp4']={'name':'demo.mp4','size_bytes':p.stat().st_size,'sha256':sha256(p),'sealed':True};self.save_workspace(w)
   return w['videos']['demo.mp4']
  if name=='begin_video_upload':
   n=a['name'];size=a['size_bytes'];digest=a['sha256']
   if not isinstance(n,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,75}\.mp4',n):raise ValueError('Use a simple .mp4 filename')
   if type(size)!=int or not 1<=size<=134217728 or not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('Invalid size or SHA256')
   p=w['folder']/'inputs'/n;row=w['videos'].get(n)
   if row and (row['sealed'] or row['size_bytes']!=size or row['sha256']!=digest):raise ValueError('VIDEO_ALREADY_RESERVED_OR_SEALED')
   if not row:
    if len(w['videos'])>=8:raise ValueError('VIDEO_LIMIT')
    w['videos'][n]={'name':n,'size_bytes':size,'sha256':digest,'sealed':False};self.save_workspace(w)
   w['bridges'].revoke_path(p)
   return {'video':w['videos'][n],**w['bridges'].create(ctx,p,'upload',digest,size)}
  if name in ('upload_video_chunk','finalize_video_upload'):
   n=a.get('name');row=w['videos'].get(n) if isinstance(n,str) else None
   if not row or row['sealed']:raise ValueError('VIDEO_NOT_PENDING')
   p=w['folder']/'inputs'/n
   w['bridges'].revoke_path(p)
   if name=='upload_video_chunk':
    raw=base64.b64decode(a['data_base64'],validate=True);offset=a['offset'];current=p.stat().st_size if p.exists() else 0
    if type(offset)!=int or offset<0 or not 0<len(raw)<=1024 or offset+len(raw)>row['size_bytes']:raise ValueError('Invalid chunk')
    if offset<current:
     with p.open('rb') as f:f.seek(offset);same=f.read(len(raw))==raw
     if not same:raise ValueError('Conflicting retry')
    elif offset==current:
     with p.open('ab') as f:f.write(raw)
    else:raise ValueError('Noncontiguous upload')
    return {'next_offset':offset+len(raw),'size':p.stat().st_size}
   if not p.is_file() or p.stat().st_size!=row['size_bytes'] or sha256(p)!=row['sha256']:raise ValueError('INCOMPLETE_OR_HASH_MISMATCH')
   row['sealed']=True;self.save_workspace(w);return row
  if name=='list_videos':return {'videos':[{**v,'uploaded_bytes':(w['folder']/'inputs'/n).stat().st_size if (w['folder']/'inputs'/n).exists() else 0} for n,v in w['videos'].items()]}
  if name=='revoke_file_link':w['bridges'].revoke(a['bridge_id']);return {'revoked':True}
  if name=='close_diffsynth_workspace':
   if any(j['state'] in ('running','queued') for j in w['jobs'].values()):raise ValueError('Cancel active jobs first')
   self.remove_workspace(w);return {'closed':True,'files_deleted':True}
  if name=='start_lora_training':
   if not (ROOT/'runtime-ready.json').is_file():raise ValueError('RUNTIME_NOT_READY')
   samples=a.get('samples');seconds=a.get('timeout_seconds',7200)
   if not isinstance(samples,list) or not 1<=len(samples)<=8:raise ValueError('Select 1..8 captioned videos')
   for row in samples:
    if not isinstance(row,dict) or set(row)!={'video','caption'}:raise ValueError('Each sample needs video and caption')
    n=row['video'];caption=row['caption']
    if not isinstance(n,str) or n not in w['videos'] or not w['videos'][n]['sealed']:raise ValueError('Video not sealed')
    if not isinstance(caption,str) or not 1<=len(caption.strip())<=1500:raise ValueError('Invalid caption')
   cfg={k:a.get(k,v) for k,v in {'steps':20,'rank':8,'learning_rate':0.0001,'height':128,'width':128,'num_frames':9,'seed':42,'preview_steps':8,'preview_prompt':'A small colorful object moves smoothly across a table.'}.items()}
   for k,lo,hi in [('steps',1,1000),('height',64,256),('width',64,256),('seed',0,2147483647),('preview_steps',4,30)]:
    if type(cfg[k])!=int or not lo<=cfg[k]<=hi:raise ValueError('Invalid '+k)
   if type(cfg['rank'])!=int or cfg['rank'] not in (4,8,16,32) or type(cfg['num_frames'])!=int or cfg['num_frames'] not in (5,9,13,17) or cfg['height']%16 or cfg['width']%16:raise ValueError('Invalid rank, frames or dimensions (multiples of16)')
   lr=cfg['learning_rate']
   if type(lr) not in (int,float) or not math.isfinite(lr) or not 1e-6<=lr<=1e-3:raise ValueError('Invalid learning rate')
   if not isinstance(cfg['preview_prompt'],str) or not 1<=len(cfg['preview_prompt'].strip())<=1500:raise ValueError('Invalid preview prompt')
   if type(seconds)!=int or not 60<=seconds<=21600:raise ValueError('Invalid timeout')
   if len(w['jobs'])>=16:raise ValueError('JOB_LIMIT')
   if len(self.pending)>=8 or any(j['quota']==w['quota'] for j in self.pending) or (self.active and self.active['quota']==w['quota']):raise ValueError('CALLER_JOB_BUSY_OR_QUEUE_FULL')
   jid=uuid.uuid4().hex;folder=w['folder']/'jobs'/jid;(folder/'inputs').mkdir(parents=True)
   for n in set(row['video'] for row in samples):os.link(w['folder']/'inputs'/n,folder/'inputs'/n)
   save(folder/'request.json',{'samples':samples,**cfg})
   j={'id':jid,'quota':w['quota'],'folder':folder,'state':'queued','created':time.time(),'timeout':seconds,'container':'nexus-diffsynth-'+jid}
   self.save_job(j);w['jobs'][jid]=j;self.pending.append(j);self.wake.set();return {'job_id':jid,'state':'queued'}
  jid=a.get('job_id');j=w['jobs'].get(jid) if isinstance(jid,str) else None
  if not j:raise ValueError('JOB_NOT_FOUND')
  if name=='cancel_job':
   if j['state']=='queued':self.pending.remove(j);j['state']='cancelled';self.save_job(j)
   elif j['state']=='running':j['forced']='cancelled'
   return {'state':j['state'],'cancellation_requested':True}
  if name=='job_status':
   out={'job_id':j['id'],'state':j['state'],'elapsed_seconds':round(max(0,j.get('ended',time.time())-j.get('started',time.time())),2)}
   log=j['folder']/'run.log'
   if log.exists():
    with log.open('rb') as f:f.seek(max(0,log.stat().st_size-10000));out['log_tail']=f.read().decode(errors='replace')
   for key in ('progress','report'):
    p=j['folder']/(key+'.json')
    if p.exists():out[key]=json.loads(p.read_text())
   return out
  if name=='list_artifacts':return {'files':[{'path':str(p.relative_to(j['folder'])),'size':p.stat().st_size} for p in j['folder'].rglob('*') if p.is_file() and not p.is_symlink() and p.name!='job.json']}
  if name in ('read_artifact','get_artifact_download'):
   path=a.get('path')
   if not isinstance(path,str) or len(path)>240:raise ValueError('Invalid path')
   p=(j['folder']/path).resolve()
   if not p.is_relative_to(j['folder']) or not p.is_file() or p.name=='job.json':raise ValueError('ARTIFACT_NOT_FOUND')
   if name=='get_artifact_download':
    if j['state'] in ('running','queued'):raise ValueError('Wait for job completion')
    return w['bridges'].create(ctx,p,'download',sha256(p),p.stat().st_size)
   offset=a.get('offset',0);size=p.stat().st_size
   if type(offset)!=int or not 0<=offset<=size:raise ValueError('Invalid offset')
   with p.open('rb') as f:f.seek(offset);raw=f.read(24576)
   out={'data_base64':base64.b64encode(raw).decode(),'next_offset':offset+len(raw),'eof':offset+len(raw)>=size,'size':size}
   if j['state'] not in ('running','queued'):out['sha256']=sha256(p)
   return out
  raise ValueError('Unknown tool')
 def remove_workspace(self,w):
  w['closed']=True;w['bridges'].close();self.save_workspace(w);self.workspaces.pop(w['id'],None)
  assert w['folder'].parent==DATA and re.fullmatch('[0-9a-f]{32}',w['folder'].name)
  shutil.rmtree(w['folder'])
 def run_queue(self):
  while not self.stop.is_set():
   self.wake.wait(2);self.wake.clear()
   with self.lock:
    for w in list(self.workspaces.values()):
     if time.time()-w['touched']>86400 and not any(j['state'] in ('running','queued') for j in w['jobs'].values()):self.remove_workspace(w)
    if not self.pending:continue
    j=self.pending.pop(0);self.active=j;j['state']='running';j['started']=time.time();self.save_job(j)
   cmd=['docker','run','--rm','--init','--name',j['container'],'--network','none','--read-only','--user',f'{os.getuid()}:{os.getgid()}',
    '--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','64g','--cpus','8','--pids-limit','512','--shm-size','1g','--tmpfs','/tmp:rw,nosuid,nodev,size=2g',
    '--device','/dev/nvidia2','--device','/dev/nvidiactl','--device','/dev/nvidia-uvm',
    '-e','CUDA_VISIBLE_DEVICES=0',
    '--mount',f'type=bind,src={ROOT / "models"},dst=/models,readonly','--mount',f'type=bind,src={j["folder"]},dst=/work']
   for lib in ('libcuda.so.1','libnvidia-ml.so.1','libnvidia-ptxjitcompiler.so.1'):cmd+=['--mount',f'type=bind,src=/usr/lib/x86_64-linux-gnu/{lib},dst=/driver/{lib},readonly']
   cmd.append(IMAGE)
   try:
    with (j['folder']/'run.log').open('wb') as f:
     p=subprocess.Popen(cmd,stdout=f,stderr=f)
     while p.poll() is None:
      if time.time()-j['started']>j['timeout']:j['forced']='timed_out'
      if self.stop.is_set():j['forced']='interrupted'
      if j.get('forced'):subprocess.run(['docker','rm','-f',j['container']],capture_output=True,timeout=30)
      try:p.wait(timeout=1)
      except subprocess.TimeoutExpired:pass
    j['state']=j.get('forced','succeeded' if p.returncode==0 else 'failed');j['exit_code']=p.returncode
    if j['state']=='succeeded' and not (j['folder']/'report.json').is_file():j['state']='failed'
   except Exception as e:j['state']='failed';j['error']=type(e).__name__
   finally:
    with self.lock:j['ended']=time.time();self.save_job(j);self.active=None
    self.wake.set()
 def close(self):
  self.stop.set();self.wake.set()
  with self.lock:
   for j in self.pending:j['state']='interrupted';self.save_job(j)
   self.pending.clear()
  self.thread.join(timeout=60)
