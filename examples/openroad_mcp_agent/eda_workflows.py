"""Persistent ORFS jobs and bounded file transfer inside the isolated EDA container."""
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
import zipfile

STAGES = ['synth', 'floorplan', 'place', 'cts', 'route', 'finish']
ID = {'type': 'string', 'pattern': '^[0-9a-f]{32}$'}
TEXT = {'type': 'string'}
OFFSET = {'type': 'integer', 'minimum': 0, 'default': 0}

def tool(name, description, properties, required=()):
    return {'name': name, 'description': description, 'inputSchema': {
        'type': 'object', 'properties': properties, 'required': list(required),
        'additionalProperties': False}}

TOOLS = [
    tool('list_eda_platforms', 'List installed ORFS platforms and bundled design configurations. Presence does not guarantee that all optional PDK files or tools are installed.', {}),
    tool('create_eda_project', 'Create a project with a random ID and separate directory. Choose a bundled design, or provide top for your own RTL and upload .v/.sv/.vh/.sdc files. Keep project_id; projects are not globally listed.',
         {'platform': TEXT, 'design': TEXT, 'top': TEXT}, ['platform']),
    tool('upload_eda_file', 'Upload an input file as base64 chunks (max 48 KiB decoded). Use offset=0 first, then next_offset. Identical chunk retries are safe; conflicting bytes are rejected. Supply sha256 on the final chunk to verify the complete file. Upload before submitting a job.',
         {'project_id': ID, 'path': TEXT, 'offset': OFFSET, 'data_base64': TEXT, 'sha256': TEXT}, ['project_id', 'path', 'data_base64']),
    tool('run_eda_flow', 'Start an asynchronous, isolated RTL-to-GDS ORFS flow. Returns job_id immediately. request_id is a caller-generated retry key: reuse it only for the same request. Inputs are snapshotted. Poll get_eda_job; use list_eda_artifacts and read_eda_artifact when terminal. One flow runs at a time.',
         {'project_id': ID, 'request_id': {'type':'string','maxLength':64},
          'through': {'type':'string','enum':STAGES,'default':'finish'},
          'cores': {'type':'integer','minimum':1,'maximum':4,'default':2},
          'timeout_seconds': {'type':'integer','minimum':30,'maximum':21600,'default':3600}}, ['project_id','request_id']),
    tool('get_eda_job', 'Read durable job status, completed stages and incremental real process output. log_offset/next_offset are bytes. Poll until succeeded, failed, cancelled, timed_out, or interrupted. A restart marks unfinished jobs interrupted; they are never silently replayed.',
         {'job_id':ID,'log_offset':OFFSET,'limit':{'type':'integer','minimum':256,'maximum':16384,'default':4096}}, ['job_id']),
    tool('cancel_eda_job', 'Cancel a flow and terminate its subprocess group. Completed logs and partial artifacts remain downloadable after finalization.', {'job_id':ID}, ['job_id']),
    tool('list_eda_artifacts', 'List immutable artifacts of a terminal job with artifact_id, relative path, size and SHA256. Includes reports, metrics, netlists, layout outputs and a ZIP bundle when produced. Use offset/next_offset to paginate; read_eda_artifact transfers bytes.',
         {'job_id':ID,'offset':OFFSET,'limit':{'type':'integer','minimum':1,'maximum':100,'default':50}}, ['job_id']),
    tool('read_eda_artifact', 'Download artifact bytes through MCP without SSH. Use artifact_id from list_eda_artifacts; decode data_base64 and append at offset. Continue at next_offset until eof, then verify whole-file SHA256. Maximum chunk is 256 KiB on the upgraded Relay.',
         {'job_id':ID,'artifact_id':{'type':'string','pattern':'^[0-9a-f]{24}$'},'offset':OFFSET,
          'limit':{'type':'integer','minimum':1,'maximum':262144,'default':262144}}, ['job_id','artifact_id']),
]

