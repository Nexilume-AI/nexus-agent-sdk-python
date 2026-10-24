"""Caller-scoped Pi sessions. Trusted model controller and offline tool sandbox."""
import base64,hashlib,http.server,json,os,queue,re,shutil,socketserver,subprocess,threading,time,uuid
from pathlib import Path
from urllib.request import Request
from urllib.error import HTTPError
from model_transport import open_model_request,validate_base_url
from nexus_agent.credentials import ProviderCredentials
import computer_tools
ROOT=Path(__file__).resolve().parent
STATE=ROOT/'state';IMAGE='nexus-pi-chat:1.4.0-computer1'

def flags(name,memory='1g'):
 return ['docker','run','--rm','--init','--name',name,'--label','nexus.pi=1','--network','none','--read-only','--user',f'{os.getuid()}:{os.getgid()}','--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory',memory,'--memory-swap',memory,'--cpus','2','--pids-limit','128','--ulimit','fsize=67108864:67108864','--tmpfs','/tmp:rw,nosuid,nodev,size=128m','--workdir','/workspace']

def stop(name):
 subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=20)

def maintenance_restore(path):
 # Only an administrator-created, short-lived snapshot survives a deployment.
 try:
  value=json.loads((path/'.maintenance-restore.json').read_text())
  if 0 < float(value['expires_at'])-time.time() <= 3600 and (path/'.workspace-restore').is_dir():return value
 except (OSError,ValueError,KeyError,TypeError):pass
 return None

def check_control(ctx):
 # Relay can have task cancellation without automatic-recovery protocol v1.
 state=ctx.control()
 if state.get('cancel_requested') or state.get('state') in ('cancel_requested','cancelled') or state.get('remaining_seconds')==0:
  raise ValueError('RUN_CANCELLED')
 ctx.raise_if_cancelled()

class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  started=False
  try:
   n=int(self.headers.get('Content-Length','0'))
   if not 0<n<=524288:raise ValueError('REQUEST_LIMIT')
   value=json.loads(self.rfile.read(n));broker=self.server.broker
   if self.path=='/v1/chat/completions':
    response=broker.model(value)
    with response:
     self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers();started=True
     size=0
     while not broker.cancel.is_set():
      line=response.readline(65537)
      if not line:break
      size+=len(line)
      if size>4194304 or len(line)>65536:raise ValueError('MODEL_RESPONSE_LIMIT')
      self.wfile.write(line);self.wfile.flush()
    return
   if self.path=='/tool':data=broker.tool(value)
   elif self.path=='/publish':data=broker.publish(value)
   else:raise ValueError('UNKNOWN_OPERATION')
   raw=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
  except Exception as e:
   if isinstance(e,(BrokenPipeError,ConnectionResetError)):return
   code=str(e) if isinstance(e,ValueError) and re.fullmatch('[A-Z0-9_]+',str(e)) else type(e).__name__
   print('Pi broker failed',code,flush=True)
   if started:return
   code=str(e) if isinstance(e,ValueError) and re.fullmatch('[A-Z0-9_]+',str(e)) else 'PI_BROKER_REQUEST_FAILED'
   raw=json.dumps({'error':code}).encode()
   try:self.send_response(400);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
   except (BrokenPipeError,ConnectionResetError):pass

