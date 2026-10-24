"""Backup pinned source, configuration, dependencies and sanitized acceptance only."""
import json,hashlib,subprocess,tarfile
from pathlib import Path
root=Path(__file__).resolve().parent
proof=json.loads((root/'public-evidence.json').read_text())
assert proof['real_model']=='gpt-6-sol' and proof['correct_kernel'] and proof['model_requests']>=1
published=json.loads((root/'published-evidence.json').read_text());assert published['publication_status']=='published'
folder=root.parent/'backups/kernelfoundry-20261023';folder.mkdir(parents=True,exist_ok=True)
image=json.loads(subprocess.check_output(['docker','image','inspect','nexus-kernelfoundry-mcp:0b2338891b7b']))[0]
(root/'image.json').write_text(json.dumps({'id':image['Id'],'tags':image['RepoTags'],'size':image['Size']},indent=2))
with (root/'requirements.lock').open('wb') as f:subprocess.run(['docker','run','--rm','--entrypoint','python3','nexus-kernelfoundry-mcp:0b2338891b7b','-m','pip','freeze'],stdout=f,check=True)
archive=folder/'source-sdk-config.tgz'
with tarfile.open(archive,'w:gz') as tar:
 for p in root.iterdir():
  if p.is_file() and (p.suffix in ('.py','.md','.json','.service','.png','.lock') or p.name in ('Dockerfile','.dockerignore','upstream.commit')):tar.add(p,arcname='kernelfoundry-agent/'+p.name)
 tar.add(root/'upstream',arcname='kernelfoundry-agent/upstream')
 tar.add(root/'accepted-artifacts',arcname='kernelfoundry-agent/accepted-artifacts')
 tar.add(root.parent/'sdk',arcname='sdk')
digest=hashlib.sha256(archive.read_bytes()).hexdigest();(folder/'SHA256SUMS').write_text(digest+'  source-sdk-config.tgz\n')
print('BACKUP',str(archive),'SHA256',digest)
