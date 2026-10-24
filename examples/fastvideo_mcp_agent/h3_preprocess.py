"""Official H3 layer-50 text conditioning, with bounded GPU layer streaming."""
import json
import os
from pathlib import Path
os.environ['FASTVIDEO_H3_ENCODER_LAYERWISE'] = '1'
os.environ['FASTVIDEO_ATTENTION_BACKEND'] = 'TORCH_SDPA'


def main():
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    import torch
    from fastvideo.configs.pipelines.minimax_h3 import MiniMaxH3PipelineConfig
    from fastvideo.fastvideo_args import FastVideoArgs
    from fastvideo.pipelines import ForwardBatch
    from fastvideo.pipelines.basic.minimax_h3.stages.minimax_h3_conditioning import MiniMaxH3ConditioningStage
    from fastvideo.pipelines.basic.minimax_h3.stages.minimax_h3_input_preparation import MINIMAX_H3_KEYFRAMES_KEY
    from fastvideo.pipelines.preprocess.preprocess_minimax_h3_overfit import _init_single_process_distributed, _load_component
    from fastvideo.dataset.dataloader.schema import pyarrow_schema_text_only
    _init_single_process_distributed()
    model = Path('/models/h3')
    request = json.loads(Path('/work/request.json').read_text())
    index = json.loads((model/'model_index.json').read_text())
    args = FastVideoArgs(model_path=str(model), pipeline_config=MiniMaxH3PipelineConfig(),
        num_gpus=1, tp_size=1, sp_size=1, text_encoder_cpu_offload=True, pin_cpu_memory=False)
    args.inference_mode = True
    tokenizer = _load_component('tokenizer', model, index, args)
    processor = _load_component('processor', model, index, args)
    conditioner = _load_component('text_encoder', model, index, args)
    stage = MiniMaxH3ConditioningStage(conditioner=conditioner, tokenizer=tokenizer, processor=processor)
    rows = []
    for i, prompt in enumerate(request['prompts']):
        batch = ForwardBatch(data_type='video', prompt=prompt)
        batch.extra[MINIMAX_H3_KEYFRAMES_KEY] = []
        with torch.inference_mode(): batch = stage.forward(batch, args)
        array = batch.prompt_embeds[0].squeeze(0).float().cpu().numpy().astype(np.float32)
        if array.ndim != 2 or array.shape[1] != 5120 or not np.isfinite(array).all():
            raise ValueError('Invalid H3 text conditioning')
        rows.append({'id':str(i), 'caption':prompt, 'text_embedding_bytes':array.tobytes(),
                     'text_embedding_shape':list(array.shape), 'text_embedding_dtype':'float32'})
        print(json.dumps({'stage':'h3_prompt_encoded', 'index':i, 'tokens':len(array)}), flush=True)
    output = Path('/work/conditioning'); output.mkdir(exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=pyarrow_schema_text_only), output/'prompts.parquet')
    torch.distributed.destroy_process_group()


if __name__ == '__main__': main()
