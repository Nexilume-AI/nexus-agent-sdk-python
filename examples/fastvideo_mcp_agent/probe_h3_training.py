"""Operator diagnostic: tiny random H3 fixture for native DMD2 plumbing only.

Does not validate the released 33B weights, quality or production resource needs.
Run in a disposable container with /work writable.
"""
import json
import os
from pathlib import Path
import shutil
import sys
sys.path.insert(0, '/opt/nexus')
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import torch
import yaml
from safetensors.torch import save_file
from fastvideo.configs.models.dits.minimax_h3 import MiniMaxH3Config, MiniMaxH3ArchConfig
from fastvideo.models.dits.minimax_h3 import MiniMaxH3Transformer3DModel
from fastvideo.dataset.dataloader.schema import pyarrow_schema_text_only
from h3_recipe import configuration

root=Path('/work')
gpu_count=int(os.environ.get('H3_PROBE_GPUS','1'))
torch.manual_seed(42)
base=root/'tiny-h3';(base/'transformer').mkdir(parents=True,exist_ok=True)
arch=dict(num_attention_heads=8, attention_head_dim=32, hidden_size=256,
    num_layers=1, num_refiner_layers=1, ffn_dim=512, text_dim=5120,
    freq_dim=32, time_embed_hidden_dim=64, time_embed_dim=32, rope_freq_dim=4)
model=MiniMaxH3Transformer3DModel(MiniMaxH3Config(
    arch_config=MiniMaxH3ArchConfig(**arch),uniform_parameter_dtype=True), {})
with torch.no_grad():
    for parameter in model.parameters(): parameter.normal_(std=0.1)
weights={}
for key,value in model.state_dict().items():
    key=key.replace('time_embedder.fc_in.','time_embedder.linear_1.').replace('time_embedder.fc_out.','time_embedder.linear_2.')
    key=key.replace('.attn.to_out.','.attn.to_out.0.').replace('.ff.fc_in.','.ff.net.0.proj.').replace('.ff.fc_out.','.ff.net.2.')
    weights[key]=value.contiguous()
save_file(weights,str(base/'transformer/diffusion_pytorch_model.safetensors'))
(base/'transformer/config.json').write_text(json.dumps({'_class_name':'MiniMaxH3Transformer3DModel',**arch}))
index=json.loads(Path('/models/h3/model_index.json').read_text())
(base/'model_index.json').write_text(json.dumps({k:v for k,v in index.items()
    if k.startswith('_') or k=='transformer'}))
request={'prompts':['a','b','c','d'],'height':64,'width':64,'num_frames':124,'seed':42,'max_train_steps':5}
config=configuration(request,yaml.safe_load(Path('/opt/nexus/wan_dmd2.yaml').read_text()),model=str(base),gpu_count=gpu_count)
# This artificial random network has a much smaller gradient signal than the
# real pretrained model. Exercise optimizer/export plumbing explicitly; these
# diagnostic learning rates must never become the production H3 recipe.
config['training']['optimizer']['learning_rate']=1e-3
config['method']['fake_score_learning_rate']=1e-3
for role in ('student','critic'):config['models'][role]['expected_lora_layers']=12
(root/'train.yaml').write_text(yaml.safe_dump(config))
rows=[]
for i in range(4):
    array=np.random.default_rng(i).normal(0,0.2,(8,5120)).astype(np.float32)
    rows.append({'id':str(i),'caption':str(i),'text_embedding_bytes':array.tobytes(),
        'text_embedding_shape':list(array.shape),'text_embedding_dtype':'float32'})
(root/'conditioning').mkdir(exist_ok=True)
pq.write_table(pa.Table.from_pylist(rows,schema=pyarrow_schema_text_only),root/'conditioning/prompts.parquet')
from fastvideo.train.utils.config import load_run_config
cfg=load_run_config(str(root/'train.yaml'))
assert type(cfg.training.pipeline_config).__name__=='MiniMaxH3PipelineConfig'
print('TINY_H3_CONFIG_VALIDATED',flush=True)
