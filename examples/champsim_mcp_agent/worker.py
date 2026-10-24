"""Networkless per-caller worker; background jobs and bounded file transfer."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import signal
import subprocess
import sys
import threading
import time
import uuid
import struct, gzip, lzma, bz2
PRESETS=json.loads(Path("/opt/champsim/configs.json").read_text())

ROOT=Path('/work');JOBS={};LOCK=threading.RLock()
ROOT.joinpath('traces').mkdir(exist_ok=True)
RECORD=struct.Struct('<QBB2B4B2Q4Q')
sample=ROOT/'traces/sample.champsimtrace'
with sample.open('wb') as f:
    for i in range(4096):
        branch=int(i%16==15)
        f.write(RECORD.pack(0x400000+(i%256)*4,branch,int(branch and i%32==15),26 if branch else 1,0,
            25 if branch else 2,0,0,0,0x200000+(i%512)*64 if i%8==0 else 0,0,
            0x100000+(i%512)*64 if i%4==0 else 0,0,0,0))


def tool(name,desc,props=None,required=()):
    return {'name':name,'description':desc,'inputSchema':{'type':'object','properties':props or {},'required':list(required),'additionalProperties':False}}
S={'type':'string'}
TOOLS=[
 tool('list_simulation_presets','List the three prebuilt official ChampSim module configurations. Includes exact source JSON, trace format, synthetic sample description and metric definitions.'),
 tool('upload_trace','Upload ChampSim standard 64-byte little-endian binary input_instr records, optionally .xz/.gz/.bz2 compressed. Base64 chunks <=1024 decoded bytes, maximum64MiB compressed and256MiB expanded. Idempotent offsets; no URLs, CloudSuite traces or arbitrary code.',{'filename':S,'data_base64':S,'offset':{'type':'integer','minimum':0}},('filename','data_base64','offset')),
 tool('start_simulation','Start a background ChampSim run. Default uses a synthetic sample, not a real benchmark. Returns job_id immediately. Inspect job_status then download stats.json/run.log/config.json with read_artifact. One running job/workspace,16 retained jobs,timeout<=600s. Trace repeats if shorter than requested instructions.',
      {'trace':{'type':'string','default':'sample.champsimtrace'},'preset':{'type':'string','enum':['baseline','next-line','perceptron'],'default':'baseline'},
       'warmup_instructions':{'type':'integer','minimum':0,'maximum':10000000,'default':1000},
       'simulation_instructions':{'type':'integer','minimum':100,'maximum':100000000,'default':10000},
       'timeout_seconds':{'type':'integer','minimum':1,'maximum':600,'default':120}}),
 tool('job_status','Get execution state, elapsed time, actual heartbeat log tail and completed JSON statistics. No estimated percentage. States: running/succeeded/failed/cancelled/timed_out.',{'job_id':S},('job_id',)),
 tool('cancel_simulation','Terminate your workspace simulation and preserve logs and artifacts.',{'job_id':S},('job_id',)),
 tool('list_artifacts','List this workspace trace and job files. Download before30minute idle expiry, closure or service restart.'),
 tool('read_artifact','Download file in base64 pages of24576bytes. Concatenate until eof, verify complete SHA256. Use completed files for stable checksums.',{'path':S,'offset':{'type':'integer','minimum':0,'default':0}},('path',)),
]

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
    if name=='list_simulation_presets':return {'presets':PRESETS,'trace_format':'64-byte little-endian input_instr; optional xz/gz/bz2','sample':'Synthetic deterministic 4096-instruction loop; not benchmark evidence','metrics':'IPC=retired instructions/cycles in simulation phase; warmup excluded'}
    if name=='upload_trace':
        filename=a['filename']
        if not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',filename):raise ValueError('Use a simple trace filename')
        p=safe_path('traces/'+filename);offset=a['offset']
        if not p.exists() and len(list((ROOT/'traces').iterdir()))>=64:raise ValueError('Trace file limit reached')
        if type(offset)!=int or not 0<=offset<=64*1024*1024:raise ValueError('Invalid offset')
        data=base64.b64decode(a['data_base64'],validate=True)
        if not 0<len(data)<=1024 or offset+len(data)>64*1024*1024:raise ValueError('Chunk or trace too large')
        size=p.stat().st_size if p.exists() else 0
        if p.exists() and offset<size:
            with p.open('rb') as f:f.seek(offset);previous=f.read(len(data))
            if previous!=data:raise ValueError('Offset already contains different data; choose another filename')
        elif offset==size:
            with p.open('ab') as f:f.write(data)
        else:raise ValueError('Offset must equal current file size')
        return {'path':'traces/'+filename,'next_offset':offset+len(data),'size':p.stat().st_size}
    if name=='start_simulation':
        if any(j['state']=='running' for j in JOBS.values()):raise ValueError('WORKSPACE_BUSY')
        if len(JOBS)>=16:raise ValueError('JOB_LIMIT; create a new workspace')
        preset=a.get('preset','baseline')
        if preset not in {p['executable_name'] for p in PRESETS}:raise ValueError('Unknown preset')
        warmup=a.get('warmup_instructions',1000);instructions=a.get('simulation_instructions',10000);seconds=a.get('timeout_seconds',120)
        if type(warmup)!=int or not 0<=warmup<=10000000:raise ValueError('Invalid warmup instruction count')
        if type(instructions)!=int or not 100<=instructions<=100000000:raise ValueError('Invalid simulation instruction count')
        if type(seconds)!=int or not 1<=seconds<=600:raise ValueError('Invalid timeout')
        filename=a.get('trace','sample.champsimtrace');path=safe_path('traces/'+filename)
        if path.parent!=ROOT/'traces' or not path.is_file() or path.stat().st_size>64*1024*1024:raise ValueError('Trace not found or too large')
        jid=uuid.uuid4().hex;folder=ROOT/'jobs'/jid;folder.mkdir(parents=True)
        # Expand once with a hard byte cap. The simulator only sees validated raw records.
        opener={'.xz':lzma.open,'.gz':gzip.open,'.bz2':bz2.open}.get(path.suffix,open)
        total=0
        try:
            with opener(path,'rb') as f,(folder/'input.champsimtrace').open('wb') as out:
                while chunk:=f.read(65536):
                    total+=len(chunk)
                    if total>256*1024*1024:raise ValueError('Expanded trace exceeds256MiB')
                    out.write(chunk)
            if total<128 or total%64:raise ValueError('Trace must contain at least2 complete64-byte records; CloudSuite format unsupported')
        except Exception:
            (folder/'input.champsimtrace').unlink(missing_ok=True);folder.rmdir();raise
        config={'preset':preset,'configuration':next(p for p in PRESETS if p['executable_name']==preset),'trace':filename,'trace_records':total//64,
                'synthetic_sample':filename=='sample.champsimtrace','warmup_instructions':warmup,'simulation_instructions':instructions}
        (folder/'config.json').write_text(json.dumps(config,indent=2))
        command=['/opt/champsim/bin/'+preset,'--warmup-instructions',str(warmup),'--simulation-instructions',str(instructions),
                 '--json',str(folder/'stats.json'),str(folder/'input.champsimtrace')]
        with (folder/'run.log').open('wb') as log:
            proc=subprocess.Popen(command,cwd=folder,stdout=log,stderr=log,start_new_session=True,preexec_fn=limits)
        job={'id':jid,'folder':folder,'process':proc,'state':'running','started':time.monotonic()};JOBS[jid]=job
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
        p=job['folder']/'stats.json'
        if state=='succeeded' and p.exists():result['stats']=json.loads(p.read_text())
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
        if method=='initialize':value={'protocolVersion':'2025-03-26','capabilities':{'tools':{}},'serverInfo':{'name':'champsim','version':'410cee62d9c4'}}
        elif method=='tools/list':value={'tools':TOOLS}
        elif method=='tools/call':
            try:
                value=call(msg['params']['name'],msg['params'].get('arguments',{}));value={'content':[{'type':'text','text':json.dumps(value)}],'structuredContent':value}
            except Exception as exc:
                error={'error':'CHAMPSIM_TOOL_FAILED','message':str(exc)[:1000]}
                value={'content':[{'type':'text','text':json.dumps(error)}],'structuredContent':error,'isError':True}
        else:raise ValueError('Unknown method')
        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':value}),flush=True)
    except Exception:
        print(json.dumps({'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid request'}}),flush=True)
