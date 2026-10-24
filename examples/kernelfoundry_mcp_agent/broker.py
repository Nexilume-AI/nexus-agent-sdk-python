"""One job's model/evaluation broker. No provider credentials enter any container."""
import hashlib,http.server,json,math,os,socketserver,subprocess,threading,time,uuid
from pathlib import Path
from urllib.request import Request
from urllib.error import HTTPError
from model_transport import open_model_request,validate_base_url
IMAGE='nexus-kernelfoundry-mcp:0b2338891b7b'
def docker_flags(name,memory='2g',cpus='2'):
 return ['docker','run','--rm','--init','--name',name,'--network','none','--read-only','--user',f'{os.getuid()}:{os.getgid()}','--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory',memory,'--memory-swap',memory,'--cpus',cpus,'--pids-limit','512','--ulimit','fsize=134217728:134217728','--ulimit','nofile=256:256','--tmpfs','/tmp:rw,exec,nosuid,nodev,size=2g']
class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):daemon_threads=True
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  try:
   n=int(self.headers.get('Content-Length','0'))
   if not 0<n<=4194304:raise ValueError('PAYLOAD_LIMIT')
   value=self.server.broker.handle(self.path,json.loads(self.rfile.read(n)));code=200
  except Exception as e:
   value={'error':str(e) if isinstance(e,ValueError) else type(e).__name__};code=400
  raw=json.dumps(value,allow_nan=False).encode();self.send_response(code);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
