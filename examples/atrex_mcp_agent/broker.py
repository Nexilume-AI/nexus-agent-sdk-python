"""Host-only execution broker. Private sockets; no arbitrary Docker API or commands.

Provider secrets live only in this process during the authorized job. Each model
request rechecks the Cloud grant; revocation or Cloud failure stops access. Worker
containers have no network, Docker socket, host home, or actual provider secrets.
"""
import base64
from datetime import datetime
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import signal
import socketserver
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid
from model_transport import open_model_request, validate_base_url

TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted', 'authorization_expired'}
PROVIDERS = {'openai': 'https://api.openai.com/v1', 'anthropic': 'https://api.anthropic.com', 'nexus': 'https://cloud.nexilume.com/api/v1/openai/v1', 'custom_openai':None, 'custom_anthropic':None}
MODEL_PATHS = {'/v1/responses', '/v1/chat/completions', '/v1/messages'}
BODY_LIMIT = 8 * 1024 * 1024
ARTIFACT_LIMIT = 64 * 1024 * 1024


class Failure(Exception): pass


def check_client_tools(tools, depth=0):
    if not isinstance(tools,list) or depth>2 or len(tools)>256:
        raise Failure('REMOTE_HOSTED_TOOL_NOT_ALLOWED')
    for tool in tools:
        if not isinstance(tool,dict) or tool.get('type') not in ('function','custom','namespace'):
            raise Failure('REMOTE_HOSTED_TOOL_NOT_ALLOWED')
        if tool['type']=='namespace':check_client_tools(tool.get('tools'),depth+1)


def bounded_int(value, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        raise Failure('INVALID_ARGUMENT')
    return value


def atomic(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=True), encoding='utf-8')
    temporary.chmod(0o600)
    os.replace(temporary, path)


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024): result.update(chunk)
    return result.hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Failure('UPSTREAM_REDIRECT_REJECTED')


