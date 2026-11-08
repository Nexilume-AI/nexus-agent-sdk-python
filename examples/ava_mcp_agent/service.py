"""Caller-owned source snapshots, native rule jobs and optional authorized reviews."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile

from file_bridge import Bridges, save, sha256
from job_events import Journal, Delivery

ROOT = Path(__file__).resolve().parent
DATA, CONTROL = ROOT/'data', ROOT/'control'
COMMIT = 'd247b29c8399fa51828b184652b0b34c41d5d888'
IMAGE = 'nexus-ava-mcp:'+COMMIT[:12]
STRATEGIES = ['pipeline', 'model_pipeline', 'compare', 'generate', 'rtl_check', 'simulate']
S = {'type': 'string'}
AUTH = {k: S for k in ('workspace_id', 'workspace_key')}


def tool(name, description, properties=None, required=(), auth=True):
    return {'name': name, 'description': description, 'inputSchema': {
        'type': 'object', 'properties': {**(AUTH if auth else {}), **(properties or {})},
        'required': [*(AUTH if auth else {}), *required], 'additionalProperties': False}}


CATALOG=[
 tool('get_ava_status','Inspect pinned AVA upstream, EDA toolchain, job limits and evidence limitations.',auth=False),
 tool('ava_tool_help','Get complete input schema and instructions for a tool.',{'tool_name':S},('tool_name',),False),
 tool('authorize_ava_provider','Authorize your own Nexus Router or OpenAI-compatible model through secure confirmation or explicit MCP preauthorization. Never send keys as tool arguments.',{'provider':{'type':'string','enum':['nexus','openai','custom_openai']},'model':S,'base_url':S,'authorization_id':S,'force_new':{'type':'boolean'}},auth=False),
 tool('create_ava_workspace','Create caller-owned workspace; save id/key. Same API key is one caller. Two workspaces/caller, eight total,24-hour idle expiry.',auth=False),
 tool('load_demo_project','Load demo RTL/testbench and matching/mismatching canonical commit logs. Fixtures demonstrate tools; they are not a verified RISC-V core.'),
 tool('put_project_file','Write a bounded UTF-8 project file in chunks of at most1024 bytes. Paths are relative, no traversal. offset is byte offset; identical chunk retries allowed. Max1MiB/file,8MiB/workspace,128files. Upload .sv/.v/.vh/.jsonl/.json/.S/.hex/.txt only.',{'path':S,'content':{'type':'string','maxLength':1024},'offset':{'type':'integer','minimum':0}},('path','content')),
 tool('start_verification_job','Queue native AVA pipeline/model_pipeline, canonical commit-log compare, RV32IM test generation+GCC+Spike, RTL Verilator lint+Yosys+native graph, or Verilator binary simulation with your tb.sv. Pipeline may report no_data until a real DUT harness exists; never implies sign-off. model_pipeline requires caller authorization_id. UUID request_id makes retries idempotent. One queued/running job per caller. Returns immediately; model jobs include Background page.',{'mode':{'type':'string','enum':STRATEGIES},'request_id':S,'authorization_id':S,'rtl_path':S,'top':S,'rtl_log':S,'iss_log':S,'seed':{'type':'integer','minimum':0,'maximum':4294967295},'instructions':{'type':'string','maxLength':2000}},('request_id','mode')),
 tool('job_status','Read job state and native report. Job succeeded means execution completed; inspect verification_status, gaps and measurements.',{'job_id':S},('job_id',)),
 tool('cancel_job','Cancel queued/running job, including model request and isolated process. Wait for terminal state.',{'job_id':S},('job_id',)),
 tool('list_artifacts','List native reports, generated assembly/ELF, simulation evidence and result.zip with SHA256.',{'job_id':S},('job_id',)),
 tool('read_artifact','Read artifact in base64 pages up to24576bytes, with SHA256. Concatenate pages until eof.',{'job_id':S,'path':S,'offset':{'type':'integer','minimum':0}},('job_id','path')),
 tool('get_artifact_download','Create authenticated download:10-minute one-time redemption,1-hour session, HTTP Range, SHA256 and revocation.',{'job_id':S,'path':S},('job_id','path')),
 tool('revoke_file_link','Revoke download capability and download session.',{'bridge_id':S},('bridge_id',)),
 tool('close_ava_workspace','Delete caller workspace and revoke downloads after all jobs stop.'),
]


def result(value, error=False):
    return {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value, 'isError': error}


def valid_identity(identity):
    return (isinstance(identity, dict) and identity.get('version') == 1
            and identity.get('principal_type') in ('api_key', 'user', 'service_account')
            and all(re.fullmatch('[0-9a-f]{64}', str(identity.get(k, ''))) for k in ('owner_scope', 'quota_scope')))


def source_name(value):
    if (not isinstance(value,str) or not re.fullmatch(r'[a-zA-Z0-9_./-]{1,160}',value)
            or value.startswith(('/', '-')) or '..' in value.split('/')
            or Path(value).suffix not in ('.sv','.v','.vh','.jsonl','.json','.S','.hex','.txt')):
        raise ValueError('INVALID_PATH')
    return value


class Service:
    def __init__(self):
        DATA.mkdir(mode=0o700, exist_ok=True)
        CONTROL.mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.RLock()
        self.stop, self.wake = threading.Event(), threading.Event()
        self.workspaces, self.live, self.deliveries = {}, {}, {}
        self.pending, self.active = [], None
        for p in DATA.glob('*/workspace.json'):
            w = json.loads(p.read_text())
            w.update(folder=p.parent, jobs={})
            if w['closed']:
                continue
            for f in p.parent.glob('jobs/*/job.json'):
                j = json.loads(f.read_text())
                j['folder'] = f.parent/'work'
                if j['state'] in ('queued', 'running'):
                    subprocess.run(['docker', 'rm', '-f', j['container']], capture_output=True, timeout=20)
                    j.update(state='interrupted', ended=time.time())
                    self.save_job(j)
                w['jobs'][j['id']] = j
            w['bridges'] = Bridges(w)
            self.workspaces[w['id']] = w
        self.thread = threading.Thread(target=self.queue, daemon=True)
        self.thread.start()

    def save_workspace(self, w):
        save(w['folder']/'workspace.json', {k: v for k, v in w.items() if k not in ('folder', 'jobs', 'bridges')})

    def save_job(self, j):
        save(j['folder'].parent/'job.json', {k: v for k, v in j.items() if k != 'folder'})

    def call(self, name, args, identity=None, ctx=None):
        try:
            if name not in ('get_ava_status', 'ava_tool_help') and not valid_identity(identity):
                raise ValueError('VERIFIED_CALLER_REQUIRED')
            with self.lock:
                return result(self.dispatch(name, args, identity, ctx))
        except Exception as exc:
            code = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[A-Z_0-9]+', str(exc)) else 'REQUEST_FAILED'
            return result({'error': code, 'message': 'Check caller ownership, inputs, model authorization and job state.'}, True)

    def dispatch(self, name, a, identity, ctx):
        if name == 'get_ava_status':
            return {'ready':(ROOT/'runtime-ready.json').is_file(),'commit':COMMIT,'tools':len(CATALOG),
                'modes':STRATEGIES,'toolchain':['Verilator','Yosys','Spike','RISC-V GCC','z3'],
                'native_components':['AVA.generate_suite','AGENT_D.compare_logs','AGENT_G.generate_tests','RTLGraphAnalyzer'],
                'limits':'One job per caller; FIFO single CPU worker;900-second job deadline;8MiB inputs;16MiB exported artifacts',
                'limitations':'No physical sign-off or commercial EDA. Pipeline-generated harness is not automatically a runnable RISC-V DUT: no-data/partial states remain explicit. LLM analysis is advisory.'}
        if name == 'ava_tool_help':
            return next(t for t in CATALOG if t['name'] == a.get('tool_name'))
        if name == 'create_ava_workspace':
            if len(self.workspaces) >= 8 or sum(w['quota'] == identity['quota_scope'] for w in self.workspaces.values()) >= 2:
                raise ValueError('WORKSPACE_LIMIT')
            wid, key = uuid.uuid4().hex, secrets.token_urlsafe(32)
            folder = DATA/wid
            (folder/'inputs').mkdir(parents=True, mode=0o700)
            w = {'id': wid, 'owner': identity['owner_scope'], 'quota': identity['quota_scope'],
                 'key_hash': hashlib.sha256(key.encode()).hexdigest(), 'folder': folder,
                 'jobs': {}, 'closed': False, 'touched': time.time()}
            w['bridges'] = Bridges(w)
            self.workspaces[wid] = w
            self.save_workspace(w)
            return {'workspace_id': wid, 'workspace_key': key, 'idle_seconds': 86400}
        w, key = self.workspaces.get(a.get('workspace_id')), a.get('workspace_key')
        if (not w or w['owner'] != identity['owner_scope'] or not isinstance(key, str)
                or not hmac.compare_digest(w['key_hash'], hashlib.sha256(key.encode()).hexdigest())):
            raise ValueError('WORKSPACE_NOT_FOUND')
        w['touched'] = time.time()
        self.save_workspace(w)
        if name == 'load_demo_project':
            from demos import FILES
            for path,content in FILES.items():
                (w['folder']/'inputs'/path).write_text(content,encoding='utf-8')
            return {'saved':list(FILES),'fixture':True}
        if name == 'put_project_file':
            path,content=source_name(a.get('path')),a.get('content')
            offset=a.get('offset',0)
            if not isinstance(content,str) or '\0' in content or not 1<=len(content.encode())<=1024 or type(offset)!=int or offset<0:
                raise ValueError('INVALID_SOURCE')
            target=w['folder']/'inputs'/path;chunk=content.encode()
            files=list((w['folder']/'inputs').rglob('*'))
            if offset+len(chunk)>1048576 or sum(p.stat().st_size for p in files if p.is_file())+len(chunk)>8388608 or (not target.exists() and sum(p.is_file() for p in files)>=128):
                raise ValueError('INPUT_LIMIT')
            target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('r+b' if target.exists() else 'w+b') as f:
                f.seek(0,2);size=f.tell()
                if offset>size:raise ValueError('INVALID_OFFSET')
                if offset<size:
                    f.seek(offset)
                    if f.read(len(chunk))!=chunk:raise ValueError('CHUNK_CONFLICT')
                else:f.write(chunk)
            return {'saved':path,'next_offset':offset+len(chunk),'sha256':sha256(target)}
        if name == 'start_verification_job':
            return self.start(w, a, ctx)
        if name == 'revoke_file_link':
            w['bridges'].revoke(a['bridge_id'])
            return {'revoked': True}
        if name == 'close_ava_workspace':
            if any(j['state'] in ('queued', 'running') for j in w['jobs'].values()):
                raise ValueError('JOB_BUSY')
            self.remove(w)
            return {'closed': True}
        j = w['jobs'].get(a.get('job_id'))
        if not j:
            raise ValueError('JOB_NOT_FOUND')
        if name == 'cancel_job':
            if j['state'] in ('queued', 'running'):
                self.live[j['id']]['cancel'].set()
            return {'cancellation_requested': True, 'state': j['state']}
        if name == 'job_status':
            out = {k: j.get(k) for k in ('id', 'state', 'stage', 'created', 'started', 'ended', 'view_url', 'error')}
            if j['state'] not in ('queued', 'running'):
                for n in ('report.json', 'model-metrics.json'):
                    if (j['folder']/n).exists():
                        out[n] = json.loads((j['folder']/n).read_text())
            return out
        if j['state'] in ('queued', 'running'):
            raise ValueError('JOB_BUSY')
        if name == 'list_artifacts':
            return {'files': [{'path': p.relative_to(j['folder']).as_posix(), 'size': p.stat().st_size, 'sha256': sha256(p)}
                              for p in sorted(j['folder'].rglob('*')) if p.is_file() and not p.is_symlink()]}
        path = a.get('path')
        if not isinstance(path, str) or not 1 <= len(path) <= 240:
            raise ValueError('INVALID_PATH')
        p = (j['folder']/path).resolve()
        if not p.is_relative_to(j['folder']) or not p.is_file():
            raise ValueError('ARTIFACT_NOT_FOUND')
        if name == 'get_artifact_download':
            return w['bridges'].create(ctx, p, 'download', sha256(p), p.stat().st_size)
        if name == 'read_artifact':
            offset = a.get('offset', 0)
            if type(offset) != int or not 0 <= offset <= p.stat().st_size:
                raise ValueError('INVALID_OFFSET')
            with p.open('rb') as f:
                f.seek(offset)
                raw = f.read(24576)
            return {'data_base64': base64.b64encode(raw).decode(), 'next_offset': offset+len(raw),
                    'eof': offset+len(raw) >= p.stat().st_size, 'sha256': sha256(p)}
        raise ValueError('UNKNOWN_TOOL')

    def start(self, w, a, ctx):
        rid = str(uuid.UUID(a['request_id']))
        for j in w['jobs'].values():
            if j['request_id'] == rid:
                return {'job_id': j['id'], 'state': j['state'], 'view_url': j.get('view_url'), 'idempotent': True}
        if not (ROOT/'runtime-ready.json').is_file():
            raise ValueError('RUNTIME_NOT_READY')
        strategy=a.get('mode','pipeline')
        review=strategy=='model_pipeline'
        if strategy not in STRATEGIES:raise ValueError('INVALID_MODE')
        seed=a.get('seed',42)
        if type(seed)!=int or not 0<=seed<=4294967295:raise ValueError('INVALID_SEED')
        top=a.get('top','demo_core')
        if not isinstance(top,str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,99}',top):raise ValueError('INVALID_TOP')
        rtl=source_name(a.get('rtl_path','core.sv'));rtl_log=source_name(a.get('rtl_log','rtl.jsonl'));iss_log=source_name(a.get('iss_log','iss.jsonl'))
        instructions=a.get('instructions','')
        if not isinstance(instructions,str) or len(instructions)>2000:raise ValueError('INVALID_INSTRUCTIONS')
        needed=([rtl_log,iss_log] if strategy=='compare' else [] if strategy=='generate' else [rtl])
        if strategy=='simulate':needed+=['tb.sv']
        if any(not (w['folder']/'inputs'/name).is_file() for name in needed):raise ValueError('PROJECT_EMPTY')
        if len(w['jobs']) >= 16 or len(self.pending) >= 8 or any(j['quota'] == w['quota'] for j in self.pending) or (self.active and self.active['quota'] == w['quota']):
            raise ValueError('CALLER_BUSY_OR_QUEUE_FULL')
        grant, credentials, delivery, aid = {}, None, None, None
        if review:
            from nexus_agent.credentials import ProviderCredentials
            from nexus_agent.background_jobs import BackgroundJobs
            aid = str(uuid.UUID(a['authorization_id']))
            credentials = ProviderCredentials(ctx)
            grant = credentials.resolve_for_runtime(aid)
            if grant.get('provider') not in ('nexus', 'openai', 'custom_openai') or not grant.get('model'):
                grant.clear()
                raise ValueError('OPENAI_COMPATIBLE_MODEL_REQUIRED')
            try:
                delivery = BackgroundJobs(ctx).register_for_runtime(aid, rid)
            except Exception:
                grant.clear()
                raise
        jid = uuid.uuid4().hex
        folder, private = w['folder']/'jobs'/jid/'work', CONTROL/jid
        folder.mkdir(parents=True)
        private.mkdir(mode=0o700)
        shutil.copytree(w['folder']/'inputs', folder/'inputs')
        save(folder/'request.json', {'mode':strategy,'instructions':instructions,'seed':seed,'top':top,'rtl_path':rtl,'rtl_log':rtl_log,'iss_log':iss_log,'model':grant.get('model')})
        j = {'id': jid, 'quota': w['quota'], 'request_id': rid, 'authorization_id': aid, 'folder': folder,
             'state': 'queued', 'stage': 'queued', 'created': time.time(), 'review': review, 'mode':strategy,
             'container': 'nexus-ava-'+jid, 'view_url': delivery.get('view_url') if delivery else None}
        journal, cancel = Journal(private, delivery), threading.Event()
        self.live[jid] = {'grant': grant, 'credentials': credentials, 'journal': journal, 'cancel': cancel}
        if delivery:
            d = Delivery(journal, cancel)
            self.deliveries[jid] = d
            d.start()
        journal.emit('state', {'job_id': jid, 'status': 'queued'})
        w['jobs'][jid] = j
        self.save_job(j)
        self.pending.append(j)
        self.wake.set()
        return {'job_id': jid, 'state': 'queued', 'view_url': j['view_url']}

    def interrupted(self, j, live):
        if self.stop.is_set():
            return 'interrupted'
        if live['cancel'].is_set():
            return 'cancelled'
        if time.time()-j['started'] > 900:
            return 'timed_out'
        return None

    def check_grant(self, j, live):
        status = live['credentials'].status(j['authorization_id'])
        if status.get('status') != 'authorized' or any(status.get(k) != live['grant'].get(k) for k in ('provider', 'model', 'base_url')):
            raise ValueError('AUTHORIZATION_EXPIRED_OR_CHANGED')

    def execute(self,j,live):
        from broker import Broker
        if self.interrupted(j,live):j['_final_state']=self.interrupted(j,live);return
        j['stage']='native_toolchain'
        private=CONTROL/j['id'];broker=None
        if j['review']:
            self.check_grant(j,live)
            broker=Broker(private,live['grant'],lambda:self.check_grant(j,live),lambda s:live['journal'].emit('log',{'text':s}),10)
        try:
            cmd=[sys.executable,str(ROOT/'runner.py'),str(j['folder']),j['container'],str(private)]
            with (j['folder']/'run.log').open('wb') as log:
                p=subprocess.Popen(cmd,stdout=log,stderr=log)
                checked=time.monotonic()
                try:
                    while p.poll() is None:
                        forced=self.interrupted(j,live)
                        if forced:j['_final_state']=forced;return
                        if broker and time.monotonic()-checked>10:self.check_grant(j,live);checked=time.monotonic()
                        time.sleep(.2)
                finally:
                    if p.poll() is None:
                        p.terminate()
                        subprocess.run(['docker','rm','-f',j['container']],capture_output=True,timeout=20)
                        try:p.wait(timeout=10)
                        except subprocess.TimeoutExpired:p.kill();p.wait()
            if p.returncode or not (j['folder']/'report.json').is_file():raise ValueError('NATIVE_PIPELINE_FAILED')
            if broker:
                self.check_grant(j,live)
                save(j['folder']/'model-metrics.json',{'model':live['grant']['model'],'requests':broker.count,'usage':broker.usage})
                episode=j['folder']/'episode.json'
                if episode.exists():
                    e=json.loads(episode.read_text());e.update(model=live['grant']['model'],prompt_tokens=sum(u.get('prompt_tokens',0) for u in broker.usage),output_tokens=sum(u.get('completion_tokens',0) for u in broker.usage));save(episode,e)
            with zipfile.ZipFile(j['folder']/'result.zip','w',zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(j['folder'].rglob('*')):
                    if path.is_file() and path.name!='result.zip':archive.write(path,path.relative_to(j['folder']))
            j.update(_final_state='succeeded',stage='complete')
            live['journal'].emit('log',{'text':'Native AVA workflow completed. Inspect evidence, verification status and coverage gaps; completion is not sign-off.'})
        finally:
            if broker:broker.close()

    def queue(self):
        while not self.stop.is_set():
            self.wake.wait(2)
            self.wake.clear()
            with self.lock:
                for w in list(self.workspaces.values()):
                    if time.time()-w['touched'] > 86400 and not any(j['state'] in ('queued', 'running') for j in w['jobs'].values()):
                        self.remove(w)
                if not self.pending:
                    continue
                j = self.pending.pop(0)
                self.active = j
                j.update(state='running', started=time.time())
                self.save_job(j)
            live = self.live[j['id']]
            try:
                live['journal'].emit('state', {'job_id': j['id'], 'status': 'running'})
                self.execute(j, live)
            except Exception as exc:
                j['_final_state'] = 'failed'
                j['error'] = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[A-Z_0-9]+', str(exc)) else type(exc).__name__
            finally:
                live['grant'].clear()
                with self.lock:
                    j['state'] = j.pop('_final_state', 'failed')
                    j['ended'] = time.time()
                    self.save_job(j)
                    save(CONTROL/j['id']/'job.json', {'artifacts': []})
                    live['journal'].emit('state', {'job_id': j['id'], 'status': j['state']})
                    self.live.pop(j['id'], None)
                    self.active = None
                self.wake.set()

    def remove(self, w):
        w['closed'] = True
        w['bridges'].close()
        self.workspaces.pop(w['id'])
        assert w['folder'].parent == DATA
        shutil.rmtree(w['folder'])
        for jid in w['jobs']:
            d = self.deliveries.pop(jid, None)
            if d:
                d.stop.set()
            private = CONTROL/jid
            assert private.parent == CONTROL and re.fullmatch('[0-9a-f]{32}', jid)
            if private.exists():
                shutil.rmtree(private)

    def close(self):
        self.stop.set()
        self.wake.set()
        self.thread.join(timeout=45)
        for live in self.live.values():
            live['grant'].clear()
        for d in self.deliveries.values():
            d.stop.set()
