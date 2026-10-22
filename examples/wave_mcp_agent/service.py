"""Nexus bridge: each bearer-protected workspace owns a networkless Docker worker."""
import base64
import copy
import hashlib
import hmac
import json
import os
import queue
import secrets
import subprocess
import threading
import time
import uuid
from fair_queue import FairQueue

IMAGE = os.environ.get('WAVE_IMAGE', 'nexus-wave-mcp:0b8c5fff11c9')
VIEWER_TOOLS = {'open_wave_view','update_wave_view','get_view_state','list_wave_views','close_wave_view'}
AUTH = {'workspace_id':{'type':'string','pattern':'^[0-9a-f]{32}$'}, 'workspace_key':{'type':'string','description':'Private workspace capability returned by create_wave_workspace.'}}


def result(value, error=False):
    return {'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}], 'structuredContent':value, 'isError':error}


class Worker:
    def __init__(self, sample=False):
        self.id = uuid.uuid4().hex
        self.name = 'nexus-wave-work-'+self.id
        self.key = secrets.token_urlsafe(32)
        self.key_hash = hashlib.sha256(self.key.encode()).digest()
        self.lock = threading.Lock()
        self.last = time.monotonic()
        self.results = {}
        self.sequence = 0
        self.inbox = queue.Queue()
        self.process = subprocess.Popen(['docker','run','--rm','--init','-i','--name',self.name,
            '--label','nexus.service=wave-mcp-worker','--network','none','--read-only','--user',f'{os.getuid()}:{os.getgid()}',
            '--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','768m','--cpus','1','--pids-limit','64',
            '--ulimit','nofile=512:512','--tmpfs',f'/work:rw,nosuid,nodev,noexec,size=256m,uid={os.getuid()},gid={os.getgid()},mode=700',
            '--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=64m',
            '-e','WAVE_SAMPLE='+('1' if sample else '0'),IMAGE],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1)
        threading.Thread(target=self._read,daemon=True).start()
        try:
            self.rpc('initialize',{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'Nexus-Wave-Bridge','version':'1.0'}},timeout=35)
            self.send({'jsonrpc':'2.0','method':'notifications/initialized'})
        except BaseException:
            self.close();raise

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(32*1024*1024)
                if not line: break
                if len(line)>=32*1024*1024: break
                try:self.inbox.put(json.loads(line))
                except ValueError:continue
        finally:self.inbox.put({'error':{'message':'WORKSPACE_WORKER_EXITED'}})

    def send(self, message):
        self.process.stdin.write(json.dumps(message,separators=(',',':'))+'\n');self.process.stdin.flush()

    def rpc(self, method, params=None, timeout=45):
        self.sequence += 1
        self.send({'jsonrpc':'2.0','id':self.sequence,'method':method,'params':params or {}})
        deadline=time.monotonic()+timeout
        while True:
            try:message=self.inbox.get(timeout=max(.01,deadline-time.monotonic()))
            except queue.Empty:
                self.close();raise RuntimeError('WORKSPACE_TIMEOUT_CLOSED') from None
            if 'error' in message and 'id' not in message:raise RuntimeError('WORKSPACE_WORKER_EXITED')
            if message.get('id')!=self.sequence:continue
            if 'error' in message:raise ValueError(str(message['error'].get('message','MCP error'))[:800])
            return message.get('result',{})

    def close(self):
        subprocess.run(['docker','rm','-f',self.name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:self.process.kill()
        self.results.clear()

    def bounded(self, value):
        blob=json.dumps(value,ensure_ascii=False).encode()
        if len(blob)<=200000:return value
        if len(blob)>16*1024*1024:return result({'error':'RESULT_TOO_LARGE','message':'Narrow the query, signal set or time window.'},True)
        while self.results and sum(len(v) for v in self.results.values())+len(blob)>16*1024*1024:
            self.results.pop(next(iter(self.results)))
        rid=uuid.uuid4().hex;self.results[rid]=blob
        return result({'paged_result':True,'result_id':rid,'size':len(blob),'sha256':hashlib.sha256(blob).hexdigest(),
                       'next_step':'Use read_wave_result with the same workspace and result_id. Concatenate decoded base64 pages to obtain the complete original MCP result JSON.'})


def local_tool(name, description, properties=None, required=(), auth=True):
    return {'name':name,'description':description,'inputSchema':{'type':'object','properties':{**(AUTH if auth else {}),**(properties or {})},
            'required':list(AUTH if auth else [])+list(required),'additionalProperties':False}}


LOCAL = [
 local_tool('create_wave_workspace','Create a private Wave MCP workspace bound to your authenticated caller. The same API Key shares workspaces across MCP sessions. Save workspace_id/key. sample=true includes /work/sample counter VCD and RTL. Temporary 256 MiB storage expires after 30 minutes idle or restart. Download artifacts before closing. Limit 2 workspaces per caller, 4 total.',{'sample':{'type':'boolean','default':False}},auth=False),
 local_tool('get_wave_status','Show deployed upstream version, tool availability, isolation limits and optional dependencies. Does not list other users or workspaces.',auth=False),
 local_tool('close_wave_workspace','Permanently close this workspace and discard its in-memory files, sessions and paged results. Download required files before calling.'),
 local_tool('read_wave_result','Read a large saved MCP result without rerunning the tool. Decode each base64 page and concatenate until eof; then parse the original JSON. Offsets are bytes. Results belong to this workspace and expire with it.',{'result_id':{'type':'string'},'offset':{'type':'integer','minimum':0,'default':0}},('result_id',)),
 local_tool('wave_tool_help','Get the complete original upstream documentation and input schema for a Wave tool. Use this for argument meanings, time units, query examples and optional conversion dependencies.',{'tool_name':{'type':'string'}},('tool_name',),auth=False),
]


def compact_schema(value):
    if isinstance(value,dict):return {k:compact_schema(v) for k,v in value.items() if k not in ('title',)}
    if isinstance(value,list):return [compact_schema(v) for v in value]
    return value


class Service:
    def __init__(self):
        self.lock=threading.RLock();self.workers={};self.stop=threading.Event()
        self.scheduler=FairQueue();self.creating=0;self.creating_by_caller={}
        probe=Worker()
        try:
            self.original={t['name']:t for t in probe.rpc('tools/list')['tools'] if t['name'] not in VIEWER_TOOLS}
        finally:probe.close()
        self.catalog=copy.deepcopy(LOCAL)
        for name,item in self.original.items():
            schema=compact_schema(copy.deepcopy(item['inputSchema']))
            schema.setdefault('properties',{}).update(AUTH)
            schema['required']=list(dict.fromkeys([*schema.get('required',[]),*AUTH]))
            doc=item.get('description','')
            short=doc.split('\n\n')[0].replace('\n',' ').strip()
            if len(short.encode())>330:short=short.encode()[:320].decode(errors='ignore')+'…'
            desc=short+' Requires workspace_id/key. File paths use /work. Call wave_tool_help for full instructions.'
            self.catalog.append({'name':name,'description':desc,'inputSchema':schema})
        threading.Thread(target=self.reap,daemon=True).start()

    def reap(self):
        while not self.stop.wait(30):
            with self.lock:workers=list(self.workers.values())
            for worker in workers:
                if time.monotonic()-worker.last>1800 and worker.lock.acquire(blocking=False):
                    try:
                        with self.lock:self.workers.pop(worker.id,None)
                        worker.close()
                    finally:worker.lock.release()

    def close(self):
        self.stop.set()
        self.scheduler.close()
        with self.lock:
            workers=list(self.workers.values());self.workers.clear()
        for worker in workers:worker.close()

    def call(self,name,args,identity=None):
        try:
            if name in ('get_wave_status','wave_tool_help'):
                return self._call(name,dict(args),None)
            if (not isinstance(identity,dict) or identity.get('version')!=1
                    or identity.get('principal_type') not in ('api_key','user','service_account')
                    or any(not isinstance(identity.get(k),str) or len(identity[k])!=64
                           or any(c not in '0123456789abcdef' for c in identity[k])
                           for k in ('owner_scope','quota_scope'))):
                raise ValueError('VERIFIED_CALLER_REQUIRED; use the authenticated Nexus Cloud MCP endpoint.')
            if name!='create_wave_workspace':
                self.authorized_worker(args,identity)
            with self.scheduler.slot(identity['quota_scope']):
                return self._call(name,dict(args),identity)
        except (ValueError,RuntimeError,OSError,KeyError) as exc:
            return result({'error':'WAVE_REQUEST_FAILED','message':str(exc)[:800]},True)

    def authorized_worker(self,args,identity):
        wid=args.get('workspace_id','');key=args.get('workspace_key','')
        with self.lock:worker=self.workers.get(wid) if isinstance(wid,str) else None
        if (not worker or not hmac.compare_digest(worker.owner_scope,identity['owner_scope'])
                or not isinstance(key,str) or not hmac.compare_digest(worker.key_hash,hashlib.sha256(key.encode()).digest())):
            raise ValueError('WORKSPACE_NOT_FOUND_OR_EXPIRED')
        return worker

    def _call(self,name,args,identity):
        if name=='get_wave_status':return result({'upstream':'Tencent/wave-mcp','version':'1.1.0','commit':'0b8c5fff11c9b4bf1e668787322ec41d08dd9d91',
            'tools':len(self.catalog),'transport':'Nexus Cloud MCP over OpenWrt Relay','waveforms':['FST','VCD'],'static_rtl':True,
            'optional_unavailable':{'browser_viewer':'Viewer binaries are not installed; viewer tools are not advertised.','FSDB':'Requires a compatible Verdi FsdbReader runtime, not installed.'},
            'workspace':{'storage':'temporary tmpfs','max_bytes':268435456,'idle_seconds':1800,'max_workers':4,'max_workers_per_caller':2,'network':'none',
                'identity':'server-verified Key or account; same Key is one caller'},
            'scheduling':{'policy':'round-robin per caller; FIFO within caller','active_total':2,'active_per_caller':1,'pending_total':32,'pending_per_caller':8,'queue_timeout_seconds':10}})
        if name=='wave_tool_help':
            item=self.original.get(args.get('tool_name'))
            if not item:raise ValueError('Unknown tool.')
            return result(item)
        if name=='create_wave_workspace':
            caller=identity['quota_scope']
            with self.lock:
                if sum(w.quota_scope==caller for w in self.workers.values())+self.creating_by_caller.get(caller,0)>=2:
                    raise ValueError('CALLER_WORKSPACE_LIMIT; close one of your workspaces first.')
                if len(self.workers)+self.creating>=4:raise ValueError('WORKSPACE_CAPACITY_REACHED; close an unused workspace or retry later.')
                self.creating+=1;self.creating_by_caller[caller]=self.creating_by_caller.get(caller,0)+1
            try:
                worker=Worker(sample=bool(args.get('sample')))
                worker.owner_scope=identity['owner_scope'];worker.quota_scope=caller
                with self.lock:self.workers[worker.id]=worker
                key=worker.key;worker.key=None
            finally:
                with self.lock:
                    self.creating-=1;self.creating_by_caller[caller]-=1
                    if not self.creating_by_caller[caller]:del self.creating_by_caller[caller]
            return result({'workspace_id':worker.id,'workspace_key':key,'root':'/work','idle_seconds':1800,'temporary':True,
                'sample':{'wave_path':'/work/sample/dump.vcd','filelist_path':'/work/sample/rtl.f','top':'top_tb'} if args.get('sample') else None})
        worker=self.authorized_worker(args,identity)
        wid=args.pop('workspace_id');args.pop('workspace_key')
        if not worker.lock.acquire(blocking=False):raise ValueError('WORKSPACE_BUSY; one request per workspace at a time.')
        try:
            with self.lock:
                if self.workers.get(wid) is not worker:raise ValueError('WORKSPACE_NOT_FOUND_OR_EXPIRED')
            worker.last=time.monotonic()
            if name=='close_wave_workspace':
                with self.lock:self.workers.pop(wid,None)
                worker.close();return result({'closed':True})
            if name=='read_wave_result':
                data=worker.results.get(args.get('result_id'))
                if data is None:raise ValueError('RESULT_NOT_FOUND')
                start=args.get('offset',0)
                if type(start)!=int or not 0<=start<=len(data):raise ValueError('Invalid offset.')
                part=data[start:start+24576]
                return result({'data_base64':base64.b64encode(part).decode(),'next_offset':start+len(part),'eof':start+len(part)>=len(data),'sha256':hashlib.sha256(data).hexdigest()})
            if name not in self.original:raise ValueError('Unknown tool.')
            return worker.bounded(worker.rpc('tools/call',{'name':name,'arguments':args}))
        finally:worker.last=time.monotonic();worker.lock.release()