class Broker:
    def __init__(self, root, image, projects, gpu=0):
        self.root, self.projects = Path(root).resolve(), Path(projects).resolve()
        self.image, self.gpu = image, bounded_int(gpu, 0, 31)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.live = {}
        self.journals = {}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.model_transport = open_model_request
        for path in self.root.glob('*/job.json'):
            record = json.loads(path.read_text())
            if record['status'] not in TERMINAL:
                self.docker('rm', '-f', self.container(record['job_id']), check=False)
                record.update(status='interrupted', finished_at=time.time(), error='BROKER_RESTARTED_REAUTHORIZE_AND_RETRY')
                atomic(path, record)
            from job_events import Journal, Delivery
            journal=Journal(path.parent);self.journals[record['job_id']]=journal
            if record['status']=='interrupted' and not journal.terminal:journal.emit('state',record)
            Delivery(journal,threading.Event()).start()

    @staticmethod
    def container(job_id): return 'nexus-atrex-job-' + job_id

    @staticmethod
    def docker(*args, check=True):
        result = subprocess.run(['docker', *args], capture_output=True, timeout=40)
        if check and result.returncode:
            raise Failure('CONTAINER_OPERATION_FAILED')
        return result.stdout + result.stderr if args and args[0] == 'logs' else result.stdout

    def health(self):
        image_id = self.docker('image', 'inspect', '--format', '{{.Id}}', self.image, check=False).decode().strip()
        available = bool(image_id)
        probe = self.root / 'gpu-health.json'
        gpu = json.loads(probe.read_text()) if probe.exists() else {}
        ready = available and gpu.get('cuda_test_passed') is True and gpu.get('image_id') == image_id
        return {'execution_enabled': ready, 'model_backend': 'caller_authorized',
                'gpu_runner': 'offline_docker', 'supported_frameworks': ['CUDA'],
                'supported_execution_layouts': ['atrex-bench'], 'hardware_counter_profiling': False,
                'gpu': gpu, 'max_concurrent_jobs': 1, 'max_duration_seconds': 3300,
                'blockers': [] if ready else ['GPU_PREFLIGHT_REQUIRED']}

    def grant_status(self, credentials):
        url = credentials['status_url']
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'cloud.nexilume.com' or parsed.query or parsed.fragment or not re.fullmatch(
                r'/api/v1/internal/agent-runs/[0-9a-f-]{36}/interactions/credentials/[0-9a-f-]{36}/', parsed.path):
            raise Failure('INVALID_AUTHORIZATION_CALLBACK')
        req = urllib.request.Request(url, headers={'X-Nexus-Interaction-Token': credentials['interaction_token'], 'User-Agent':'Nexus-Atrex-Runner/0.3'})
        with self.opener.open(req, timeout=15) as response:
            value = json.loads(response.read(65536))
        if 'data' in value: value = value['data']
        if (value.get('status') != 'authorized' or value.get('provider') != credentials['provider']
                or value.get('base_url') != credentials['base_url'] or value.get('model','') != credentials.get('model','')):
            raise Failure('AUTHORIZATION_NOT_ACTIVE')
        return value

    def project(self, args):
        pid, key = args.get('project_id', ''), args.get('access_key', '')
        if not isinstance(pid, str) or not re.fullmatch('[0-9a-f]{32}', pid) or not isinstance(key, str):
            raise Failure('PROJECT_NOT_FOUND')
        directory = self.projects / pid
        try:
            record = json.loads((directory / 'project.json').read_text())
            if not hmac.compare_digest(record['access_hash'], hashlib.sha256(key.encode()).hexdigest()): raise ValueError()
        except (ValueError, OSError): raise Failure('PROJECT_NOT_FOUND') from None
        return directory

    def record(self, args):
        self.project(args)
        jid = args.get('job_id', '')
        if not isinstance(jid, str) or not re.fullmatch('[0-9a-f]{32}', jid): raise Failure('JOB_NOT_FOUND')
        try: value = json.loads((self.root / jid / 'job.json').read_text())
        except OSError: raise Failure('JOB_NOT_FOUND') from None
        if value['project_id'] != args['project_id']: raise Failure('JOB_NOT_FOUND')
        return value

    def submit(self, args):
        project = self.project(args)
        if not all((project/'files'/name).is_file() for name in ('reference.py','input.py','shapes.json')):
            raise Failure('NATIVE_ATREX_BENCH_INPUT_REQUIRED')
        request_id = str(uuid.UUID(args.get('request_id', '')))
        credentials = args['credentials']
        provider = args.get('provider', 'nexus')
        if provider not in PROVIDERS or credentials['provider'] != provider or (PROVIDERS[provider] and credentials['base_url'] != PROVIDERS[provider]):
            raise Failure('INVALID_PROVIDER')
        try:validate_base_url(credentials['base_url'])
        except ValueError:raise Failure('INVALID_MODEL_ENDPOINT') from None
        if args.get('base_url') is not None and args['base_url'].rstrip('/')!=credentials['base_url']:
            raise Failure('ENDPOINT_NOT_AUTHORIZED')
        model = args.get('model', '')
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}', model): raise Failure('INVALID_MODEL')
        if credentials.get('model') and credentials['model']!=model:raise Failure('MODEL_NOT_AUTHORIZED')
        if provider.startswith('custom_') and not credentials.get('model'):raise Failure('MODEL_NOT_AUTHORIZED')
        if args.get('framework', 'CUDA') != 'CUDA': raise Failure('FRAMEWORK_UNSUPPORTED_ON_GPU')
        spec = {'provider': provider, 'base_url':credentials['base_url'], 'protocol':'anthropic' if provider in ('anthropic','custom_anthropic') else 'openai', 'model': model, 'framework': 'CUDA', 'platform': 'NVIDIA-Pascal',
                'max_iters': bounded_int(args.get('max_iters', 1), 1, 20),
                'token_budget': bounded_int(args.get('token_budget', 50000), 1000, 2000000),
                'timeout_seconds': bounded_int(args.get('timeout_seconds', 1800), 60, 3300)}
        spec['max_model_requests'] = max(80, min(320, (spec['token_budget'] + 6249) // 6250))
        grant = self.grant_status(credentials)
        authorization_id = str(uuid.UUID(args['authorization_id']))
        if grant['authorization_id'] != authorization_id: raise Failure('AUTHORIZATION_MISMATCH')
        deadline = min(time.time()+spec['timeout_seconds'], datetime.fromisoformat(grant['expires_at'].replace('Z','+00:00')).timestamp())
        if deadline < time.time()+30: raise Failure('AUTHORIZATION_EXPIRES_TOO_SOON')
        with self.lock:
            for path in self.root.glob('*/job.json'):
                record = json.loads(path.read_text())
                if record['project_id'] == args['project_id'] and record['request_id'] == request_id:
                    if record['authorization_id'] != authorization_id or record['spec'] != spec: raise Failure('IDEMPOTENCY_CONFLICT')
                    return record
            if self.live: raise Failure('GPU_BUSY_RETRY_LATER')
            if not self.health()['execution_enabled']: raise Failure('GPU_PREFLIGHT_REQUIRED')
            if os.name == 'posix':
                probe = subprocess.run(['nvidia-smi','-i',str(self.gpu),'--query-gpu=memory.used,utilization.gpu','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=10)
                if probe.returncode: raise Failure('GPU_PROBE_FAILED')
                memory,utilization = (int(part.strip()) for part in probe.stdout.strip().split(','))
                if memory>128 or utilization>5: raise Failure('GPU_BUSY_RETRY_LATER')
            if len(list(self.root.glob('*/job.json'))) >= 128: raise Failure('JOB_STORAGE_QUOTA')
            jid = uuid.uuid4().hex
            directory = self.root / jid
            (directory / 'input/files').mkdir(parents=True)
            (directory / 'input').chmod(0o755)
            total = 0
            for path in (project/'files').iterdir():
                if path.is_symlink() or not path.is_file(): raise Failure('INVALID_INPUT_FILE')
                data = path.read_bytes(); total += len(data)
                if total > 2*1024*1024: raise Failure('INPUT_TOO_LARGE')
                (directory/'input/files'/path.name).write_bytes(data)
            atomic(directory/'input/spec.json', spec)
            # The unprivileged worker reads only this immutable snapshot, never project metadata.
            for path in (directory/'input').rglob('*'):
                path.chmod(0o755 if path.is_dir() else 0o644)
            record = {'job_id':jid, 'project_id':args['project_id'], 'request_id':request_id,
                      'authorization_id':authorization_id, 'status':'queued', 'created_at':time.time(),
                      'spec':spec, 'model_requests':0, 'artifacts':[]}
            from job_events import Journal, Delivery
            journal=Journal(directory,args.get('event_delivery'));self.journals[jid]=journal
            if journal.config:record['view_url']=journal.config['view_url']
            atomic(directory/'job.json', record)
            journal.emit('state',record)
            self.live[jid] = {'credentials':credentials, 'capability':secrets.token_urlsafe(32),
                              'deadline':deadline, 'cancel':threading.Event(), 'model_lock':threading.Lock()}
            Delivery(journal,self.live[jid]['cancel']).start()
            threading.Thread(target=self.run, args=(jid,), daemon=True).start()
            return record

    def update(self, jid, **changes):
        with self.lock:
            path = self.root/jid/'job.json'
            value = json.loads(path.read_text()); value.update(changes); atomic(path,value)
            if jid in self.journals:self.journals[jid].emit('state',changes)
            return value

    def follow_logs(self,jid,process):
        """Docker's follow pipe blocks until output arrives; no docker-logs polling."""
        import codecs
        decoder=codecs.getincrementaldecoder('utf-8')(errors='replace')
        live=self.live[jid];pending=''
        stage_buffer=''
        def emit_text(text):
            nonlocal stage_buffer
            self.journals[jid].emit('log',{'text':text})
            stage_buffer+=text
            while '\n' in stage_buffer:
                line,stage_buffer=stage_buffer.split('\n',1)
                try:value=json.loads(line)
                except ValueError:continue
                if isinstance(value,dict) and value.get('event') in ('reference_measurement_started','reference_measurement_completed','optimizer_started'):
                    self.journals[jid].emit('stage',{'name':value['event']})
            stage_buffer=stage_buffer[-16384:]
        protected=[live['capability'],live['credentials'].get('secret',''),live['credentials'].get('interaction_token','')]
        try:
            while chunk:=process.stdout.read1(4096):
                pending+=decoder.decode(chunk)
                for secret in protected:
                    if secret:pending=pending.replace(secret,'[redacted]')
                # Retain only a suffix that could be an incomplete secret.
                keep=max((n for secret in protected for n in range(1,min(len(secret),len(pending)+1)) if pending.endswith(secret[:n])),default=0)
                cut=len(pending)-keep
                text,pending=pending[:cut],pending[cut:]
                if text:emit_text(text)
            pending+=decoder.decode(b'',final=True)
            for secret in protected:
                if secret:pending=pending.replace(secret,'[redacted]')
            if pending:emit_text(pending)
        except (OSError,ValueError):pass

    def run(self, jid):
        live = self.live[jid]
        directory = self.root/jid
        name = self.container(jid)
        log_process=None;log_thread=None
        try:
            args = ['run','-d','--name',name,'--label','nexus.atrex.job='+jid,
                '--network','none','--read-only','--user','1006:1008','--cap-drop','ALL',
                '--security-opt','no-new-privileges','--pids-limit','512','--memory','12g','--cpus','4',
                '--ulimit','fsize=67108864:67108864','--shm-size','512m',
                '--tmpfs','/tmp:rw,exec,nosuid,nodev,size=1g,mode=1777',
                '--tmpfs','/work:rw,exec,nosuid,nodev,size=8g,uid=1006,gid=1008,mode=0700',
                '--mount','type=bind,src='+str(directory/'input')+',dst=/input,readonly',
                '--mount','type=bind,src='+str(self.root/'proxy')+',dst=/model,readonly',
                '--device',f'/dev/nvidia{self.gpu}', '--device','/dev/nvidiactl','--device','/dev/nvidia-uvm',
                '--env','CUDA_VISIBLE_DEVICES=0','--env','ATREX_JOB_CAPABILITY='+live['capability'],
                '--log-driver','json-file','--log-opt','max-size=16m','--log-opt','max-file=1']
            for library in ('libcuda.so.1','libnvidia-ml.so.1','libnvidia-ptxjitcompiler.so.1','libnvidia-gpucomp.so.580.178.04'):
                path = Path('/lib/x86_64-linux-gnu')/library
                if path.exists(): args += ['--mount',f'type=bind,src={path.resolve()},dst=/driver/{library},readonly']
            args += ['--mount','type=bind,src=/usr/bin/nvidia-smi,dst=/usr/bin/nvidia-smi,readonly',self.image]
            self.docker(*args)
            self.update(jid,status='running',started_at=time.time())
            log_process=subprocess.Popen(['docker','logs','--follow',name],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            log_thread=threading.Thread(target=self.follow_logs,args=(jid,log_process),daemon=True);log_thread.start()
            while True:
                state = json.loads(self.docker('inspect','--format','{{json .State}}',name))
                if not state['Running']:
                    terminal = 'succeeded' if state['ExitCode']==0 else 'failed'
                    self.update(jid,exit_code=state['ExitCode']); break
                completed = self.docker('exec',name,'cat','/work/finished.json',check=False)
                if completed:
                    code=json.loads(completed)['exit_code']
                    terminal='succeeded' if code==0 else 'failed'
                    self.update(jid,exit_code=code);break
                if live['cancel'].is_set(): terminal='cancelled';break
                if time.time() >= live['deadline']: terminal='timed_out';break
                try: self.grant_status(live['credentials'])
                except Exception: terminal='authorization_expired'; break
                live['cancel'].wait(5)
            live['cancel'].set()
            self.update(jid,status='collecting')
            log = self.docker('logs',name,check=False).replace(live['capability'].encode(),b'[redacted]')
            (directory/'optimizer.log').write_bytes(log[-16*1024*1024:])
            try:
                artifacts = self.collect(jid, live['capability'])
            except (OSError, tarfile.TarError):
                artifacts = []
                self.update(jid, artifact_collection_error='OUTPUT_UNAVAILABLE_AFTER_CONTAINER_EXIT')
            artifacts.append({'path':'optimizer.log','bytes':(directory/'optimizer.log').stat().st_size,'sha256':digest(directory/'optimizer.log')})
            log_process.terminate();log_thread.join(timeout=5)
            self.update(jid,status=terminal,finished_at=time.time(),artifacts=artifacts)
        except Exception as exc:
            self.update(jid,status='failed',finished_at=time.time(),error=str(exc) if isinstance(exc,Failure) else 'EXECUTION_INTERNAL_ERROR')
        finally:
            if log_process:
                if log_process.poll() is None:log_process.kill()
                log_process.wait(timeout=5)
                if log_thread:log_thread.join(timeout=5)
                log_process.stdout.close()
            try:
                self.docker('rm','-f',name,check=False)
            finally:
                with self.lock: self.live.pop(jid,None)
                live['credentials'].clear()

    def collect(self,jid,capability):
        directory=self.root/jid
        # docker cp cannot reliably read tmpfs mounts. Freeze other processes from
        # inside the private PID namespace, then stream tar from that namespace.
        exporter = "import os,signal; me=os.getpid(); " + "\nfor entry in os.listdir('/proc'):\n if entry.isdigit() and int(entry)!=me:\n  try: os.kill(int(entry),signal.SIGSTOP)\n  except (ProcessLookupError,PermissionError): pass\nos.execlp('tar','tar','--one-file-system','-C','/work/output','-cf','-','.')"
        process=subprocess.Popen(['docker','exec',self.container(jid),'python3','-c',exporter],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        artifacts=[]; total=0; walked=0; count=0
        try:
            with tarfile.open(fileobj=process.stdout,mode='r|*') as archive:
                for member in archive:
                    count+=1;walked+=member.size
                    if count>4096 or walked>128*1024*1024:break
                    parts=PurePosixPath(member.name).parts
                    if not member.isfile() or any(p.startswith('.') or p=='..' for p in parts) or PurePosixPath(member.name).is_absolute() or '\\' in member.name or any(ord(c)<32 for c in member.name): continue
                    saved_log=directory/'optimizer.log'
                    log_bytes=saved_log.stat().st_size if saved_log.exists() else 0
                    if len(artifacts)>=255 or member.size>16*1024*1024 or total+member.size>ARTIFACT_LIMIT-log_bytes: continue
                    data=archive.extractfile(member).read().replace(capability.encode(),b'[redacted]')
                    target=directory/'artifacts'/Path(*parts)
                    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
                    total+=len(data)
                    artifacts.append({'path':'artifacts/'+PurePosixPath(*parts).as_posix(),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        finally:
            process.stdout.close()
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired: process.kill();process.wait()
        return artifacts

    def control(self,operation,args):
        if operation=='health': return self.health()
        if operation=='submit': return self.submit(args)
        record=self.record(args);jid=record['job_id']
        if operation=='record':return record
        if operation=='get':
            offset=bounded_int(args.get('offset',0),0,16*1024*1024)
            if record['status'] not in TERMINAL:
                with self.lock: cap=self.live.get(jid,{}).get('capability','')
                data=self.docker('logs','--tail','100',self.container(jid),check=False)
                if cap:data=data.replace(cap.encode(),b'[redacted]')
                record['log_tail']=data[-12000:].decode(errors='replace')
            else:
                log=self.root/jid/'optimizer.log'
                data=log.read_bytes() if log.exists() else b''
                record.update(log=data[offset:offset+12000].decode(errors='replace'),next_offset=min(offset+12000,len(data)),log_eof=offset+12000>=len(data))
            return record
        if operation=='cancel':
            with self.lock:
                if jid in self.live:self.live[jid]['cancel'].set()
            return {'job_id':jid,'cancel_requested':record['status'] not in TERMINAL,'status':record['status']}
        if operation=='read':
            item=next((x for x in record['artifacts'] if x['path']==args.get('path')),None)
            if not item:raise Failure('ARTIFACT_NOT_FOUND')
            offset=bounded_int(args.get('offset',0),0,item['bytes'])
            limit=bounded_int(args.get('limit',24576),1,24576)
            with (self.root/jid/item['path']).open('rb') as file:file.seek(offset);data=file.read(limit)
            return {**item,'encoding':'base64','data':base64.b64encode(data).decode(),'offset':offset,'next_offset':offset+len(data),'eof':offset+len(data)>=item['bytes']}
        raise Failure('UNKNOWN_OPERATION')

    def model(self,handler,payload):
        supplied=handler.headers.get('Authorization','').removeprefix('Bearer ')
        with self.lock:
            match=next(((jid,state) for jid,state in self.live.items() if hmac.compare_digest(state['capability'],supplied)),None)
        if not match:raise Failure('JOB_AUTHORIZATION_REQUIRED')
        jid,live=match
        with live['model_lock']:
            if live['cancel'].is_set() or time.time()>=live['deadline']:raise Failure('JOB_EXPIRED')
            self.grant_status(live['credentials'])
            record=json.loads((self.root/jid/'job.json').read_text());spec=record['spec']
            if payload.get('model')!=spec['model']:raise Failure('MODEL_NOT_AUTHORIZED')
            if payload.get('background'):raise Failure('BACKGROUND_MODEL_REQUEST_NOT_ALLOWED')
            if handler.path == '/v1/responses': payload['store']=False
            if spec['protocol']=='anthropic':
                for tool in payload.get('tools',[]):
                    if not isinstance(tool,dict) or tool.get('type') or not isinstance(tool.get('input_schema'),dict):
                        raise Failure('REMOTE_HOSTED_TOOL_NOT_ALLOWED')
            else:check_client_tools(payload.get('tools',[]))
            if isinstance(payload.get('input'),list):
                for item in payload['input']:
                    if isinstance(item,dict) and item.get('type')=='additional_tools':
                        check_client_tools(item.get('tools'))
            if handler.path not in MODEL_PATHS:raise Failure('MODEL_ENDPOINT_NOT_ALLOWED')
            if record['model_requests'] >= spec.get('max_model_requests', 80):
                raise Failure('MODEL_REQUEST_BUDGET_EXCEEDED')
            anthropic=spec['protocol']=='anthropic'
            if anthropic and handler.path!='/v1/messages':raise Failure('MODEL_ENDPOINT_NOT_ALLOWED')
            if not anthropic and handler.path=='/v1/messages':raise Failure('MODEL_ENDPOINT_NOT_ALLOWED')
            output_field = {'/v1/responses':'max_output_tokens','/v1/messages':'max_tokens','/v1/chat/completions':'max_completion_tokens'}[handler.path]
            payload.setdefault(output_field, 8192)
            for field in ('max_tokens','max_output_tokens','max_completion_tokens'):
                if field in payload:bounded_int(payload[field],1,16384)
            self.update(jid,model_requests=record['model_requests']+1)
            credentials=live['credentials']
            url=spec['base_url']+(handler.path if anthropic else handler.path[3:])
            headers={'Content-Type':'application/json','User-Agent':'Nexus-Atrex-Runner/0.3'}
            if anthropic:headers.update({'x-api-key':credentials['secret'],'anthropic-version':'2023-06-01'})
            else:headers['Authorization']='Bearer '+credentials['secret']
            request=urllib.request.Request(url,data=json.dumps(payload).encode(),headers=headers)
            try:response=self.model_transport(request,timeout=90)
            except urllib.error.HTTPError as exc:
                self.update(jid,last_provider_http_status=exc.code)
                handler.reply(exc.code,{'error':{'message':'Authorized model provider returned HTTP '+str(exc.code),'type':'provider_error'}});return
            with response:
                self.update(jid,last_provider_http_status=response.status)
                handler.send_response(response.status);handler.send_header('Content-Type',response.headers.get('Content-Type','application/json'))
                handler.send_header('Connection','close');handler.end_headers()
                size=0
                while chunk:=response.read1(8192):
                    size+=len(chunk)
                    if size>32*1024*1024 or live['cancel'].is_set() or time.time()>=live['deadline']:break
                    handler.wfile.write(chunk);handler.wfile.flush()
                handler.close_connection=True


class Server(socketserver.ThreadingMixIn,getattr(socketserver,'UnixStreamServer',socketserver.TCPServer)):
    daemon_threads=True
    def __init__(self,*args,**kwargs):
        self.slots=threading.BoundedSemaphore(16)
        super().__init__(*args,**kwargs)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            self.close_request(request);return
        try:super().process_request(request,address)
        except Exception:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)
    def log_message(self,*args):pass
    def reply(self,status,value):
        data=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_POST(self):
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=BODY_LIMIT:raise Failure('REQUEST_TOO_LARGE')
            data=json.loads(self.rfile.read(size))
            if self.server.is_model:self.server.broker.model(self,data)
            elif self.path=='/events':
                record=self.server.broker.record(data)
                journal=self.server.broker.journals[record['job_id']]
                after=data.get('after',0)
                if type(after) is not int or not 0<=after<=len(journal.rows):raise Failure('INVALID_EVENT_CURSOR')
                self.send_response(200);self.send_header('Content-Type','application/x-ndjson');self.send_header('Connection','close');self.end_headers()
                for row in journal.follow(after):self.wfile.write((json.dumps(row)+'\n').encode());self.wfile.flush()
                self.close_connection=True
            else:self.reply(200,self.server.broker.control(self.path.lstrip('/'),data))
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as exc:
            self.reply(400,{'error':str(exc) if isinstance(exc,Failure) else 'REQUEST_FAILED'})


def main():
    if not hasattr(socketserver,'UnixStreamServer'):
        raise RuntimeError('The execution broker requires Linux Unix sockets.')
    root=Path(os.environ['ATREX_BROKER_ROOT'])
    broker=Broker(root,os.environ.get('ATREX_WORKER_IMAGE','nexus-atrex-worker:20261018'),os.environ['ATREX_PROJECT_ROOT'],int(os.environ.get('ATREX_GPU','0')))
    servers=[]
    for name,is_model in [('control/control.sock',False),('proxy/model.sock',True)]:
        path=root/name;path.parent.mkdir(exist_ok=True);path.parent.chmod(0o755);path.unlink(missing_ok=True)
        server=Server(str(path),Handler);server.broker=broker;server.is_model=is_model;path.chmod(0o600)
        servers.append(server);threading.Thread(target=server.serve_forever,daemon=True).start()
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    stop.wait()
    for state in list(broker.live.values()):state['cancel'].set()
    deadline=time.time()+35
    while broker.live and time.time()<deadline:time.sleep(.5)
    for server in servers:server.shutdown()


if __name__=='__main__':main()