class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):daemon_threads=True
class Broker:
 def __init__(self,entry,ctx,grant,aid,cancel):
  self.entry,self.ctx,self.grant,self.aid,self.cancel=entry,ctx,grant,aid,cancel
  self.calls=0;self.tools=0;self.lock=threading.Lock();self.files=[]
  folder=entry['path']/'ipc';folder.mkdir(exist_ok=True)
  sock=folder/'run.sock'
  if sock.exists():sock.unlink()
  fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY)
  try:self.server=Server(f'/proc/self/fd/{fd}/run.sock',Handler)
  finally:os.close(fd)
  sock.chmod(0o600);self.server.broker=self
  threading.Thread(target=self.server.serve_forever,daemon=True).start()
 def model(self,value):
  check_control(self.ctx)
  with self.lock:
   if self.cancel.is_set():raise ValueError('CANCELLED')
   if self.calls>=12:raise ValueError('MODEL_CALL_LIMIT')
   self.calls+=1
  latest=ProviderCredentials(self.ctx).status(self.aid)
  if latest.get('status')!='authorized' or any(latest.get(k)!=self.grant.get(k) for k in ('provider','model','base_url')):raise ValueError('AUTHORIZATION_EXPIRED_OR_CHANGED')
  if not isinstance(value.get('messages'),list) or len(value['messages'])>150:raise ValueError('CONTEXT_LIMIT')
  payload={k:value[k] for k in ('messages','tools','tool_choice','temperature','top_p','parallel_tool_calls') if k in value}
  payload.update(model=self.grant['model'],stream=True,max_completion_tokens=4096,stream_options={'include_usage':True})
  base=self.grant['base_url'].rstrip('/');validate_base_url(base)
  req=Request(base+'/chat/completions',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+self.grant['secret'],'User-Agent':'Nexus-Pi/1'})
  try:return open_model_request(req,timeout=180)
  except HTTPError as e:raise ValueError('MODEL_HTTP_'+str(e.code)) from None
 def tool(self,value):
  check_control(self.ctx)
  if value.get('name') not in ('read','bash','write','edit',*computer_tools.SCOPES):raise ValueError('UNKNOWN_TOOL')
  with self.lock:
   if self.tools>=30 or self.cancel.is_set():raise ValueError('TOOL_CALL_LIMIT')
   self.tools+=1
  if value['name'] in computer_tools.SCOPES:
   return computer_tools.invoke(self.ctx,value['name'],value.get('args',{}))
  return self.execute(value)
 def execute(self,value,limit=2097152):
  # The child can execute user code but sees no broker, credentials or host mounts.
  p=subprocess.Popen(['docker','exec','-i',self.entry['sandbox'],'node','/opt/pi/tools.mjs'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
  try:
   out,_=p.communicate(json.dumps(value).encode(),timeout=75)
   if p.returncode or len(out)>limit:raise ValueError('SANDBOX_TOOL_FAILED')
   return json.loads(out)
  except subprocess.TimeoutExpired:
   p.kill();p.communicate();stop(self.entry['sandbox']);raise ValueError('TOOL_TIMEOUT')
 def publish(self,value):
  check_control(self.ctx)
  if len(self.files)>=8:raise ValueError('ARTIFACT_LIMIT')
  data=self.execute({'name':'export','args':value},12000000)
  content=base64.b64decode(data['data'],validate=True)
  name=data['name']
  if not re.fullmatch(r'[\w. -]{1,128}',name) or len(content)>8388608:raise ValueError('INVALID_EXPORT')
  folder=self.entry['path']/('export-'+uuid.uuid4().hex);folder.mkdir(mode=0o700)
  file=folder/name;file.write_bytes(content)
  try:result=self.ctx.output.upload_file(file)
  finally:shutil.rmtree(folder)
  self.files.append({'name':name,'sha256':hashlib.sha256(content).hexdigest(),'size':len(content)})
  return {'content':[{'type':'text','text':'Published '+name+' to Private Display Files.'}],'details':{'file':result}}
 def close(self):
  self.cancel.set();self.server.shutdown();self.server.server_close();self.grant.clear()

class Runtime:
 def __init__(self):
  STATE.mkdir(mode=0o700,exist_ok=True);self.cv=threading.Condition();self.entries={};self.authorizations={};self.waiting=[];self.active=set();self.closed=threading.Event()
  threading.Thread(target=self.cleanup,daemon=True).start()
 def cleanup(self):
  while not self.closed.wait(60):
   with self.cv:
    for key,e in list(self.entries.items()):
     if not e['busy'] and time.time()-e['touched']>3600:
      stop(e['sandbox']);shutil.rmtree(e['path']);del self.entries[key]
 def close(self):
  self.closed.set()
  with self.cv:
   for e in self.entries.values():stop(e['sandbox'])
 def chat(self,a,ctx):
  identity=ctx.caller_identity();owner=identity['owner_scope'];quota=identity['quota_scope'];rid=str(uuid.UUID(str(ctx.run_id)))
  message=a.get('message')
  if not isinstance(message,str) or not 1<=len(message)<=16000:raise ValueError('INVALID_MESSAGE')
  key=hashlib.sha256((owner+rid).encode()).hexdigest()[:32]
  c=ProviderCredentials(ctx)
  aid=a.get('authorization_id') or self.authorizations.get(key)
  # Cloud reuses only this caller's active Pi grant, including a selected custom
  # OpenAI endpoint. Every resolution still checks scope, expiry and revocation.
  auth=c.status(aid) if aid else c.authorize('nexus',0,reuse_existing=True)
  # Preserve the approval reference when the user changes model/provider on the
  # web page. Every use is still revalidated by Cloud against this caller/Run.
  if auth.get('authorization_id'):
   if len(self.authorizations)>=1024 and key not in self.authorizations:self.authorizations.pop(next(iter(self.authorizations)))
   self.authorizations[key]=auth['authorization_id']
  if auth.get('status')!='authorized':
   url=auth.get('authorization_url') or auth.get('url')
   # Cloud issues a per-caller, short-lived approval link; never log it.
   ctx.chat.say('Choose and authorize your own model API for this conversation. Select your Nexus Router or enter an OpenAI-compatible endpoint, model and API key on the authorization page. Pi will start only after you confirm. Do not paste API keys into chat.'+(f' [Authorize model API]({url})' if url else '')+' After approval, send your message again.')
   return {'status':'authorization_required','authorization':auth}
  with self.cv:
   if quota in self.active or any(q==quota for _,q in self.waiting):raise ValueError('CALLER_ALREADY_RUNNING')
   if len(self.waiting)>=8:raise ValueError('QUEUE_FULL')
   self.waiting.append((key,quota))
   try:
    deadline=time.monotonic()+300
    while self.waiting[0][0]!=key or len(self.active)>=2:
     check_control(ctx)
     if time.monotonic()>deadline:raise ValueError('QUEUE_TIMEOUT')
     self.cv.wait(1)
    self.waiting.pop(0);self.active.add(quota)
   except BaseException:
    self.waiting=[x for x in self.waiting if x[0]!=key];self.cv.notify_all();raise
  e=None
  try:
   grant=c.resolve_for_runtime(auth['authorization_id'])
   if grant.get('provider') not in ('nexus','openai','custom_openai'):grant.clear();raise ValueError('OPENAI_COMPATIBLE_MODEL_REQUIRED')
   with self.cv:
    e=self.entries.get(key)
    if e is None:
     if len(self.entries)>=8 or sum(v['owner']==owner for v in self.entries.values())>=2:raise ValueError('SESSION_LIMIT')
     path=STATE/key
     restore=maintenance_restore(path)
     if path.exists() and not restore:shutil.rmtree(path)
     path.mkdir(mode=0o700,exist_ok=True)
     e={'path':path,'sandbox':'nexus-pi-work-'+key,'owner':owner,'busy':True,'touched':time.time(),'turns':int(restore.get('turns',0)) if restore else 0}
     stop(e['sandbox'])
     cmd=flags(e['sandbox'])+['-d','--tmpfs',f'/workspace:rw,exec,nosuid,nodev,size=128m,uid={os.getuid()},gid={os.getgid()},mode=700','--entrypoint','sleep',IMAGE,'86400']
     subprocess.run(cmd,check=True,capture_output=True,timeout=30);self.entries[key]=e
     if restore:
      subprocess.run(['docker','cp',str(path/'.workspace-restore')+'/.',e['sandbox']+':/workspace/'],check=True,capture_output=True,timeout=30)
      shutil.rmtree(path/'.workspace-restore');(path/'.maintenance-restore.json').unlink()
    e['busy']=True;e['touched']=time.time()
   if e['turns']>=40:raise ValueError('CONVERSATION_LIMIT_START_NEW_RUN')
   return self.run_turn(e,ctx,message,grant,auth['authorization_id'])
  finally:
   if 'grant' in locals():grant.clear()
   with self.cv:
    if e:e['busy']=False;e['touched']=time.time()
    if e and e.get('invalid'):
     self.entries.pop(key,None);shutil.rmtree(e['path'],ignore_errors=True)
    self.active.discard(quota);self.cv.notify_all()
 def run_turn(self,e,ctx,message,grant,aid):
  path=e['path'];(path/'request.json').write_text(json.dumps({'message':message,'model':grant['model'],'computer_tools':computer_tools.descriptors(ctx)}),encoding='utf-8')
  cancel=threading.Event();broker=Broker(e,ctx,grant,aid,cancel);name='nexus-pi-engine-'+e['path'].name
  ctx.display.title('Pi Coding Agent')
  command=flags(name,'2g')+['--mount',f'type=bind,src={path},dst=/state','--mount',f'type=bind,src={path/"ipc"},dst=/broker,readonly',IMAGE]
  process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
  events=queue.Queue();errors=[]
  def read():
   for line in process.stdout:
    if len(line)>1048576:continue
    try:events.put(json.loads(line))
    except ValueError:pass
   events.put(None)
  def stderr():
   for line in process.stderr:
    if sum(len(s) for s in errors)<12000:errors.append(line)
  threading.Thread(target=read,daemon=True).start();threading.Thread(target=stderr,daemon=True).start()
  begun=False;mid='pi-'+uuid.uuid4().hex;buffer='';last=time.monotonic();done=None;error=None;started=last
  def flush():
   nonlocal buffer,last,begun
   if buffer:
    if not begun:ctx.emit({'type':'TEXT_MESSAGE_START','messageId':mid,'role':'assistant'});begun=True
    for offset in range(0,len(buffer),2000):ctx.emit({'type':'TEXT_MESSAGE_CONTENT','messageId':mid,'delta':buffer[offset:offset+2000]})
    buffer='';last=time.monotonic()
  try:
   while True:
    check_control(ctx)
    if time.monotonic()-started>600:raise ValueError('TURN_TIMEOUT')
    try:event=events.get(timeout=.5)
    except queue.Empty:
     if buffer and time.monotonic()-last>.2:flush()
     continue
    if event is None:break
    kind=event.get('kind')
    if kind=='delta':buffer+=event['text']
    elif kind in ('tool_start','tool_end'):
     flush();ctx.shell.write(('Running ' if kind=='tool_start' else 'Finished ')+event['name'],stream='system')
    elif kind=='complete':done=event
    elif kind=='error':error=event.get('message','PI_FAILED')
    if (buffer and not begun) or len(buffer)>2000 or time.monotonic()-last>.2:flush()
   flush();process.wait(timeout=10)
   if process.returncode or not done:
    # Private local diagnostics contain no provider secrets or capability links.
    # Keep only error classes in service logs; conversation files remain ephemeral.
    print('Pi engine failed',process.returncode,'broker calls',broker.calls,'tool calls',broker.tools,flush=True)
    raise ValueError('PI_EXECUTION_FAILED')
   e['turns']+=1
   return {'status':'completed','reply':done.get('text',''),'tools_executed':broker.tools,'model_requests':broker.calls,'files':broker.files,'turns':e['turns']}
  finally:
   cancel.set()
   if process.poll() is None or not done:
    e['invalid']=True
    stop(name);stop(e['sandbox'])
   broker.close()
   if begun:
    try:ctx.emit({'type':'TEXT_MESSAGE_END','messageId':mid})
    except Exception:pass
