from pathlib import Path
import concurrent.futures, hashlib, json, urllib.request, subprocess, zipfile
root=Path(__file__).resolve().parent
expected=json.loads((root/'model-manifest.json').read_text())
hashes={r['path']:r['sha256'] for r in expected['files']}
cache=root/'models';cache.mkdir(exist_ok=True)
files={
'clip_model/ViT-B-32.pt':'https://openaipublic.azureedge.net/clip/models/40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af/ViT-B-32.pt',
'clip_model/ViT-L-14.pt':'https://openaipublic.azureedge.net/clip/models/b8cca3fd41ae0c99ba7e8951adf17d267cdb84cd88be6f7c2e0eca1737a03836/ViT-L-14.pt',
'dino_model/dino_vitbase16_pretrain.pth':'https://dl.fbaipublicfiles.com/dino/dino_vitbase16_pretrain/dino_vitbase16_pretrain.pth',
'aesthetic_model/emb_reader/sa_0_4_vit_l_14_linear.pth':'https://raw.githubusercontent.com/LAION-AI/aesthetic-predictor/main/sa_0_4_vit_l_14_linear.pth',
'amt_model/amt-s.pth':'https://huggingface.co/lalala125/AMT/resolve/main/amt-s.pth',
'pyiqa_model/musiq_spaq_ckpt-358bb6af.pth':'https://github.com/chaofengc/IQA-PyTorch/releases/download/v0.1-weights/musiq_spaq_ckpt-358bb6af.pth',
'raft_model/models.zip':'https://dl.dropboxusercontent.com/s/4j4z58wuv8o0mfz/models.zip',
}
def fetch(item):
 name,url=item;p=cache/name;p.parent.mkdir(parents=True,exist_ok=True)
 if not p.exists():
  part=p.with_suffix(p.suffix+'.partial')
  subprocess.run(['curl','--fail','--location','--retry','4','--connect-timeout','20','--max-time','1800','-C','-','-o',str(part),url],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  part.replace(p)
 h=hashlib.sha256()
 with p.open('rb') as f:
  while b:=f.read(8*1024*1024):h.update(b)
 digest=h.hexdigest()
 assert digest==hashes[name], 'Checkpoint differs from tested deployment: '+name
 if '/clip/models/' in url:assert digest==url.split('/')[-2],name
 print('VERIFIED',name,p.stat().st_size,digest,flush=True)
 return {'path':name,'source':url,'size':p.stat().st_size,'sha256':digest}
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:rows=list(pool.map(fetch,files.items()))
with zipfile.ZipFile(cache/'raft_model/models.zip') as z:
 for n in z.namelist():
  assert not n.startswith('/') and '..' not in Path(n).parts
 z.extractall(cache/'raft_model')
dino=cache/'dino_model/facebookresearch_dino_main'
if not dino.exists():
 subprocess.run(['git','clone','--depth','1','https://github.com/facebookresearch/dino.git',str(dino)],check=True)
 subprocess.run(['git','-C',str(dino),'fetch','--depth','1','origin',expected['dino_commit']],check=True)
 subprocess.run(['git','-C',str(dino),'checkout','--detach',expected['dino_commit']],check=True)
revision=subprocess.check_output(['git','-C',str(dino),'rev-parse','HEAD'],text=True).strip()
assert revision==expected['dino_commit'], 'DINO implementation differs from tested deployment'
(cache/'manifest.json').write_text(json.dumps({'files':rows,'dino_commit':revision},indent=2))
print('MODEL_CACHE_COMPLETE',flush=True)
