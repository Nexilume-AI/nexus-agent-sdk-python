"""SST configurations run only inside a per-caller, networkless container."""
import base64,hashlib,json,os,re,resource,signal,subprocess,sys,threading,time,uuid,shutil
from pathlib import Path
from decimal import Decimal
ROOT=Path('/work');EXAMPLES=Path('/opt/nexus/examples');JOBS={};LOCK=threading.RLock()
ROOT.joinpath('inputs').mkdir(exist_ok=True)
LIBRARIES=['simpleElementExample','simpleSimulation','memHierarchy','miranda','merlin','cassini']
def tool(name,desc,props=None,required=()):
 return {'name':name,'description':desc,'inputSchema':{'type':'object','properties':props or {},'required':list(required),'additionalProperties':False}}
S={'type':'string'}
TOOLS=[
 tool('list_examples','List bundled runnable SST Python models and installed Elements libraries. The small examples demonstrate behavior, not validated hardware predictions.'),
 tool('read_example','Read a bundled Python model as UTF-8 text, 16000 characters per page.',{'path':S,'offset':{'type':'integer','minimum':0,'default':0}},('path',)),
 tool('component_info','Inspect installed SST component parameters, ports, statistics and subcomponents using native sst-info. Select a library or library.component, then page the text.',{'component':{'type':'string','default':'memHierarchy'},'offset':{'type':'integer','minimum':0,'default':0}}),
 tool('upload_model','Upload a Python SST configuration or supporting data as base64 chunks <=1024 decoded bytes. Simple filename; 1MiB/file,32files. Sequential byte offsets with identical retry support. Python executes in your isolated container. Select uploaded entrypoint as file:filename.py.',{'filename':S,'data_base64':S,'offset':{'type':'integer','minimum':0}},('filename','data_base64','offset')),
 tool('start_simulation','Run SST Core 16 with bundled Elements in background, single MPI rank and 1 or 2 threads. model is example:events.py, example:memory.py, example:network.py or file:uploaded.py. All uploaded helper files are copied into the job directory. Return job_id immediately; use job_status and artifact downloads. Python model controls SST statistics output. No network, external plugins or host files. timeout_seconds is wall time; stop_at is simulated time.',{
 'model':{'type':'string','default':'example:events.py'},'threads':{'type':'integer','enum':[1,2],'default':1},
 'stop_at':{'type':'string','default':'1ms','description':'Maximum simulated duration, e.g. 1ms, 100us, 20ns. Allowed fs/ps/ns/us/ms/s.'},
 'timeout_seconds':{'type':'integer','minimum':1,'maximum':900,'default':120}}),
 tool('job_status','Read actual execution state, elapsed wall time, log tail and simulation summary. No estimated percentage. A successful exit may be stopped by stop_at before model completion; inspect stopped_at_limit and logs.',{'job_id':S},('job_id',)),
 tool('cancel_simulation','Cancel your simulation process group and preserve its partial artifacts.',{'job_id':S},('job_id',)),
 tool('list_artifacts','List uploaded files, frozen model.py/config.json, run.log, stats.csv and other model-produced artifacts. Download before workspace expires (30 minutes idle), closes or service restarts.'),
 tool('read_artifact','Download base64 pages of24576bytes. Concatenate until eof, verify SHA256. Completed files have stable checksums.',{'path':S,'offset':{'type':'integer','minimum':0,'default':0}},('path',)),
]
def example_path(value):
 if not isinstance(value,str) or len(value)>240:raise ValueError('Invalid example path')
 p=(EXAMPLES/value).resolve()
 if not p.is_relative_to(EXAMPLES) or not p.is_file() or p.suffix not in ('.py',):raise ValueError('Unknown Python example')
 return p

def safe_path(text):
    if not isinstance(text,str) or not text or len(text)>240:raise ValueError('Invalid path')
    p=(ROOT/text).resolve()
    if not p.is_relative_to(ROOT) or p==ROOT:raise ValueError('Path outside workspace')
    return p

def kill(job,state):
    with LOCK:
        if job['process'].poll() is None:
            job['forced']=state
            try:os.killpg(job['process'].pid,signal.SIGKILL)
            except ProcessLookupError:pass

