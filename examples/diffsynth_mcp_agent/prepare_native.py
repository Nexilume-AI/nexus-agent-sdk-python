from pathlib import Path
import subprocess,json
root=Path('/opt/nexus-agents/diffsynth-agent')
assert (root/'models/WEIGHTS_VERIFIED.json').is_file()
subprocess.run(['docker','run','--rm','--user',f'{__import__("os").getuid()}:{__import__("os").getgid()}',
 '--entrypoint','ffmpeg','--mount',f'type=bind,src={root},dst=/out','nexus-diffsynth-mcp:d188f01d0901',
 '-y','-f','lavfi','-i','testsrc2=size=256x256:rate=8','-t','2','-c:v','libx264','-pix_fmt','yuv420p','/out/demo.mp4'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
# Service is not installed yet. Marker authorizes this operator-only acceptance.
marker=root/'runtime-ready.json';marker.write_text(json.dumps({'state':'operator_acceptance'}))
try:
 subprocess.run(['/usr/bin/python3','-u','check.py'],cwd=root,check=True)
 evidence=json.loads((root/'native-evidence.json').read_text())
 assert evidence['report']['strict_adapter_reload'] and evidence['report']['changed_tensors']>0
 marker.write_text(json.dumps({'state':'tested','recipe':'wan2.1-t2v-1.3b','report_sha256':evidence['report_sha256']}))
except BaseException:
 marker.unlink(missing_ok=True);raise
print('NATIVE_ACCEPTED',flush=True)
