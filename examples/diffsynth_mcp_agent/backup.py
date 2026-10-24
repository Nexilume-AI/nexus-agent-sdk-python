from pathlib import Path
import hashlib,json,subprocess,tarfile,os
root=Path('/opt/nexus-agents/diffsynth-agent')
backup=root.parent/'backups/diffsynth-20261023';backup.mkdir(parents=True,exist_ok=True)
archive=backup/'source-sdk-config.tgz'
with tarfile.open(archive,'w:gz') as tar:
 for p in root.iterdir():
  if p.is_file() and p.suffix in ('.py','.md','.txt','.json','.service','.png') or p.name in ('Dockerfile','.dockerignore','upstream.commit'):
   tar.add(p,arcname='diffsynth-agent/'+p.name)
 tar.add(root/'upstream',arcname='diffsynth-agent/upstream')
 tar.add(root.parent/'sdk',arcname='sdk')
 tar.add(root/'models/WEIGHTS_VERIFIED.json',arcname='diffsynth-agent/models/WEIGHTS_VERIFIED.json')
 tar.add(root/'demo.mp4',arcname='diffsynth-agent/demo.mp4')
# Verified operator-owned model files remain on the same filesystem. Hard-link
# a snapshot so source deletion cannot remove the only copy; hashes accompany it.
model_backup=backup/'models';model_backup.mkdir(exist_ok=True)
for p in (root/'models').rglob('*'):
 if p.is_file() and '.git' not in p.parts and not p.name.endswith('.partial'):
  out=model_backup/p.relative_to(root/'models');out.parent.mkdir(parents=True,exist_ok=True)
  if not out.exists():os.link(p,out)
image=json.loads(subprocess.check_output(['docker','image','inspect','nexus-diffsynth-mcp:d188f01d0901']))[0]
(backup/'image.json').write_text(json.dumps({'id':image['Id'],'tags':image['RepoTags'],'size':image['Size']},indent=2))
with (backup/'requirements.lock').open('wb') as f:
 subprocess.run(['docker','run','--rm','--entrypoint','pip','nexus-diffsynth-mcp:d188f01d0901','freeze'],stdout=f,check=True)
h=hashlib.sha256(archive.read_bytes()).hexdigest();(backup/'SHA256SUMS').write_text(h+'  source-sdk-config.tgz\n')
print('BACKUP',backup,'SHA256',h,flush=True)
