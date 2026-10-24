"""Pinned MiniMax H3 text-to-audio-video DMD2 recipe and readiness contract."""
import json
from pathlib import Path

H3_ID = 'minimax-h3-t2va'
H3_REVISION = '42ed227ee7df40d41602854ae760620d6eb651fe'
H3_RUNGS = [999, 874, 749, 624, 500, 375, 250, 125]
# Host 11: device 15 fails CUDA initialization with an uncorrectable ECC error.
# Device 0 remains reserved for Atrex; use healthy device 3 as the replacement.
H3_GPUS = tuple(range(3, 15)) + tuple(range(16, 20))


def _read_status(path):
    try:
        value=json.loads(path.read_text())
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):
        return {}


def status(root):
    root = Path(root)
    weights = root / 'models/minimax-h3/WEIGHTS_VERIFIED.json'
    acceptance = root / 'h3-acceptance.json'
    verified = False
    if weights.is_file():
        manifest = _read_status(weights)
        verified = manifest.get('revision') == H3_REVISION and manifest.get('repo') == 'MiniMaxAI/MiniMax-H3'
    ready = False
    if verified and acceptance.is_file():
        evidence = _read_status(acceptance)
        ready = (evidence.get('revision') == H3_REVISION and
                 all(evidence.get(k) is True for k in ('real_weights', 'training_completed',
                      'export_reloaded', 'paired_audio_video_validated')))
    probe = root/'h3-probe/job.json'
    validation_job = _read_status(probe).get('state','invalid_evidence') if probe.is_file() else 'waiting_for_weights'
    return {'id': H3_ID, 'name': 'MiniMax H3 T2VA', 'family': H3_ID,
            'source': 'official', 'repo': 'MiniMaxAI/MiniMax-H3', 'revision': H3_REVISION,
            'state': 'ready' if ready else 'provisioning', 'ready': ready,
            'weights_verified': verified, 'quality_validated': False,
            'validation_job':validation_job,
            'recipe': 'minimax-h3-t2va-dmd2-lora-8step',
            'input_modes': ['text'], 'output_modalities': ['video', 'audio'],
            'method': 'DMD2 with trainable student/critic LoRA; merged full student export',
            'target_inference_forwards': 8, 'attention_backend': 'TORCH_SDPA',
            'gpu_count_per_job': len(H3_GPUS),
            'note': 'Readiness requires real-weight training, reload and paired audio/video acceptance. '
                    'This is a dense-attention recipe, not the published VSA FastH3 checkpoint.'}


def validate(values):
    if values['height'] % 32 or values['width'] % 32:
        raise ValueError('H3 width and height must be multiples of 32.')
    if values['num_frames'] != 124:
        raise ValueError('This H3 T2VA recipe uses exactly 124 frames (about 5 seconds at 24 FPS).')
    if values['max_train_steps'] % 5:
        raise ValueError('H3 training steps must be divisible by 5 to include complete student updates.')
    return values


def configuration(request, template, model='/models/h3', work='/work', gpu_count=16):
    import copy
    config = copy.deepcopy(template)
    for role, spec in config['models'].items():
        spec.update(_target_='fastvideo.train.models.minimax_h3.minimax_h3_dmd.' +
                    ('MiniMaxH3DMDModel' if role == 'teacher' else 'MiniMaxH3LoraDMDModel'),
                    init_from=model, attention_backend='TORCH_SDPA')
        if role != 'teacher':
            spec.update(lora={'enable': True, 'rank': 16, 'alpha': 16,
                        'target_modules': ['attn.to_q','attn.to_k','attn.to_v',
                                           'attn.to_out','ff.fc_in','ff.fc_out']},
                        expected_lora_layers=312)
    config['method'].update(real_score_guidance_scale=0.0,
        cfg_uncond={'text':'zero'}, dmd_denoising_steps=H3_RUNGS)
    training = config['training']
    training['distributed'].update(num_gpus=gpu_count, sp_size=8 if gpu_count>=8 else 1, tp_size=1,
        hsdp_replicate_dim=1, hsdp_shard_dim=gpu_count, pin_cpu_memory=False)
    training['dit_precision'] = 'bf16'
    training['data'].update(data_path=work+'/conditioning', preprocessed_data_type='text_only',
        dataloader_num_workers=0, num_height=request['height'], num_width=request['width'],
        num_frames=request['num_frames'], num_latent_t=(request['num_frames']-5)//17*5+2,
        seed=request['seed'], train_batch_size=1, training_cfg_rate=0.0)
    training['loop']['max_train_steps'] = request['max_train_steps']
    training['checkpoint'].update(output_dir=work+'/checkpoints', checkpoints_total_limit=1,
        training_state_checkpointing_steps=request['max_train_steps'])
    training['tracker'] = {'trackers':['none'], 'project_name':'nexus-h3', 'run_name':''}
    training['model']['enable_torch_compile'] = False
    config['callbacks'].pop('validation', None)
    config['callbacks'].pop('ema', None)
    config['pipeline'] = {'video_scheduler_shift':12.0, 'audio_scheduler_shift':3.0}
    return config


def inference_contract():
    return {'schema_version':'fasth3-inference-contract-v1',
            'dmd_denoising_steps':H3_RUNGS, 'num_inference_steps':len(H3_RUNGS)+1,
            'transformer_forwards':len(H3_RUNGS),
            'video_scheduler_shift':12.0, 'audio_scheduler_shift':3.0}
