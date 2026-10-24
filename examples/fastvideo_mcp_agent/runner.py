"""Fixed native Wan DMD2 recipe. Runs only inside the isolated job container."""
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path('/work')
MODEL = Path('/models/wan')


def stage(name):
    print(json.dumps({'stage': name, 'time': time.time()}), flush=True)
    (ROOT / 'progress.json').write_text(json.dumps({'stage': name, 'time': time.time()}))


def preprocess(request):
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    import torch
    from transformers import AutoTokenizer, UMT5EncoderModel
    from fastvideo.dataset.dataloader.schema import pyarrow_schema_text_only

    tokenizer = AutoTokenizer.from_pretrained(str(MODEL / 'tokenizer'), local_files_only=True)
    encoder = UMT5EncoderModel.from_pretrained(str(MODEL / 'text_encoder'), dtype=torch.float32, local_files_only=True).to('cuda:0').eval()
    rows = []
    for i, prompt in enumerate(request['prompts']):
        inputs = tokenizer(prompt, padding='max_length', max_length=512, truncation=True, return_tensors='pt').to('cuda:0')
        with torch.inference_mode(): embedding = encoder(**inputs).last_hidden_state[0]
        length = int(inputs.attention_mask.sum())
        array = embedding[:length].float().cpu().numpy().astype(np.float32)
        if not np.isfinite(array).all(): raise ValueError('Non-finite text conditioning')
        rows.append({'id': str(i), 'caption': prompt, 'text_embedding_bytes': array.tobytes(), 'text_embedding_shape': list(array.shape), 'text_embedding_dtype': 'float32'})
    target = ROOT / 'conditioning'; target.mkdir(exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=pyarrow_schema_text_only), target / 'prompts.parquet')
    del encoder, tokenizer, embedding, inputs
    gc.collect(); torch.cuda.empty_cache()


def configuration(request):
    import yaml
    config = yaml.safe_load(Path('/opt/nexus/wan_dmd2.yaml').read_text())
    for model in config['models'].values():
        model['init_from'] = str(MODEL)
        model['attention_backend'] = 'TORCH_SDPA'
    training = config['training']
    training['data'].update(data_path=str(ROOT / 'conditioning'), preprocessed_data_type='text_only',
        dataloader_num_workers=0, num_height=request['height'], num_width=request['width'],
        num_frames=request['num_frames'], num_latent_t=(request['num_frames'] - 1) // 4 + 1, seed=request['seed'])
    training['loop']['max_train_steps'] = request['max_train_steps']
    training['checkpoint'].update(output_dir=str(ROOT / 'checkpoints'), checkpoints_total_limit=1,
        training_state_checkpointing_steps=max(1, request['max_train_steps']))
    training['tracker'] = {'trackers': ['none'], 'project_name': 'nexus-local', 'run_name': ''}
    training['model']['enable_torch_compile'] = False
    config['callbacks'].pop('validation', None)
    config['callbacks'].pop('ema', None)
    config['pipeline'].update(vae_precision='fp32', vae_decode_precision='fp32')
    target = ROOT / 'train.yaml'; target.write_text(yaml.safe_dump(config, sort_keys=False))
    return target


def main():
    request = json.loads((ROOT / 'request.json').read_text())
    started = time.monotonic()
    report = {'upstream_commit': 'fa2d1324c60050cd65209c6aaeac470e7fb412f5',
        'base_model': 'Wan-AI/Wan2.1-T2V-1.3B-Diffusers', 'base_revision': '0fad780a534b6463e45facd96134c9f345acfa5b',
        'method': 'FastVideo native DMD2', 'target_timesteps': [1000, 750, 500, 250],
        'parameter_count_reduced': False, 'training_steps_requested': request['max_train_steps'],
        'quality_validated': False, 'measured_speedup': None,
        'notes': 'Training completion alone is not evidence of perceptual quality or measured inference acceleration.'}
    report['model_provenance'] = request.get('model_provenance', {})
    report['base_model'] = report['model_provenance'].get('id', report['base_model'])
    report['base_revision'] = report['model_provenance'].get('revision')
    try:
        stage('preprocessing_conditioning'); preprocess(request)
        config = configuration(request)
        stage('distilling')
        subprocess.run(['torchrun', '--nnodes=1', '--master_addr=127.0.0.1', '--master_port=29500', '--nproc_per_node=4', '/opt/nexus/train.py', '--config', str(config)], check=True)
        report['training_completed'] = True
        report['student_updates_completed'] = request['max_train_steps'] // 5
        stage('exporting_student')
        subprocess.run([sys.executable, '/opt/nexus/export.py'], check=True)
        report['weight_validation'] = json.loads((ROOT / 'weights-manifest.json').read_text())
        report['student_export_verified'] = True
        stage('benchmarking_teacher_and_student')
        for role in ('teacher', 'student'):
            subprocess.run([sys.executable, '/opt/nexus/benchmark.py', role], check=True)
        baseline = json.loads((ROOT / 'benchmark' / 'teacher' / 'metrics.json').read_text())
        student = json.loads((ROOT / 'benchmark' / 'student' / 'metrics.json').read_text())
        import numpy as np
        ref = np.load(ROOT / 'benchmark' / 'teacher' / 'frames.npy', allow_pickle=False).astype(np.float32)
        generated = np.load(ROOT / 'benchmark' / 'student' / 'frames.npy', allow_pickle=False).astype(np.float32)
        if ref.shape != generated.shape: raise ValueError('Benchmark outputs have different shapes')
        mse = float(np.mean((ref - generated) ** 2))
        report['benchmark'] = {'teacher': baseline, 'student': student,
            'pixel_mse_to_teacher': mse, 'psnr_to_teacher_db': None if mse == 0 else float(10 * np.log10(255 ** 2 / mse)),
            'quality_note': 'Pixel agreement on one held-out prompt is not a perceptual quality score. Review both videos. No FVD, VBench or quality threshold certification.',
            'timing_note': 'Single cold generation per role, same GPU, prompt, seed and dimensions. Excludes model loading; no warm-up or confidence interval.'}
        report['measured_speedup'] = baseline['generation_seconds'] / student['generation_seconds']
        (ROOT / 'student' / 'NEXUS_EXPORT_README.txt').write_text(
            'Native FastVideo DMD2 student transformer. This directory includes unchanged tokenizer, text encoder and VAE from '
            'Wan-AI/Wan2.1-T2V-1.3B-Diffusers revision 0fad780a534b6463e45facd96134c9f345acfa5b. '
            'Use WanDMDPipeline with timesteps [1000,750,500,250], flow_shift=8 and guidance_scale=1. '
            'See train.yaml and report.json; training completion is not quality certification.\n')
        stage('complete')
    except Exception as exc:
        report.setdefault('training_completed', False)
        report['error_type'] = type(exc).__name__
        stage('failed')
        raise
    finally:
        report['wall_seconds'] = round(time.monotonic() - started, 3)
        (ROOT / 'report.json').write_text(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
