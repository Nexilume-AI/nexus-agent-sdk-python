"""Per-caller AccelForge worker. Native YAML is evaluated only in its isolated container."""
import base64,hashlib,json,os,re,resource,signal,subprocess,sys,threading,time,uuid,shutil
from pathlib import Path
ROOT=Path('/work');EXAMPLES=Path('/opt/accelforge/examples');JOBS={};LOCK=threading.RLock()
ROOT.joinpath('inputs').mkdir(exist_ok=True)
for directory in ('/tmp/accelforge-cacti','/tmp/accelforge-neurosim'):Path(directory).mkdir(exist_ok=True)
def tool(name,desc,props=None,required=()):
 return {'name':name,'description':desc,'inputSchema':{'type':'object','properties':props or {},'required':list(required),'additionalProperties':False}}
S={'type':'string'}
TOOLS=[
 tool('list_examples','List bundled upstream architecture, workload and mapping YAML files. Select paths with example: prefix. Examples are research models; inspect assumptions before interpreting results.'),
 tool('read_example','Read an official YAML example as UTF-8 text in pages of 16000 characters. Use its relative path from list_examples.',{'path':S,'offset':{'type':'integer','minimum':0,'default':0}},('path',)),
 tool('upload_spec','Upload a custom YAML specification as base64 chunks of at most1024 decoded bytes. Simple .yaml/.yml filename, sequential byte offset,1MiB/file,32files/workspace. Existing identical chunks may be retried. Select uploaded inputs using file:filename.yaml.',{'filename':S,'data_base64':S,'offset':{'type':'integer','minimum':0}},('filename','data_base64','offset')),
 tool('start_analysis','Start native AccelForge in the background: map finds mappings; evaluate assesses a supplied mapping; costs computes component costs; validate parses schema and expressions. Returns job_id. Defaults use simple architecture and a single32x32 matmul. Use example:relative.yaml or file:uploaded.yaml selectors. Parameters are Jinja scalar variables. Read job_status, then download summary.json/results.csv/results.json/mapping.yaml/mapping.svg/logs. Results depend on input model units and assumptions; simple example uses synthetic costs.',{
 'mode':{'type':'string','enum':['map','evaluate','costs','validate'],'default':'map'},
 'architecture':{'type':'string','default':'example:arches/simple.yaml'},
 'workload':{'type':'string','default':'example:workloads/basic/matmuls_any_einsums.yaml'},
 'mapping':{'type':'string','description':'Required for evaluate. Omit for map. Example: example:mappings/unfused_matmuls_to_simple.yaml with workload basic/matmuls.yaml.'},
 'parameters':{'type':'object','additionalProperties':{'type':['string','number','boolean','null']},'description':'Jinja scalar overrides, e.g. {N_EINSUMS:1,M:32,KN:32}. Defaults are merged; max32 entries,16KiB JSON.'},
 'objective':{'type':'string','enum':['energy','latency','energy_latency'],'default':'energy'},
 'timeout_seconds':{'type':'integer','minimum':1,'maximum':900,'default':300}}),
 tool('job_status','Read state, elapsed time, live log tail and completed summary. States: running/succeeded/failed/cancelled/timed_out. No invented progress percentage.',{'job_id':S},('job_id',)),
 tool('cancel_analysis','Cancel an analysis in your workspace and retain its logs and partial outputs.',{'job_id':S},('job_id',)),
 tool('list_artifacts','List your uploaded specifications and job outputs. Temporary files expire after30minutes idle, closure or restart. Download important results first.'),
 tool('read_artifact','Download a workspace file in base64 pages of24576bytes. Concatenate until eof and verify SHA256; use completed files for stable checksums.',{'path':S,'offset':{'type':'integer','minimum':0,'default':0}},('path',)),
]
def example_path(value):
 if not isinstance(value,str) or len(value)>240:raise ValueError('Invalid example path')
 p=(EXAMPLES/value).resolve()
 if not p.is_relative_to(EXAMPLES) or not p.is_file() or p.suffix not in ('.yaml','.yml'):raise ValueError('Unknown YAML example')
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
    if name=='list_examples':return {'files':[str(p.relative_to(EXAMPLES)) for p in sorted(EXAMPLES.rglob('*.yaml'))], 'selector_prefixes':['example:','file:'],'default_parameters':{'N_EINSUMS':1,'M':32,'KN':32}}
    if name=='read_example':
        p=example_path(a['path']);text=p.read_text();offset=a.get('offset',0)
        if type(offset)!=int or not 0<=offset<=len(text):raise ValueError('Invalid character offset')
        part=text[offset:offset+16000]
        return {'path':a['path'],'text':part,'next_offset':offset+len(part),'eof':offset+len(part)==len(text)}
    if name=='upload_spec':
        filename=a['filename']
        if not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,90}\.ya?ml',filename):raise ValueError('Use a simple .yaml or .yml filename')
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
    if name=='start_analysis':
        if any(j['state']=='running' for j in JOBS.values()):raise ValueError('WORKSPACE_BUSY')
        if len(JOBS)>=16:raise ValueError('JOB_LIMIT; create a new workspace')
        mode=a.get('mode','map');objective=a.get('objective','energy');seconds=a.get('timeout_seconds',300)
        if mode not in ('map','evaluate','costs','validate') or objective not in ('energy','latency','energy_latency'):raise ValueError('Invalid mode or objective')
        if type(seconds)!=int or not 1<=seconds<=900:raise ValueError('Invalid timeout')
        parameters=a.get('parameters',{})
        if not isinstance(parameters,dict) or len(parameters)>32 or len(json.dumps(parameters))>16384 or any(not isinstance(k,str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,63}',k) or type(v) not in (str,int,float,bool,type(None)) for k,v in parameters.items()):raise ValueError('Parameters must be bounded scalar Jinja variables')
        config={'mode':mode,'objective':objective,'parameters':{'N_EINSUMS':1,'M':32,'KN':32,**parameters},'timeout_seconds':seconds}
        selected={}
        for kind,default in [('architecture','example:arches/simple.yaml'),('workload','example:workloads/basic/matmuls_any_einsums.yaml'),('mapping',None)]:
            selector=a.get(kind,default)
            if selector is None:
                if kind=='mapping' and mode=='evaluate':raise ValueError('evaluate requires a mapping selector')
                continue
            if not isinstance(selector,str):raise ValueError('Invalid selector')
            if selector.startswith('example:'):p=example_path(selector[8:])
            elif selector.startswith('file:'):
                p=safe_path('inputs/'+selector[5:])
                if p.parent!=ROOT/'inputs' or not p.is_file() or p.suffix not in ('.yaml','.yml'):raise ValueError('Uploaded YAML not found')
            else:raise ValueError('Use example:relative.yaml or file:uploaded.yaml')
            if p.stat().st_size>1048576:raise ValueError('YAML too large')
            selected[kind]=(selector,p)
        jid=uuid.uuid4().hex;folder=ROOT/'jobs'/jid;folder.mkdir(parents=True)
        shutil.copytree(ROOT/'inputs',folder/'inputs')
        config['files']=[];config['selectors']={k:v[0] for k,v in selected.items()}
        for kind,(selector,p) in selected.items():
            path=p if selector.startswith('example:') else folder/'inputs'/p.name
            config['files'].append(str(path))
            (folder/(kind+'.source.yaml')).write_bytes(p.read_bytes())
        (folder/'config.json').write_text(json.dumps(config,indent=2))
        with (folder/'run.log').open('wb') as log:
            proc=subprocess.Popen(['/opt/venv/bin/python','/opt/accelforge-bridge/runner.py',str(folder/'config.json')],cwd=folder,stdout=log,stderr=log,start_new_session=True,preexec_fn=limits)
        job={'id':jid,'folder':folder,'process':proc,'state':'running','started':time.monotonic()};JOBS[jid]=job
        threading.Thread(target=finish,args=(job,seconds),daemon=True).start()
        return {'job_id':jid,'state':'running','timeout_seconds':seconds,'next_step':'job_status'}
    if name in ('job_status','cancel_analysis'):
        job=JOBS.get(a.get('job_id'))
        if not job:raise ValueError('Job not found in this workspace')
        if name=='cancel_analysis':kill(job,'cancelled')
        with LOCK:
            state=job['state'];elapsed=job.get('ended',time.monotonic())-job['started']
        with (job['folder']/'run.log').open('rb') as f:f.seek(max(0,os.fstat(f.fileno()).st_size-12000));tail=f.read().decode(errors='replace')
        result={'job_id':job['id'],'state':state,'exit_code':job['process'].poll(),'elapsed_seconds':round(elapsed,3),'log_tail':tail}
        p=job['folder']/'summary.json'
        if state=='succeeded' and p.exists():result['summary']=json.loads(p.read_text())
        return result
    if name=='list_artifacts':return {'files':[{'path':str(p.relative_to(ROOT)),'size':p.stat().st_size} for p in sorted(ROOT.rglob('*')) if p.is_file()]}
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
        if method=='initialize':value={'protocolVersion':'2025-03-26','capabilities':{'tools':{}},'serverInfo':{'name':'accelforge','version':'7312a7d91562'}}
        elif method=='tools/list':value={'tools':TOOLS}
        elif method=='tools/call':
            try:
                value=call(msg['params']['name'],msg['params'].get('arguments',{}));value={'content':[{'type':'text','text':json.dumps(value)}],'structuredContent':value}
            except Exception as exc:
                error={'error':'ACCELFORGE_TOOL_FAILED','message':str(exc)[:1000]}
                value={'content':[{'type':'text','text':json.dumps(error)}],'structuredContent':error,'isError':True}
        else:raise ValueError('Unknown method')
        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':value}),flush=True)
    except Exception:
        print(json.dumps({'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid request'}}),flush=True)