EXAMPLES = {
    'list_eda_platforms': {},
    'create_eda_project': {'platform':'nangate45','design':'gcd'},
    'upload_eda_file': {'project_id':'0'*32,'path':'rtl/counter.v','offset':0,'data_base64':'bW9kdWxlIGNvdW50ZXI7IGVuZG1vZHVsZQo='},
    'run_eda_flow': {'project_id':'0'*32,'request_id':'my-first-flow','through':'finish'},
    'get_eda_job': {'job_id':'0'*32,'log_offset':0,'limit':4096},
    'cancel_eda_job': {'job_id':'0'*32},
    'list_eda_artifacts': {'job_id':'0'*32,'offset':0,'limit':50},
    'read_eda_artifact': {'job_id':'0'*32,'artifact_id':'0'*24,'offset':0,'limit':49152},
}
for descriptor in TOOLS:
    descriptor['inputSchema']['examples']=[EXAMPLES[descriptor['name']]]

def dump(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()

def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum: raise ValueError('Integer out of range')
    return value

def safe_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]{0,79}', value):
        raise ValueError('Use a simple identifier containing letters, numbers, underscore or hyphen')
    return value

class EdaWorkflows:
    def __init__(self, root='/work/eda', flow='/OpenROAD-flow-scripts/flow'):
        self.root, self.flow = Path(root).resolve(), Path(flow).resolve()
        for name in ('projects', 'jobs'): (self.root/name).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.processes = {}
        for state in (self.root/'jobs').glob('*/state.json'):
            value = json.loads(state.read_text())
            if value['status'] in ('queued','running','cancelling','finalizing'):
                value.update(status='interrupted', ended_at=time.time(), error='Service restarted; inspect retained outputs before starting a new job.')
                dump(state,value)
                self._artifacts(state.parent)

    def directory(self, kind, identifier):
        if not isinstance(identifier,str) or not re.fullmatch('[0-9a-f]{32}',identifier): raise ValueError('Invalid ID')
        path = self.root/kind/identifier
        if path.is_symlink() or not path.is_dir(): raise ValueError('ID not found')
        return path

    def call(self, name, args):
        if name not in {t['name'] for t in TOOLS}: raise ValueError('Unknown EDA tool')
        schema=next(t['inputSchema'] for t in TOOLS if t['name']==name)
        if not isinstance(args,dict) or set(args)-set(schema['properties']) or set(schema['required'])-set(args):
            raise ValueError('Missing or unsupported arguments')
        with self.lock: return getattr(self,name)(**args)

    def list_eda_platforms(self):
        return {'platforms':[{'platform':p.name,'designs':sorted(c.parent.name for c in p.glob('*/config.mk'))}
                            for p in sorted((self.flow/'designs').iterdir())
                            if p.is_dir() and (self.flow/'platforms'/p.name/'config.mk').is_file()]}

    def create_eda_project(self, platform, design=None, top=None):
        platform=safe_name(platform)
        if not (self.flow/'platforms'/platform/'config.mk').is_file(): raise ValueError('Platform is not installed')
        if (design is None)==(top is None): raise ValueError('Specify exactly one of design (bundled) or top (custom RTL)')
        if design is not None:
            design=safe_name(design)
            if not (self.flow/'designs'/platform/design/'config.mk').is_file(): raise ValueError('Bundled design not found')
        else:
            if not isinstance(top,str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,79}',top): raise ValueError('Invalid Verilog top name')
        if len(list((self.root/'projects').iterdir()))>=100: raise ValueError('Project quota reached; operator cleanup required')
        pid=uuid.uuid4().hex
        directory=self.root/'projects'/pid
        (directory/'inputs').mkdir(parents=True,mode=0o700)
        data={'project_id':pid,'platform':platform,'design':design,'top':top,'created_at':time.time()}
        dump(directory/'project.json',data)
        return data

    def upload_eda_file(self, project_id, path, data_base64, offset=0, sha256=None):
        directory=self.directory('projects',project_id)
        if json.loads((directory/'project.json').read_text())['design']: raise ValueError('Bundled projects use installed inputs; create a custom top project to upload RTL')
        integer(offset,0,32*1024*1024)
        if not isinstance(path,str) or not re.fullmatch('[A-Za-z0-9_./-]{1,160}',path): raise ValueError('Invalid relative path')
        parts=PurePosixPath(path)
        if parts.is_absolute() or any(p in ('.','..') or p.startswith('.') for p in parts.parts) or parts.suffix not in ('.v','.sv','.vh','.sdc'):
            raise ValueError('Only relative RTL/header/SDC paths are accepted')
        root=directory/'inputs'
        target=root/path
        if any(p.is_symlink() for p in [target,*target.parents]) or not target.resolve().is_relative_to(root): raise ValueError('Unsafe input path')
        if not isinstance(data_base64,str) or len(data_base64)>65536: raise ValueError('Chunk exceeds 48 KiB')
        raw=base64.b64decode(data_base64,validate=True)
        if offset+len(raw)>32*1024*1024: raise ValueError('File exceeds 32 MiB')
        files=[p for p in root.rglob('*') if p.is_file()]
        if (not target.exists() and len(files)>=100) or sum(p.stat().st_size for p in files)+len(raw)>128*1024*1024: raise ValueError('Project input quota exceeded')
        target.parent.mkdir(parents=True,exist_ok=True)
        size=target.stat().st_size if target.exists() else 0
        if offset>size: raise ValueError('Upload gap: use next_offset')
        with target.open('r+b' if target.exists() else 'w+b') as f:
            f.seek(offset)
            if offset<size:
                if f.read(len(raw))!=raw: raise ValueError('Conflicting retry; create a new project for changed input')
            else: f.write(raw)
        actual=digest(target) if sha256 else None
        if sha256 and actual!=sha256: raise ValueError('File SHA256 mismatch; uploaded file is not verified')
        return {'path':path,'next_offset':offset+len(raw),'size':target.stat().st_size,'sha256':actual}

    def run_eda_flow(self, project_id, request_id, through='finish', cores=2, timeout_seconds=3600):
        project=self.directory('projects',project_id)
        if not isinstance(request_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',request_id): raise ValueError('Invalid request_id')
        if through not in STAGES: raise ValueError('Unsupported stage')
        integer(cores,1,4);integer(timeout_seconds,30,21600)
        request={'project_id':project_id,'request_id':request_id,'through':through,'cores':cores,'timeout_seconds':timeout_seconds}
        for state in (self.root/'jobs').glob('*/state.json'):
            existing=json.loads(state.read_text())
            if existing['request']['project_id']==project_id and existing['request']['request_id']==request_id:
                if existing['request']!=request: raise ValueError('request_id already used with different arguments')
                return {'job_id':existing['job_id'],'status':existing['status'],'reused':True}
        if self.processes or any(json.loads(s.read_text())['status'] in ('queued','running','cancelling','finalizing') for s in (self.root/'jobs').glob('*/state.json')):
            raise ValueError('EDA worker is busy; wait for the active job to finish')
        if len(list((self.root/'jobs').iterdir()))>=50 or shutil.disk_usage(self.root).free<5*1024**3: raise ValueError('Job storage quota reached; operator cleanup required')
        spec=json.loads((project/'project.json').read_text())
        if any(p.is_symlink() for p in (project/'inputs').rglob('*')): raise ValueError('Symlink inputs are not allowed')
        if not spec['design']:
            if not list((project/'inputs').rglob('*.v')) and not list((project/'inputs').rglob('*.sv')): raise ValueError('Upload RTL files before running')
            if len(list((project/'inputs').rglob('*.sdc')))!=1: raise ValueError('Upload exactly one SDC constraint file')
        jid=uuid.uuid4().hex;directory=self.root/'jobs'/jid
        directory.mkdir(mode=0o700)
        shutil.copytree(project/'inputs',directory/'inputs',symlinks=False)
        if spec['design']:
            config='include '+str(self.flow/'designs'/spec['platform']/spec['design']/'config.mk')+'\n'
        else:
            rtl=sorted([* (directory/'inputs').rglob('*.v'),* (directory/'inputs').rglob('*.sv')])
            sdc=next((directory/'inputs').rglob('*.sdc'))
            config=f'export DESIGN_NAME = {spec["top"]}\nexport PLATFORM = {spec["platform"]}\n'
            config+='export VERILOG_FILES = '+' '.join(map(str,rtl))+'\n'
            config+='export VERILOG_INCLUDE_DIRS = '+' '.join(sorted({str(p.parent) for p in (directory/'inputs').rglob('*') if p.is_file()}))+'\n'
            config+=f'export SDC_FILE = {sdc}\nexport CORE_UTILIZATION = 40\nexport PLACE_DENSITY_LB_ADDON = 0.20\n'
            if spec['platform']=='nangate45': config+=f'export PDN_TCL = {self.flow}/designs/nangate45/gcd/grid_strategy-M1-M4-M7.tcl\n'
        (directory/'config.mk').write_text(config)
        state={'job_id':jid,'request':request,'project':spec,'status':'queued','created_at':time.time(),'completed_stages':[],
               'stages':STAGES[:STAGES.index(through)+1],'current_stage':None,'artifacts_ready':False}
        dump(directory/'state.json',state)
        threading.Thread(target=self._run,args=(directory,),daemon=True).start()
        return {'job_id':jid,'status':'queued','reused':False}

    def _save(self,directory,**fields):
        state=json.loads((directory/'state.json').read_text());state.update(fields);dump(directory/'state.json',state)
        return state

    def _run(self,directory):
        jid=directory.name
        try:
            with self.lock: state=self._save(directory,status='running',started_at=time.time())
            deadline=time.monotonic()+state['request']['timeout_seconds']
            status='succeeded';error=None
            for stage in state['stages']:
                with self.lock:
                    current=json.loads((directory/'state.json').read_text())
                    if current.get('cancel_requested'): status='cancelled';break
                    self._save(directory,current_stage=stage)
                    command=['make','--no-print-directory',f'DESIGN_CONFIG={directory}/config.mk',f'WORK_HOME={directory}/work',
                             f'NUM_CORES={state["request"]["cores"]}',stage]
                    with (directory/'flow.log').open('ab') as output:
                        output.write(f'\n[Nexus EDA] Starting {stage}\n'.encode());output.flush()
                        process=subprocess.Popen(command,cwd=self.flow,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
                    self.processes[jid]=process
                while process.poll() is None:
                    time.sleep(.25)
                    with self.lock: cancelled=json.loads((directory/'state.json').read_text()).get('cancel_requested')
                    if cancelled or time.monotonic()>deadline:
                        status='cancelled' if cancelled else 'timed_out';self._kill(process);break
                    if (directory/'flow.log').stat().st_size>128*1024*1024 or shutil.disk_usage(directory).free<1024**3:
                        status='failed';error='Output or disk quota exceeded';self._kill(process);break
                code=process.wait()
                with self.lock:
                    self.processes.pop(jid,None)
                    if status!='succeeded':break
                    if code!=0: status='failed';error=f'{stage} exited with code {code}';break
                    current=json.loads((directory/'state.json').read_text())
                    self._save(directory,completed_stages=current['completed_stages']+[stage])
            with self.lock:self._save(directory,status='finalizing',exit_status=status,error=error)
            self._artifacts(directory)
            with self.lock:self._save(directory,status=status,ended_at=time.time(),artifacts_ready=True)
        except Exception as exc:
            with self.lock:
                process=self.processes.pop(jid,None)
                if process:self._kill(process)
                self._save(directory,status='failed',error=str(exc)[:500],ended_at=time.time())

    @staticmethod
    def _kill(process):
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:return
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.wait(timeout=5)

    def get_eda_job(self,job_id,log_offset=0,limit=4096):
        directory=self.directory('jobs',job_id)
        integer(log_offset,0,2**40);integer(limit,256,16384)
        state=json.loads((directory/'state.json').read_text())
        log=directory/'flow.log';size=log.stat().st_size if log.exists() else 0
        if log_offset>size:raise ValueError('log_offset exceeds log size')
        raw=b''
        if log.exists():
            with log.open('rb') as f:f.seek(log_offset);raw=f.read(limit)
        return {**state,'log':raw.decode('utf-8',errors='replace'),'log_offset':log_offset,'next_offset':log_offset+len(raw),'log_bytes':size,
                'elapsed_seconds':round((state.get('ended_at') or time.time())-state.get('started_at',state['created_at']),2),
                'progress':{'completed':len(state['completed_stages']),'total':len(state['stages']),'unit':'completed stages'}}

    def cancel_eda_job(self,job_id):
        directory=self.directory('jobs',job_id)
        state=json.loads((directory/'state.json').read_text())
        if state['status'] in ('queued','running','cancelling'):
            state=self._save(directory,cancel_requested=True,status='cancelling')
        return {'job_id':job_id,'status':state['status']}

    def _artifacts(self,directory):
        paths=[]
        for name in ('inputs','work/results','work/reports','work/logs'):
            for path in sorted((directory/name).rglob('*')):
                if path.is_file() and not any(p.is_symlink() for p in [path,*path.parents]) and path.resolve().is_relative_to(directory):paths.append(path)
        for name in ('flow.log','config.mk'):
            if (directory/name).is_file():paths.append(directory/name)
        if sum(p.stat().st_size for p in paths)>2*1024**3:raise ValueError('Artifacts exceed 2 GiB packaging quota')
        archive=directory/'artifacts.zip'
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as bundle:
            for path in paths:bundle.write(path,path.relative_to(directory).as_posix())
        paths.append(archive)
        manifest=[{'artifact_id':hashlib.sha256(p.relative_to(directory).as_posix().encode()).hexdigest()[:24],
                   'path':p.relative_to(directory).as_posix(),'size':p.stat().st_size,'sha256':digest(p)} for p in paths]
        dump(directory/'artifacts.json',manifest)
        if (directory/'state.json').exists():
            with self.lock:self._save(directory,artifacts_ready=True)

    def list_eda_artifacts(self,job_id,offset=0,limit=50):
        directory=self.directory('jobs',job_id);integer(offset,0,1000000);integer(limit,1,100)
        if not json.loads((directory/'state.json').read_text()).get('artifacts_ready'):return {'ready':False,'artifacts':[]}
        manifest=json.loads((directory/'artifacts.json').read_text());end=min(offset+limit,len(manifest))
        return {'ready':True,'artifacts':manifest[offset:end],'next_offset':end,'eof':end==len(manifest),'total':len(manifest)}

    def read_eda_artifact(self,job_id,artifact_id,offset=0,limit=262144):
        directory=self.directory('jobs',job_id);integer(offset,0,2**40);integer(limit,1,262144)
        if not json.loads((directory/'state.json').read_text()).get('artifacts_ready'):raise ValueError('Artifacts are not finalized yet')
        entries=json.loads((directory/'artifacts.json').read_text())
        entry=next((e for e in entries if e['artifact_id']==artifact_id),None)
        if not entry:raise ValueError('Unknown artifact_id')
        path=directory/entry['path']
        if any(p.is_symlink() for p in [path,*path.parents]) or not path.resolve().is_relative_to(directory):raise ValueError('Unsafe artifact path')
        if path.stat().st_size!=entry['size'] or offset>entry['size']:raise ValueError('Artifact changed or offset is invalid')
        with path.open('rb') as f:f.seek(offset);raw=f.read(limit)
        return {**entry,'offset':offset,'next_offset':offset+len(raw),'eof':offset+len(raw)==entry['size'],'data_base64':base64.b64encode(raw).decode()}
