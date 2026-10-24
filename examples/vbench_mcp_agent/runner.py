"""Run pinned upstream VBench metrics on validated, caller-owned videos."""
import csv, gc, hashlib, json, math, os, subprocess, sys, time
from pathlib import Path

ROOT=Path('/work')
DIMENSIONS=['subject_consistency','background_consistency','temporal_flickering',
            'motion_smoothness','dynamic_degree','aesthetic_quality','imaging_quality']

def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  while b:=f.read(1048576):h.update(b)
 return h.hexdigest()

def write(p,value):
 tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(value,indent=2,allow_nan=False));tmp.replace(p)

def main():
 request=json.loads((ROOT/'request.json').read_text());started=time.time()
 # pyiqa 0.1.13 imports the pre-0.17 torchvision module alias.
 import torchvision.transforms.functional as functional
 sys.modules.setdefault('torchvision.transforms.functional_tensor',functional)
 import torch
 from vbench import VBench
 # DINO hubconf uses Torch's cache even with VBench local=True.
 hub=Path(torch.hub.get_dir())/'checkpoints';hub.mkdir(parents=True,exist_ok=True)
 target=hub/'dino_vitbase16_pretrain.pth'
 if not target.exists():target.symlink_to('/models/dino_model/dino_vitbase16_pretrain.pth')
 torch.set_num_threads(4)
 device=torch.device('cuda:0')
 if not torch.cuda.is_available():raise RuntimeError('Assigned GPU is unavailable')
 videos=[]
 for item in request['videos']:
  p=ROOT/'inputs'/item['name']
  probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(p)],timeout=30))
  streams=[s for s in probe['streams'] if s['codec_type']=='video']
  if len(streams)!=1:raise ValueError('Exactly one video stream required')
  s=streams[0];duration=float(probe['format'].get('duration',0));frames=int(s.get('nb_frames','0'))
  if not (0<duration<=30 and 2<=frames<=360 and 32<=s['width']<=512 and 32<=s['height']<=512):
   raise ValueError('Video must have 2..360 frames, 32..512 pixels per side and at most 30 seconds')
  # Confirm actual decoding; malformed files are rejected before metrics.
  subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(p),'-map','0:v:0','-f','null','-'],check=True,timeout=60,stdout=subprocess.DEVNULL)
  videos.append({**item,'sha256':digest(p),'frames':frames,'width':s['width'],'height':s['height'],'duration_seconds':duration})
 evaluator=VBench(device,'/opt/vbench/vbench/VBench_full_info.json',str(ROOT/'results'))
 scores={};details={}
 for index,dimension in enumerate(request['dimensions']):
  if dimension not in DIMENSIONS:raise ValueError('Unsupported metric')
  write(ROOT/'progress.json',{'stage':'evaluating','dimension':dimension,'completed':index,'total':len(request['dimensions'])})
  print('EVALUATING',dimension,flush=True)
  evaluator.evaluate(str(ROOT/'inputs'),dimension,
    prompt_list={v['name']:v.get('prompt','') for v in videos},dimension_list=[dimension],local=True,mode='custom_input')
  raw=json.loads((ROOT/'results'/f'{dimension}_eval_results.json').read_text())[dimension]
  score=float(raw[0]);per_video=raw[1]
  if not math.isfinite(score) or len(per_video)!=len(videos):raise RuntimeError('Incomplete or nonfinite metric output')
  for entry in per_video:
   if not math.isfinite(float(entry['video_results'])):raise RuntimeError('Nonfinite per-video score')
   entry['video_path']=Path(entry['video_path']).name
  scores[dimension]=score;details[dimension]=per_video
  gc.collect();torch.cuda.empty_cache()
 report={'upstream':'Vchitect/VBench','commit':Path('/opt/vbench/COMMIT').read_text().strip(),
  'mode':'custom_input','dimensions':request['dimensions'],'scores':scores,'per_video':details,'videos':videos,
  'seconds':round(time.time()-started,3),'device':torch.cuda.get_device_name(0),
  'limitations':['Custom-input scores are not an official VBench leaderboard score.',
  'Temporal flickering is intended for static scenes; real motion can lower its score.',
  'Seven VBench 1.0 visual dimensions; no VBench 2.0, audio or semantic category metrics.',
  'A short sample demonstrates the evaluation pipeline, not model quality.']}
 write(ROOT/'report.json',report)
 with (ROOT/'scores.csv').open('w',newline='') as f:
  w=csv.writer(f);w.writerow(['dimension','score']);w.writerows(scores.items())
 write(ROOT/'progress.json',{'stage':'completed','completed':len(scores),'total':len(scores)})
 print('EVALUATION_COMPLETE',json.dumps(scores),flush=True)

if __name__=='__main__':main()