def finish(job,seconds):
    try:job['process'].wait(timeout=seconds)
    except subprocess.TimeoutExpired:kill(job,'timed_out');job['process'].wait()
    with LOCK:
        job['ended']=time.monotonic();job['state']=job.get('forced') or ('succeeded' if job['process'].returncode==0 else 'failed')

def limits():
    resource.setrlimit(resource.RLIMIT_FSIZE,(64*1024*1024,64*1024*1024))
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))

def call(name,a):
    if name=='list_examples':return {'files':[p.name for p in sorted(EXAMPLES.glob('*.py'))],'libraries':LIBRARIES,'core':'16.0.0','elements':'16.0.0','selector_prefixes':['example:','file:']}
    if name=='component_info':
        component=a.get('component','memHierarchy')
        if not isinstance(component,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)?',component) or component.split('.')[0] not in LIBRARIES:raise ValueError('Select an installed library or library.component')
        r=subprocess.run(['/opt/sst/bin/sst-info',component],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=15)
        text=r.stdout;offset=a.get('offset',0)
        if type(offset)!=int or not 0<=offset<=len(text):raise ValueError('Invalid character offset')
        part=text[offset:offset+16000]
        return {'text':part,'next_offset':offset+len(part),'eof':offset+len(part)==len(text),'exit_code':r.returncode}
    if name=='read_example':
        p=example_path(a['path']);text=p.read_text();offset=a.get('offset',0)
        if type(offset)!=int or not 0<=offset<=len(text):raise ValueError('Invalid character offset')
        part=text[offset:offset+16000]
        return {'path':a['path'],'text':part,'next_offset':offset+len(part),'eof':offset+len(part)==len(text)}
    if name=='upload_model':
        filename=a['filename']
        if not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',filename):raise ValueError('Use a simple filename')
        p=safe_path('inputs/'+filename);offset=a['offset']
        if not p.exists() and len(list((ROOT/'inputs').iterdir()))>=32:raise ValueError('File count limit reached')
        if type(offset)!=int or not 0<=offset<=1048576:raise ValueError('Invalid offset')
        data=base64.b64decode(a['data_base64'],validate=True)
        if not 0<len(data)<=1024 or offset+len(data)>1048576:raise ValueError('Chunk or file too large')
        size=p.stat().st_size if p.exists() else 0
        if p.exists() and offset<size:
            with p.open('rb') as f:f.seek(offset);previous=f.read(len(data))
            if previous!=data:raise ValueError('Offset already contains different data; choose another filename')
        elif offset==size:
            with p.open('ab') as f:f.write(data)
        else:raise ValueError('Offset must equal current file size')
        return {'path':'inputs/'+filename,'selector':'file:'+filename,'next_offset':offset+len(data),'size':p.stat().st_size}
    if name=='start_simulation':
        if any(j['state']=='running' for j in JOBS.values()):raise ValueError('WORKSPACE_BUSY')
        if len(JOBS)>=16:raise ValueError('JOB_LIMIT; create a new workspace')
        seconds=a.get('timeout_seconds',120);threads=a.get('threads',1);stop=a.get('stop_at','1ms');selector=a.get('model','example:events.py')
        if type(seconds)!=int or not 1<=seconds<=900:raise ValueError('Invalid timeout')
        if type(threads)!=int or threads not in (1,2):raise ValueError('Invalid thread count')
        if not isinstance(stop,str) or len(stop)>24 or not re.fullmatch(r'[1-9][0-9]*(?:\.[0-9]+)?(?:fs|ps|ns|us|ms|s)',stop):raise ValueError('Invalid stop_at')
        if not isinstance(selector,str):raise ValueError('Invalid model')
        if selector.startswith('example:'):p=example_path(selector[8:])
        elif selector.startswith('file:'):
            p=safe_path('inputs/'+selector[5:])
            if p.parent!=ROOT/'inputs' or not p.is_file() or p.suffix!='.py':raise ValueError('Uploaded .py model not found')
        else:raise ValueError('Use example:filename.py or file:filename.py')
        if p.stat().st_size>1048576:raise ValueError('Model too large')
        jid=uuid.uuid4().hex;folder=ROOT/'jobs'/jid;folder.mkdir(parents=True)
        for inp in (ROOT/'inputs').iterdir():
            resolved=inp.resolve()
            if resolved.is_relative_to(ROOT/'inputs') and resolved.is_file():shutil.copyfile(resolved,folder/inp.name)
        shutil.copyfile(p,folder/'model.py')
        config={'model':selector,'threads':threads,'stop_at':stop,'timeout_seconds':seconds,'core':'16.0.0','elements':'16.0.0'}
        (folder/'config.json').write_text(json.dumps(config,indent=2))
        command=['/opt/sst/bin/sst','--num-threads='+str(threads),'--stop-at='+stop,'model.py']
        with (folder/'run.log').open('wb') as log:
            proc=subprocess.Popen(command,cwd=folder,stdout=log,stderr=log,start_new_session=True,preexec_fn=limits)
        job={'id':jid,'folder':folder,'process':proc,'stop_at':stop,'state':'running','started':time.monotonic()};JOBS[jid]=job
        threading.Thread(target=finish,args=(job,seconds),daemon=True).start()
        return {'job_id':jid,'state':'running','timeout_seconds':seconds,'next_step':'job_status'}
    if name in ('job_status','cancel_simulation'):
        job=JOBS.get(a.get('job_id'))
        if not job:raise ValueError('Job not found in this workspace')
        if name=='cancel_simulation':kill(job,'cancelled')
        with LOCK:
            state=job['state'];elapsed=job.get('ended',time.monotonic())-job['started']
        with (job['folder']/'run.log').open('rb') as f:f.seek(max(0,os.fstat(f.fileno()).st_size-12000));tail=f.read().decode(errors='replace')
        result={'job_id':job['id'],'state':state,'exit_code':job['process'].poll(),'elapsed_seconds':round(elapsed,3),'log_tail':tail}
        if state!='running':
            matches=re.findall(r'Simulation is complete, simulated time: (.+)',tail)
            simulated=matches[-1] if matches else None
            def seconds(value):
                m=re.fullmatch(r'([0-9.eE+-]+)\s*(fs|ps|ns|us|ms|s)',value or '')
                return Decimal(m[1])*Decimal({'fs':'1e-15','ps':'1e-12','ns':'1e-9','us':'1e-6','ms':'1e-3','s':'1'}[m[2]]) if m else None
            actual=seconds(simulated);limit=seconds(job['stop_at'])
            result['summary']={'simulated_time':simulated,'stopped_at_limit':actual is not None and limit is not None and actual>=limit,'exit_code':job['process'].returncode}
            (job['folder']/'summary.json').write_text(json.dumps(result['summary'],indent=2))
        return result
    if name=='list_artifacts':return {'files':[{'path':str(p.relative_to(ROOT)),'size':p.stat().st_size} for p in sorted(ROOT.rglob('*')) if p.is_file() and p.resolve().is_relative_to(ROOT)]}
    if name=='read_artifact':
        p=safe_path(a['path']);offset=a.get('offset',0)
        if not p.is_file() or type(offset)!=int or not 0<=offset<=p.stat().st_size:raise ValueError('File or offset invalid')
        sha=hashlib.sha256()
        with p.open('rb') as f:
            while chunk:=f.read(65536):sha.update(chunk)
            f.seek(offset);data=f.read(24576)
        return {'data_base64':base64.b64encode(data).decode(),'next_offset':offset+len(data),'eof':offset+len(data)>=p.stat().st_size,'sha256':sha.hexdigest()}
    raise ValueError('Unknown tool')

for line in sys.stdin:
    try:
        msg=json.loads(line)
        if 'id' not in msg:continue
        method=msg.get('method')
        if method=='initialize':value={'protocolVersion':'2025-03-26','capabilities':{'tools':{}},'serverInfo':{'name':'sst','version':'16.0.0'}}
        elif method=='tools/list':value={'tools':TOOLS}
        elif method=='tools/call':
            try:
                value=call(msg['params']['name'],msg['params'].get('arguments',{}));value={'content':[{'type':'text','text':json.dumps(value)}],'structuredContent':value}
            except Exception as exc:
                error={'error':'SST_TOOL_FAILED','message':str(exc)[:1000]}
                value={'content':[{'type':'text','text':json.dumps(error)}],'structuredContent':error,'isError':True}
        else:raise ValueError('Unknown method')
        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':value}),flush=True)
    except Exception:
        print(json.dumps({'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid request'}}),flush=True)
