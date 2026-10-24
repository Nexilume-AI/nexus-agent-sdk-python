from pathlib import Path
import json,hashlib,tarfile,subprocess
root=Path('/opt/nexus-agents/openevolve-agent')
proof=json.loads((root/'public-evidence.json').read_text());assert proof['real_model']=='gpt-6-sol'
published=json.loads((root/'published-evidence.json').read_text());assert published['publication_status']=='published'
folder=root.parent/'backups/openevolve-20261023';folder.mkdir(parents=True,exist_ok=True)
archive=folder/'source-sdk-config.tgz'
with tarfile.open(archive,'w:gz') as tar:
 for p in root.iterdir():
  if p.is_file() and (p.suffix in ('.py','.md','.json','.service','.png') or p.name in ('Dockerfile','.dockerignore','upstream.commit')):tar.add(p,arcname='openevolve-agent/'+p.name)
 tar.add(root/'upstream',arcname='openevolve-agent/upstream')
 tar.add(root/'accepted-artifacts',arcname='openevolve-agent/accepted-artifacts')
 tar.add(root.parent/'sdk',arcname='sdk')
image=json.loads(subprocess.check_output(['docker','image','inspect','nexus-openevolve-mcp:04a37f191b2e']))[0]
(folder/'image.json').write_text(json.dumps({'id':image['Id'],'tags':image['RepoTags'],'size':image['Size']},indent=2))
with (folder/'requirements.lock').open('wb') as f:subprocess.run(['docker','run','--rm','--entrypoint','pip','nexus-openevolve-mcp:04a37f191b2e','freeze'],stdout=f,check=True)
digest=hashlib.sha256(archive.read_bytes()).hexdigest();(folder/'SHA256SUMS').write_text(digest+'  source-sdk-config.tgz\n')
print('BACKUP',str(archive),'SHA256',digest)
