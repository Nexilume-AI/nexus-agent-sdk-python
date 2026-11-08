import base64,json,os,subprocess,sys,time
from pathlib import Path
from demos import FILES
root=Path(__file__).resolve().parent;proof={}
for key in ('contract_lint','simulate','simulate_bad','formal','formal_bad','trace'):
 mode=key.removesuffix('_bad');request={'mode':mode,'rtl_path':'bad_rtl.sv' if key.endswith('_bad') else 'rtl.sv','top':'tb'}
 cmd=['docker','run','--rm','--init','-i','--network','none','--read-only','--user',f'{os.getuid()}:{os.getgid()}','--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','3g','--memory-swap','3g','--cpus','2','--pids-limit','192','--ulimit','fsize=33554432:33554432','--tmpfs','/tmp:rw,exec,nosuid,nodev,size=1g','--tmpfs','/work:rw,exec,nosuid,nodev,mode=1777,size=512m','nexus-verisure-mcp:aab5bed60f04-r2']
 t=time.monotonic();p=subprocess.run(cmd,input=json.dumps({'request':request,'files':FILES}).encode(),capture_output=True,timeout=240)
 folder=root/'native-proof'/key;folder.mkdir(parents=True,exist_ok=True);(folder/'stderr.log').write_bytes(p.stderr)
 assert p.returncode==0,(key,p.returncode,p.stderr[-2000:].decode(errors='replace'))
 data=json.loads(p.stdout)
 for n,b in data['artifacts'].items():
  path=folder/n;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(base64.b64decode(b))
 r=json.loads((folder/'report.json').read_text());proof[key]=r;print(key,json.dumps(r),flush=True)
 if mode in ('simulate','formal'):assert r['passed']==(not key.endswith('_bad')),(key,r)
 if mode=='formal':assert r['formal_status']==('fail' if key.endswith('_bad') else 'pass')
 if mode=='contract_lint':assert r['passed']
 if mode=='trace':assert r['slices']>0
 if key=='simulate':assert (folder/'wave.vcd').stat().st_size>0
proof['passed']=True;(root/'native-evidence.json').write_text(json.dumps(proof,indent=2));print('NATIVE_ACCEPTANCE_PASSED')
