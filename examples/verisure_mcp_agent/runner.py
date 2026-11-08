"""Host launch layer. Project code and all uploaded sources execute inside Docker."""
import base64,json,os,subprocess,sys,time
from pathlib import Path
work=Path(sys.argv[1]);container=sys.argv[2];private=Path(sys.argv[3])
cfg=json.loads((work/'request.json').read_text())
files={p.relative_to(work/'inputs').as_posix():p.read_text() for p in (work/'inputs').rglob('*') if p.is_file()}
cmd=['docker','run','--rm','--init','-i','--name',container,'--network','none','--read-only',
 '--user',f'{os.getuid()}:{os.getgid()}','--cap-drop','ALL','--security-opt','no-new-privileges:true',
 '--memory','3g','--memory-swap','3g','--cpus','2','--pids-limit','192','--ulimit','fsize=33554432:33554432',
 '--tmpfs','/tmp:rw,exec,nosuid,nodev,size=1g','--tmpfs','/work:rw,exec,nosuid,nodev,mode=1777,size=512m']
if cfg['mode'] in ('generate','benchmark'):cmd+=['--mount',f'type=bind,src={private}/ipc,dst=/broker,readonly']
cmd+=['nexus-verisure-mcp:aab5bed60f04-r2']
output=work/'worker-output.json';err=work/'worker-error.log'
with output.open('wb') as stdout,err.open('wb') as stderr:
    p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=stdout,stderr=stderr)
    p.stdin.write(json.dumps({'request':cfg,'files':files}).encode());p.stdin.close();start=time.monotonic()
    try:
        while p.poll() is None:
            if time.monotonic()-start>840 or output.stat().st_size>24*1024*1024 or err.stat().st_size>4194304:raise RuntimeError('WORKER_LIMIT')
            time.sleep(.2)
        if p.returncode:raise RuntimeError('WORKER_FAILED')
    finally:
        if p.poll() is None:
            subprocess.run(['docker','rm','-f',container],capture_output=True,timeout=20);p.terminate();p.wait(timeout=20)
value=json.loads(output.read_text());total=0
for name,data in value['artifacts'].items():
    target=work/name
    if not target.resolve().is_relative_to(work) or name in ('request.json','run.log','worker-output.json'):raise ValueError('INVALID_ARTIFACT')
    blob=base64.b64decode(data,validate=True);total+=len(blob)
    if total>16*1024*1024:raise ValueError('ARTIFACT_LIMIT')
    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(blob)
output.unlink()
