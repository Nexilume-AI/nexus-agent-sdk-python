"""Authenticated VBench workspaces, bounded uploads and one fair GPU queue."""
import base64, copy, hashlib, hmac, json, os, re, secrets, shutil, subprocess, threading, time, uuid
from pathlib import Path
from file_bridge import Bridges, sha256, save

ROOT=Path(__file__).resolve().parent;DATA=ROOT/'data'
IMAGE='nexus-vbench-mcp:fd18b3d055cb';COMMIT='fd18b3d055cb0fc6f066ca90fe2c3c8cbb698490'
DIMENSIONS=['subject_consistency','background_consistency','temporal_flickering','motion_smoothness','dynamic_degree','aesthetic_quality','imaging_quality']
AUTH={'workspace_id':{'type':'string'},'workspace_key':{'type':'string','description':'Private capability returned by create_vbench_workspace; use only with the same authenticated caller.'}}
S={'type':'string'}
def result(v,error=False):return {'content':[{'type':'text','text':json.dumps(v)}],'structuredContent':v,'isError':error}
def tool(name,desc,props=None,required=(),auth=True):return {'name':name,'description':desc,'inputSchema':{'type':'object','properties':{**(AUTH if auth else {}),**(props or {})},'required':[* (AUTH if auth else {}),*required],'additionalProperties':False}}
CATALOG=[
 tool('get_vbench_status','Inspect installed VBench revision, tested metrics, readiness and limits. No model-provider API key required.',auth=False),
 tool('list_dimensions','List supported native VBench custom-input metrics and score limitations. This deployment does not produce official leaderboard scores.',auth=False),
 tool('vbench_tool_help','Return full instructions and JSON input schema for one tool.',{'tool_name':S},('tool_name',),auth=False),
 tool('create_vbench_workspace','Create isolated video/report storage bound to the server-verified caller. Same API key means one caller. Save workspace_id/key. Two workspaces per caller, eight total; 24-hour idle expiry. Close deletes files and revokes links.',auth=False),
 tool('create_demo_video','Copy a small bundled moving-pattern MP4 into your workspace for pipeline testing. It is synthetic and not evidence of generation quality.'),
 tool('begin_video_upload','Reserve an MP4 using exact size and SHA256; returns a one-time authenticated browser upload link with resumable chunks. Max 128 MiB/file and 8 videos/workspace. Renew using the same metadata. No arbitrary URL fetch.',{'name':S,'size_bytes':{'type':'integer','minimum':1,'maximum':134217728},'sha256':S},('name','size_bytes','sha256')),
 tool('upload_video_chunk','Programmatic MCP upload alternative: reserve with begin_video_upload, then sequential base64 chunks up to 1024 decoded bytes. Exact retries supported; never overwrite a sealed video.',{'name':S,'offset':{'type':'integer','minimum':0},'data_base64':S},('name','offset','data_base64')),
 tool('finalize_video_upload','Revoke upload links, check complete size and SHA256, and seal video for jobs. Video codec/dimensions are checked inside the evaluation sandbox.',{'name':S},('name',)),
 tool('list_videos','List only your uploaded/sealed videos and resumable byte offsets.'),
 tool('start_evaluation','Queue native VBench on 1..8 sealed MP4 files. Each video: 32..512 pixels per side, 2..360 frames, <=30 seconds. Returns job_id immediately. One global GPU job, FIFO; max one running/queued job per caller. Seven visual metrics available; temporal flickering assumes static content.',{'videos':{'type':'array','minItems':1,'maxItems':8,'items':S},'dimensions':{'type':'array','minItems':1,'uniqueItems':True,'items':{'type':'string','enum':DIMENSIONS},'default':['temporal_flickering','background_consistency']},'timeout_seconds':{'type':'integer','minimum':10,'maximum':3600,'default':900}},('videos',)),
 tool('job_status','Read job state, actual metric progress, bounded logs and completed scores.',{'job_id':S},('job_id',)),
 tool('cancel_job','Cancel your queued/running evaluation and retain partial artifacts.',{'job_id':S},('job_id',)),
 tool('list_artifacts','List your evaluation reports, CSV, inputs and upstream raw outputs.',{'job_id':S},('job_id',)),
 tool('read_artifact','Download artifact as base64 pages up to 24576 bytes. Completed files include SHA256; concatenate and verify.',{'job_id':S,'path':S,'offset':{'type':'integer','minimum':0,'default':0}},('job_id','path')),
 tool('get_artifact_download','Create one-time authenticated HTTPS download link. Redeem within 10 minutes; session lasts one hour and supports Range/ETag/SHA256. Completed jobs only.',{'job_id':S,'path':S},('job_id','path')),
 tool('revoke_file_link','Revoke one of your upload/download capabilities.',{'bridge_id':S},('bridge_id',)),
 tool('close_vbench_workspace','Revoke links and permanently remove this workspace and its videos/reports. Cancel running or queued jobs first.'),
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
   if name not in ('get_vbench_status','list_dimensions','vbench_tool_help'):
    if not isinstance(identity,dict) or identity.get('version')!=1 or identity.get('principal_type') not in ('api_key','user','service_account') or any(not isinstance(identity.get(k),str) or not re.fullmatch('[0-9a-f]{64}',identity[k]) for k in ('owner_scope','quota_scope')):raise ValueError('VERIFIED_CALLER_REQUIRED')
   with self.lock:return result(self.dispatch(name,args,identity,context))
  except (ValueError,KeyError,OSError) as e:return result({'error':'VBENCH_REQUEST_FAILED','message':str(e)[:700]},True)
 def dispatch(self,name,a,identity,ctx):
  if name=='get_vbench_status':return {'upstream':'Vchitect/VBench','commit':COMMIT,'ready':(ROOT/'runtime-ready.json').is_file(),'tools':len(CATALOG),'dimensions':DIMENSIONS,'gpu':'dedicated physical GPU 1','queue':'One active job globally; one running/queued job per caller; FIFO','limits':{'max_videos':8,'max_file_bytes':134217728,'max_frames':360,'max_dimension':512,'max_video_seconds':30,'max_job_seconds':3600},'transport':'Nexus Cloud MCP over OpenWrt Relay'}
  if name=='list_dimensions':return {'supported':DIMENSIONS,'mode':'custom_input','not_supported':['official leaderboard aggregation','VBench 2.0','audio metrics','semantic/object category metrics'],'notes':'Native upstream scores. Temporal flickering expects static scenes. Short demo scores are not a model-quality benchmark.'}
  if name=='vbench_tool_help':
   row=next((t for t in CATALOG if t['name']==a.get('tool_name')),None)
   if row is None:raise ValueError('Unknown tool')
   return row
  if name=='create_vbench_workspace':
   if len(self.workspaces)>=8 or sum(w['quota']==identity['quota_scope'] for w in self.workspaces.values())>=2:raise ValueError('WORKSPACE_LIMIT; close an unused workspace')
   if shutil.disk_usage(DATA).free<5*1024**3:raise ValueError('INSUFFICIENT_STORAGE')
   wid=uuid.uuid4().hex;key=secrets.token_urlsafe(32);folder=DATA/wid;folder.mkdir(mode=0o700);(folder/'inputs').mkdir()
   w={'id':wid,'folder':folder,'owner':identity['owner_scope'],'quota':identity['quota_scope'],'key_hash':hashlib.sha256(key.encode()).hexdigest(),'closed':False,'videos':{},'jobs':{},'touched':time.time()}
   w['bridges']=Bridges(w);self.workspaces[wid]=w;self.save_workspace(w)
   return {'workspace_id':wid,'workspace_key':key,'idle_seconds':86400,'next_step':'begin_video_upload or create_demo_video, then start_evaluation'}
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
  if name=='close_vbench_workspace':
   if any(j['state'] in ('running','queued') for j in w['jobs'].values()):raise ValueError('Cancel active jobs first')
   self.remove_workspace(w);return {'closed':True,'files_deleted':True}
  if name=='start_evaluation':
   if not (ROOT/'runtime-ready.json').is_file():raise ValueError('RUNTIME_NOT_READY')
   names=a.get('videos');dims=a.get('dimensions',['temporal_flickering','background_consistency']);seconds=a.get('timeout_seconds',900)
   if not isinstance(names,list) or not 1<=len(names)<=8 or any(not isinstance(n,str) or n not in w['videos'] or not w['videos'][n]['sealed'] for n in names) or len(set(names))!=len(names):raise ValueError('Select 1..8 distinct sealed videos')
   if not isinstance(dims,list) or not dims or len(dims)>7 or any(d not in DIMENSIONS for d in dims) or len(set(dims))!=len(dims):raise ValueError('Invalid dimensions')
   if type(seconds)!=int or not 10<=seconds<=3600:raise ValueError('Invalid timeout')
   if len(w['jobs'])>=16:raise ValueError('JOB_LIMIT')
   if len(self.pending)>=8 or any(j['quota']==w['quota'] for j in self.pending) or (self.active and self.active['quota']==w['quota']):raise ValueError('CALLER_JOB_BUSY_OR_QUEUE_FULL')
   jid=uuid.uuid4().hex;folder=w['folder']/'jobs'/jid;(folder/'inputs').mkdir(parents=True)
   for n in names:os.link(w['folder']/'inputs'/n,folder/'inputs'/n)
   save(folder/'request.json',{'videos':[{'name':n} for n in names],'dimensions':dims})
   j={'id':jid,'quota':w['quota'],'folder':folder,'state':'queued','created':time.time(),'timeout':seconds,'container':'nexus-vbench-'+jid}
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
    '--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','24g','--cpus','4','--pids-limit','128','--shm-size','1g','--tmpfs','/tmp:rw,nosuid,nodev,size=2g',
    '--device','/dev/nvidia1','--device','/dev/nvidiactl','--device','/dev/nvidia-uvm',
    '-e','CUDA_VISIBLE_DEVICES=0','-e','TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1',
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
