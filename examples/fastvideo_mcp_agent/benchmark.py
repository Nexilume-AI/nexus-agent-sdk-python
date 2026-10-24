"""Same-device, held-out paired video measurement; deliberately no quality claims."""
import json
from pathlib import Path
import sys
import time


def main():
    import imageio.v3 as iio
    import numpy as np
    import torch
    from fastvideo import VideoGenerator
    from fastvideo.api.sampling_param import SamplingParam

    role = sys.argv[1]
    assert role in ('teacher', 'student')
    root = Path('/work')
    request = json.loads((root / 'request.json').read_text())
    output = root / 'benchmark' / role; output.mkdir(parents=True, exist_ok=True)
    path = '/models/wan' if role == 'teacher' else '/work/student'
    config = {'dit_precision': 'fp32', 'text_encoder_precisions': ['fp32'], 'vae_precision': 'fp32',
        'vae_decode_precision': 'fp32', 'disable_autocast': True, 'flow_shift': 3.0 if role == 'teacher' else 8.0}
    if role == 'student': config['dmd_denoising_steps'] = [1000, 750, 500, 250]
    loaded = time.monotonic()
    generator = VideoGenerator.from_pretrained(path, num_gpus=1, use_fsdp_inference=False,
        dit_cpu_offload=True, vae_cpu_offload=True, text_encoder_cpu_offload=True,
        enable_torch_compile=False, disable_autocast=True, pin_cpu_memory=False, pipeline_config=config,
        override_pipeline_cls_name='WanPipeline' if role == 'teacher' else 'WanDMDPipeline')
    load_seconds = time.monotonic() - loaded
    params = SamplingParam.from_pretrained('/models/wan')
    params.num_inference_steps = 50 if role == 'teacher' else 4
    params.height = request['height']; params.width = request['width']; params.num_frames = request['num_frames']
    params.seed = request['seed']; params.guidance_scale = 5.0 if role == 'teacher' else 1.0
    prompt = 'A small paper boat floats slowly across a calm pond in warm morning light, steady camera.'
    training_prompts = {p.strip().casefold() for p in request['prompts']}
    variation = 1
    while prompt.strip().casefold() in training_prompts:
        prompt = f'{variation} paper boats drift across a quiet pond beside green reeds, steady camera.'
        variation += 1
    torch.cuda.synchronize(); started = time.monotonic()
    generator.generate_video(prompt, sampling_param=params, output_path=str(output), save_video=True)
    torch.cuda.synchronize(); elapsed = time.monotonic() - started
    files = list(output.rglob('*.mp4'))
    if len(files) != 1: raise RuntimeError('Expected exactly one benchmark video')
    frames = iio.imread(files[0], index=None)
    if frames.size == 0 or not np.isfinite(frames).all(): raise RuntimeError('Invalid benchmark media')
    np.save(output / 'frames.npy', frames, allow_pickle=False)
    (output / 'metrics.json').write_text(json.dumps({'role': role, 'prompt': prompt, 'seed': request['seed'],
        'num_inference_steps': params.num_inference_steps, 'guidance_scale': params.guidance_scale,
        'shape': list(frames.shape), 'model_load_seconds': load_seconds, 'generation_seconds': elapsed,
        'gpu': torch.cuda.get_device_name(0), 'compute_capability': list(torch.cuda.get_device_capability(0)),
        'torch_version': torch.__version__, 'cuda_version': torch.version.cuda, 'attention_backend': 'TORCH_SDPA',
        'dit_weight_precision': 'fp32', 'autocast_disabled': True,
        'component_cpu_offload': True}, indent=2))


if __name__ == '__main__': main()
