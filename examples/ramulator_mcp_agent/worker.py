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
from runner import presets, CONTROLLERS

ROOT=Path('/work');JOBS={};LOCK=threading.RLock()
ROOT.joinpath('traces').mkdir(exist_ok=True)
ROOT.joinpath('traces/sample.trace').write_text('LD 0x0\nLD 0x40\nST 0x80\nLD 0x1000\nLD 0x0\nLD 0x1040\n')

def tool(name,desc,props=None,required=()):
    return {'name':name,'description':desc,'inputSchema':{'type':'object','properties':props or {},'required':list(required),'additionalProperties':False}}
S={'type':'string'}
TOOLS=[
 tool('list_dram_presets','List installed DRAM standards, organization and timing presets. Pass the exact matching preset names to start_simulation.'),
 tool('upload_trace','Upload UTF-8 LoadStoreTrace text as base64 chunks (up to 1024 decoded bytes). Lines: LD 0xADDRESS or ST DECIMAL_ADDRESS. First offset=0 creates the trace; subsequent offsets append. Identical retries are idempotent. Max 16 MiB per trace.',{'filename':S,'data_base64':S,'offset':{'type':'integer','minimum':0}},('filename','data_base64','offset')),
 tool('start_simulation','Start a real Ramulator simulation in the background. Returns job_id immediately. Default DDR4 sample works without uploads. Use job_status for completion/logs, read_artifact for stats/config/log files. One running job/workspace; max 16 retained jobs. Timeout 1..600 seconds. No percentage estimate is available.',
      {'trace':{'type':'string','default':'sample.trace'},'standard':{'type':'string','enum':list(CONTROLLERS),'default':'DDR4'},
       'organization':{'type':'string','default':'DDR4_8Gb_x8'},'timing':{'type':'string','default':'DDR4_2400R'},
       'channels':{'type':'integer','minimum':1,'maximum':8,'default':1},
       'overrides':{'type':'object','description':'DRAM numeric overrides, e.g. {"rank":2}. Keys validated by upstream.', 'additionalProperties':{'type':'integer','minimum':1,'maximum':1000000}},
       'timeout_seconds':{'type':'integer','minimum':1,'maximum':600,'default':120}}),
 tool('job_status','Get job state, exit code, elapsed seconds, bounded log tail and completed statistics. States: running, succeeded, failed, cancelled, timed_out. Actual statistics are available only on success.',{'job_id':S},('job_id',)),
 tool('cancel_simulation','Stop this workspace job and its process group. Preserves existing logs and artifacts.',{'job_id':S},('job_id',)),
 tool('list_artifacts','List uploaded traces and job artifacts with byte sizes; files expire when the workspace closes.'),
 tool('read_artifact','Download a file in base64 pages of 24576 bytes. Concatenate until eof; verify complete SHA256. Use paths returned by list_artifacts. Download completed job files for a stable checksum.',{'path':S,'offset':{'type':'integer','minimum':0,'default':0}},('path',)),
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
    resource.setrlimit(resource.RLIMIT_FSIZE,(16*1024*1024,16*1024*1024))
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))

def call(name,a):
    if name=='list_dram_presets':return presets()
    if name=='upload_trace':
        filename=a['filename']
        if not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',filename):raise ValueError('Use a simple trace filename')
        p=safe_path('traces/'+filename);offset=a['offset']
        if not p.exists() and len(list((ROOT/'traces').iterdir()))>=64:raise ValueError('Trace file limit reached')
        if type(offset)!=int or not 0<=offset<=16*1024*1024:raise ValueError('Invalid offset')
        data=base64.b64decode(a['data_base64'],validate=True)
        if not 0<len(data)<=1024 or offset+len(data)>16*1024*1024:raise ValueError('Chunk or trace too large')
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
        standard=a.get('standard','DDR4');choices=presets()
        if standard not in choices:raise ValueError('Unsupported DRAM standard')
        config={'standard':standard,'organization':a.get('organization','DDR4_8Gb_x8'),'timing':a.get('timing','DDR4_2400R'),
                'channels':a.get('channels',1),'overrides':a.get('overrides',{})}
        if config['organization'] not in choices[standard]['organizations'] or config['timing'] not in choices[standard]['timings']:raise ValueError('Invalid preset; call list_dram_presets')
        channels=config['channels'];seconds=a.get('timeout_seconds',120)
        if type(channels)!=int or not 1<=channels<=8 or type(seconds)!=int or not 1<=seconds<=600:raise ValueError('Invalid channels or timeout')
        ov=config['overrides']
        if not isinstance(ov,dict) or len(ov)>32 or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',k) or type(v)!=int or not 1<=v<=1000000 for k,v in ov.items()):raise ValueError('Invalid numeric overrides')
        path=safe_path('traces/'+a.get('trace','sample.trace'))
        if path.parent!=ROOT/'traces' or not path.is_file() or path.stat().st_size>16*1024*1024:raise ValueError('Trace not found or too large')
        lines=path.read_text().splitlines()
        if not lines or any(not re.fullmatch(r'(LD|ST) (0[xX][0-9a-fA-F]+|[0-9]+)',line) for line in lines):raise ValueError('Expected nonempty trace: LD 0xADDRESS or ST DECIMAL_ADDRESS')
        if any(int(line.split()[1],16 if line.split()[1].lower().startswith('0x') else 10)>=2**63 for line in lines):raise ValueError('Address exceeds signed 64-bit range')
        jid=uuid.uuid4().hex;folder=ROOT/'jobs'/jid;folder.mkdir(parents=True)
        # Freeze input so subsequent uploads cannot change an in-flight run.
        (folder/'input.trace').write_bytes(path.read_bytes());config['trace']=str(folder/'input.trace')
        (folder/'config.json').write_text(json.dumps(config,indent=2))
        with (folder/'run.log').open('wb') as log:
            proc=subprocess.Popen([sys.executable,'/opt/bridge/runner.py',str(folder/'config.json')],cwd=folder,stdout=log,stderr=log,start_new_session=True,preexec_fn=limits)
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
        if method=='initialize':value={'protocolVersion':'2025-03-26','capabilities':{'tools':{}},'serverInfo':{'name':'ramulator2','version':'2.1.0'}}
        elif method=='tools/list':value={'tools':TOOLS}
        elif method=='tools/call':
            try:
                value=call(msg['params']['name'],msg['params'].get('arguments',{}));value={'content':[{'type':'text','text':json.dumps(value)}],'structuredContent':value}
            except Exception as exc:
                error={'error':'RAMULATOR_TOOL_FAILED','message':str(exc)[:1000]}
                value={'content':[{'type':'text','text':json.dumps(error)}],'structuredContent':error,'isError':True}
        else:raise ValueError('Unknown method')
        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':value}),flush=True)
    except Exception:
        print(json.dumps({'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid request'}}),flush=True)
