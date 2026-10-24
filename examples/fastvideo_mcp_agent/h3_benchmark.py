"""Paired H3 audio/video generation with identical prompts, seeds and dimensions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
os.environ['FASTVIDEO_H3_ENCODER_LAYERWISE'] = '1'
os.environ['FASTVIDEO_ATTENTION_BACKEND'] = 'TORCH_SDPA'


def main():
    from h3_compat import disable_optional_sparse_kernels
    disable_optional_sparse_kernels()
    import imageio.v3 as iio
    import numpy as np
    from fastvideo import VideoGenerator
    from fastvideo.api import (GeneratorConfig, EngineConfig, OffloadConfig, CompileConfig,
        PipelineSelection, GenerationRequest, SamplingConfig, OutputConfig)
    role = sys.argv[1]
    if role not in ('teacher','student'): raise ValueError('Invalid role')
    request = json.loads(Path('/work/request.json').read_text())
    output = Path('/work/benchmark')/role; output.mkdir(parents=True, exist_ok=True)
    prompt = 'A small paper boat floats across a quiet pond, gentle ripples and soft birdsong.'
    while prompt.casefold() in {p.strip().casefold() for p in request['prompts']}:
        prompt = 'At sunrise, ' + prompt
    path = '/models/h3' if role == 'teacher' else '/work/student'
    started = time.monotonic()
    generator = VideoGenerator.from_config(GeneratorConfig(model_path=path,
        pipeline=PipelineSelection(), engine=EngineConfig(num_gpus=1,
        offload=OffloadConfig(dit=False, dit_layerwise=True, text_encoder=True,
                             vae=True, pin_cpu_memory=False),
        compile=CompileConfig(enabled=False, vae_enabled=False))))
    loaded = time.monotonic()-started
    try:
        started = time.monotonic()
        generator.generate(GenerationRequest(prompt=prompt, negative_prompt='',
            sampling=SamplingConfig(height=request['height'], width=request['width'],
                num_frames=request['num_frames'], fps=24, num_inference_steps=50 if role=='teacher' else 9,
                guidance_scale=1.0, batch_cfg=False, seed=request['seed']),
            output=OutputConfig(output_path=str(output/'h3.mp4'), save_video=True, return_frames=False)))
        elapsed = time.monotonic()-started
        videos = list(output.rglob('*.mp4'))
        if len(videos) != 1: raise ValueError('Expected one benchmark MP4')
        frames = iio.imread(videos[0], index=None)
        if not frames.size or not np.isfinite(frames).all(): raise ValueError('Invalid video')
        probe = subprocess.run(['ffprobe','-v','error','-show_streams','-of','json',str(videos[0])],
            check=True, capture_output=True, text=True)
        streams = json.loads(probe.stdout)['streams']
        if not any(s.get('codec_type')=='audio' for s in streams):
            raise ValueError('H3 result is missing its audio track')
        np.save(output/'frames.npy', frames, allow_pickle=False)
        (output/'metrics.json').write_text(json.dumps({'role':role, 'prompt':prompt,
            'seed':request['seed'], 'model_load_seconds':loaded, 'generation_seconds':elapsed,
            'shape':list(frames.shape), 'streams':streams, 'audio_present':True,
            'quality_validated':False, 'attention_backend':'TORCH_SDPA',
            'note':'One cold run including layer transfers; not a production performance certification.'}, indent=2))
    finally: generator.shutdown()


if __name__ == '__main__': main()