class Broker:
 def __init__(self,job,private,credentials=None,check_grant=None,emit=None,stub=False):
  self.job,self.private,self.credentials,self.check_grant,self.emit,self.stub=job,private,credentials,check_grant,emit,stub
  self.lock=threading.Lock();self.model_count=0;self.eval_count=0;self.cancel=threading.Event();self.eval_container=None
  private.mkdir(parents=True,exist_ok=True);(private/'ipc').mkdir(exist_ok=True)
  # Linux AF_UNIX paths are limited to 107 bytes. Bind through an open
  # directory descriptor so deeply nested per-job paths remain supported.
  fd=os.open(private/'ipc',os.O_RDONLY|os.O_DIRECTORY)
  try:self.server=Server(f'/proc/self/fd/{fd}/run.sock',Handler)
  finally:os.close(fd)
  self.server.broker=self
  (private/'ipc/run.sock').chmod(0o600);threading.Thread(target=self.server.serve_forever,daemon=True).start()
 def handle(self,path,a):
  with self.lock:
   if self.cancel.is_set():raise ValueError('JOB_CANCELLED')
   if path=='/model':return self.model(a)
   if path=='/evaluate':return self.evaluate(a)
   raise ValueError('UNKNOWN_OPERATION')
 def model(self,a):
  cfg=self.job['config'];msgs=a.get('messages')
  if not isinstance(msgs,list) or not 1<=len(msgs)<=30 or any(not isinstance(x,dict) or x.get('role') not in ('system','user','assistant') or not isinstance(x.get('content'),str) for x in msgs):raise ValueError('INVALID_MESSAGES')
  if len(json.dumps(msgs).encode())>196608:raise ValueError('MODEL_INPUT_LIMIT')
  if self.model_count>=cfg['iterations']+2:raise ValueError('MODEL_REQUEST_LIMIT')
  self.model_count+=1
  if self.emit:self.emit('model_request',{'request':self.model_count})
  if self.stub:return {'text':'```python\ndef solution():\n    return (3.0, -2.0)\n```'}
  grant=self.check_grant()
  if grant.get('status')!='authorized' or any(grant.get(k)!=self.credentials.get(k) for k in ('provider','model','base_url')):raise ValueError('AUTHORIZATION_EXPIRED_OR_CHANGED')
  base=self.credentials['base_url'].rstrip('/');validate_base_url(base)
  data={'model':self.credentials['model'],'messages':msgs,'max_completion_tokens':4096,'stream':False}
  request=Request(base+'/chat/completions',data=json.dumps(data).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+self.credentials['secret'],'User-Agent':'Nexus-KernelFoundry/1'})
  try:
   with open_model_request(request,timeout=180) as r:
    raw=b''
    while len(raw)<=1048576:
     part=r.read1(65536)
     if not part:break
     raw+=part
    if len(raw)>1048576:raise ValueError('MODEL_RESPONSE_LIMIT')
   data=json.loads(raw);text=data['choices'][0]['message']['content']
   if not isinstance(text,str) or not text.strip():raise ValueError('EMPTY_MODEL_RESPONSE')
   if self.emit:self.emit('model_complete',{'request':self.model_count,'usage':{k:v for k,v in data.get('usage',{}).items() if type(v)==int}})
   return {'text':text,'usage':{k:v for k,v in data.get('usage',{}).items() if type(v)==int}}
  except HTTPError as e:raise ValueError('MODEL_HTTP_'+str(e.code)) from None
 def evaluate(self,a):
  if self.eval_count>=self.job['config']['iterations']+3:raise ValueError('EVALUATION_LIMIT')
  raw=json.dumps(a).encode()
  if len(raw)>4194304:raise ValueError('TASK_LIMIT')
  self.eval_count+=1;folder=self.private/'evaluations'/str(self.eval_count);folder.mkdir(parents=True)
  inp=folder/'input';out=folder/'output';inp.mkdir();out.mkdir()
  (inp/'request.json').write_bytes(raw)
  name='nexus-kf-eval-'+self.job['id']+'-'+str(self.eval_count);self.eval_container=name
  cmd=docker_flags(name,'12g','4')+gpu_flags()+['--mount',f'type=bind,src={inp},dst=/input,readonly','--mount',f'type=bind,src={out},dst=/output','--entrypoint','python3',IMAGE,'-I','/opt/nexus/evaluate.py']
  started=time.time()
  try:
   with (folder/'run.log').open('wb') as f:
    p=subprocess.Popen(cmd,stdout=f,stderr=f)
    while p.poll() is None:
     if self.cancel.is_set() or time.time()-started>self.job['config']['evaluation_seconds'] or f.tell()>16777216:
      subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=15);raise ValueError('EVALUATION_CANCELLED_OR_LIMIT')
     time.sleep(.3)
   response=out/'response.json'
   if p.returncode or response.is_symlink() or not response.is_file() or response.stat().st_size>4194304:raise ValueError('EVALUATION_FAILED')
   answer=json.loads(response.read_text());v=answer['result']
   row={'evaluation':self.eval_count,'compiled':v['compiled'],'correctness':v['correctness'],'runtime':v['runtime'],'speedup':v['runtime_improvement'],'seconds':round(time.time()-started,3)}
   with (self.job['folder']/'evaluations.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
   if self.emit:self.emit('evaluation',row)
   return answer
  finally:self.eval_container=None
 def close(self):
  self.cancel.set()
  if self.eval_container:subprocess.run(['docker','rm','-f',self.eval_container],capture_output=True,timeout=15)
  self.server.shutdown();self.server.server_close()
  if self.credentials:self.credentials.clear()

def gpu_flags():
 # GPU0 uses the same idle admission check as Atrex; GPU15 is never selected.
 flags=['--device','/dev/nvidia0','--device','/dev/nvidiactl','--device','/dev/nvidia-uvm','--env','CUDA_VISIBLE_DEVICES=0','--env','TORCH_CUDA_ARCH_LIST=6.0']
 for name in ('libcuda.so.1','libnvidia-ml.so.1','libnvidia-ptxjitcompiler.so.1','libnvidia-gpucomp.so.580.178.04'):
  p=Path('/lib/x86_64-linux-gnu')/name
  if p.exists():flags+=['--mount',f'type=bind,src={p.resolve()},dst=/driver/{name},readonly']
 return flags
