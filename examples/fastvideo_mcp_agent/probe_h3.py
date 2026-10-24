"""Small randomly initialized H3 forward/backward device probe, NOT a model test."""
import json
import os
os.environ['FASTVIDEO_ATTENTION_BACKEND'] = 'TORCH_SDPA'
os.environ['FASTVIDEO_MINIMAX_H3_FUSIONS'] = ''
os.environ['FASTVIDEO_MINIMAX_H3_EXACT_KERNELS'] = ''
import torch
from fastvideo.configs.models.dits.minimax_h3 import MiniMaxH3Config, MiniMaxH3ArchConfig
from fastvideo.models.dits.minimax_h3 import MiniMaxH3Transformer3DModel
from fastvideo.forward_context import set_forward_context
from fastvideo.pipelines.preprocess.preprocess_minimax_h3_overfit import _init_single_process_distributed
_init_single_process_distributed()

torch.manual_seed(7)
arch = MiniMaxH3ArchConfig(num_attention_heads=2, attention_head_dim=32,
    hidden_size=64, num_layers=1, num_refiner_layers=1, ffn_dim=128,
    text_dim=64, freq_dim=32, time_embed_hidden_dim=64, time_embed_dim=32, rope_freq_dim=4)
model = MiniMaxH3Transformer3DModel(MiniMaxH3Config(arch_config=arch,
    uniform_parameter_dtype=True), {}).to(device='cuda', dtype=torch.bfloat16)
with torch.no_grad():
    for p in model.parameters(): p.normal_(std=0.02)
inputs = dict(hidden_states=torch.randn(1, 4, 96, device='cuda'),
    audio_hidden_states=torch.randn(1, 2, 32, device='cuda'),
    encoder_hidden_states=torch.randn(1, 2, 64, device='cuda'),
    timestep=torch.tensor([0.7, 0.3], device='cuda'),
    timestep_indices=torch.tensor([0,0,1,1,0,0,0,0], device='cuda'),
    token_tags=torch.tensor([1,1,2,2,0,0,0,0], device='cuda'),
    position_ids=torch.zeros(8,3, device='cuda'),
    text_indices=torch.arange(2, device='cuda'), audio_indices=torch.arange(2,4, device='cuda'),
    video_indices=torch.arange(4,8, device='cuda'))
with set_forward_context(current_timestep=0, attn_metadata=None):
    video, audio = model(**inputs)
    loss = video.float().square().mean() + audio.float().square().mean()
    loss.backward()
assert torch.isfinite(loss)
assert all(torch.isfinite(p.grad).all() for p in model.parameters() if p.grad is not None)
print(json.dumps({'probe':'tiny_random_h3_forward_backward', 'success':True,
    'loss':loss.item(), 'compute_capability':torch.cuda.get_device_capability(),
    'real_weights_tested':False}))
