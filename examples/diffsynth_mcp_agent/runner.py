"""Networkless native DiffSynth Wan SFT LoRA; bounded operator-defined recipe."""
import gc,hashlib,json,math,os,sys,time,subprocess
from pathlib import Path
os.environ['TOKENIZERS_PARALLELISM']='false'
import numpy as np
import torch
from PIL import Image
from safetensors.torch import save_file
sys.path.insert(0,'/opt/diffsynth/examples/wanvideo/model_training')
from train import WanTrainingModule
from diffsynth.models import wan_video_dit
wan_video_dit.FLASH_ATTN_2_AVAILABLE=False
wan_video_dit.FLASH_ATTN_3_AVAILABLE=False
wan_video_dit.SAGE_ATTN_AVAILABLE=False
from diffsynth.utils.data import save_video

ROOT=Path('/work');START=time.monotonic()
def write(name,v):
    p=ROOT/name;tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(v,indent=2));tmp.replace(p)
def progress(stage,**kw):
    v={'stage':stage,'elapsed_seconds':round(time.monotonic()-START,2),**kw}
    write('progress.json',v);print(json.dumps(v),flush=True)
def digest(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        while b:=f.read(4*1024*1024):h.update(b)
    return h.hexdigest()
def frames(path,height,width,count):
    probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],timeout=30))
    video=next(s for s in probe['streams'] if s['codec_type']=='video')
    duration=float(probe['format'].get('duration',0))
    if not 0<duration<=30 or not 32<=int(video['width'])<=1024 or not 32<=int(video['height'])<=1024:
        raise ValueError('Input MP4 must be <=30 seconds and 32..1024 pixels per side')
    raw=subprocess.check_output(['ffmpeg','-v','error','-i',str(path),'-vf',f'fps={count/duration},scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}',
        '-frames:v',str(count),'-f','rawvideo','-pix_fmt','rgb24','-threads','1','pipe:1'],timeout=60)
    n=len(raw)//(height*width*3)
    if n<1:raise ValueError('No decoded frames')
    images=[Image.fromarray(x.copy()) for x in np.frombuffer(raw,dtype=np.uint8).reshape(n,height,width,3)]
    while len(images)<count:images.append(images[-1].copy())
    return images[:count]
def make_model(cfg,checkpoint=None):
    return WanTrainingModule(model_paths=json.dumps(['/models/diffusion_pytorch_model.safetensors',
        '/models/models_t5_umt5-xxl-enc-bf16.pth','/models/Wan2.1_VAE.pth']),
        tokenizer_path='/models/google/umt5-xxl',lora_base_model='dit',
        lora_target_modules='q,k,v,o,ffn.0,ffn.2',lora_rank=cfg['rank'],
        lora_checkpoint=checkpoint,use_gradient_checkpointing=True,device='cuda',task='sft')
def adapter(model):
    return {k:v.detach().cpu().contiguous() for k,v in model.export_trainable_state_dict(model.state_dict(),remove_prefix='pipe.dit.').items()}
def main():
    torch.set_num_threads(2);torch.set_num_interop_threads(1)
    cfg=json.loads((ROOT/'request.json').read_text());torch.manual_seed(cfg['seed'])
    report={'upstream_commit':'d188f01d09013688dea27cc28f2e8dc0953c2543','base_model':'Wan-AI/Wan2.1-T2V-1.3B',
        'method':'Native DiffSynth Wan FlowMatch SFT LoRA','quality_validated':False,
        'note':'A short training and reload test is not model-quality certification.'}
    progress('validating_dataset')
    data=[{'prompt':row['caption'],'video':frames(ROOT/'inputs'/row['video'],cfg['height'],cfg['width'],cfg['num_frames'])} for row in cfg['samples']]
    progress('loading_model')
    model=make_model(cfg);params=list(model.trainable_modules());initial=adapter(model)
    assert initial and all('lora_' in k for k in initial)
    optim=torch.optim.AdamW(params,lr=cfg['learning_rate'],weight_decay=0.01)
    losses=[]
    for step in range(cfg['steps']):
        optim.zero_grad(set_to_none=True)
        loss=model(data[step%len(data)]);value=float(loss.detach().float())
        if not math.isfinite(value):raise ValueError('Nonfinite training loss')
        loss.backward()
        if any(p.grad is not None and not torch.isfinite(p.grad).all() for p in params):raise ValueError('Nonfinite gradient')
        norm=torch.nn.utils.clip_grad_norm_(params,1.0,error_if_nonfinite=True)
        optim.step();losses.append(value)
        with (ROOT/'metrics.jsonl').open('a') as f:f.write(json.dumps({'step':step+1,'loss':value,'gradient_norm':float(norm)})+'\n')
        progress('training',step=step+1,total_steps=cfg['steps'],loss=value)
    tensors=adapter(model)
    assert set(tensors)==set(initial) and all(torch.isfinite(t).all() for t in tensors.values())
    changed=[k for k in tensors if not torch.equal(tensors[k],initial[k])]
    assert changed and any('lora_B' in k and torch.count_nonzero(tensors[k]) for k in changed),'No effective LoRA update'
    weights=ROOT/'lora.safetensors';save_file(tensors,str(weights),metadata={'base_model':report['base_model'],'rank':str(cfg['rank'])})
    report.update(training_completed=True,steps_completed=cfg['steps'],losses=losses,
        lora_rank=cfg['rank'],tensor_count=len(tensors),changed_tensors=len(changed),all_tensors_finite=True,
        lora_sha256=digest(weights),lora_bytes=weights.stat().st_size)
    del optim,params,model,initial,loss;gc.collect();torch.cuda.empty_cache()
    progress('reloading_lora')
    reloaded=make_model(cfg,str(weights));restored=adapter(reloaded)
    assert set(restored)==set(tensors) and all(torch.equal(restored[k],tensors[k]) for k in tensors),'LoRA reload mismatch'
    report['strict_adapter_reload']=True
    progress('validating_reloaded_forward')
    with torch.no_grad():check=float(reloaded(data[0]).float())
    assert math.isfinite(check);report['reloaded_forward_loss']=check
    progress('generating_preview')
    with torch.inference_mode():
        video=reloaded.pipe(prompt=cfg['preview_prompt'],negative_prompt='',height=cfg['height'],width=cfg['width'],
            num_frames=cfg['num_frames'],num_inference_steps=cfg['preview_steps'],cfg_scale=1.0,seed=cfg['seed'],tiled=False)
    save_video(video,str(ROOT/'preview.mp4'),fps=8,quality=6)
    report['preview_sha256']=digest(ROOT/'preview.mp4');report['preview_generated']=True
    report['wall_seconds']=round(time.monotonic()-START,2)
    write('report.json',report);progress('complete')
if __name__=='__main__':
    try:main()
    except Exception as exc:progress('failed',error_type=type(exc).__name__);raise
