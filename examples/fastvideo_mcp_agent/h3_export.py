"""Distributed H3 student export; LoRA is merged by FastVideo's native exporter."""
import gc
import json
import os
from pathlib import Path


def main():
    from h3_compat import enable_cpu_weight_staging
    enable_cpu_weight_staging()
    import torch
    import torch.distributed as dist
    import torch.distributed.checkpoint as dcp
    from fastvideo.distributed import maybe_init_distributed_environment_and_model_parallel
    from fastvideo.train.entrypoint.dcp_to_diffusers import _save_role_pretrained, _strict_reload_verify
    from fastvideo.train.utils.checkpoint import _resolve_resume_checkpoint
    from fastvideo.train.utils.config import load_run_config
    from fastvideo.train.utils.instantiate import instantiate
    from fastvideo.training.checkpointing_utils import ModelWrapper
    from h3_recipe import inference_contract
    maybe_init_distributed_environment_and_model_parallel(tp_size=1, sp_size=1)
    cfg = load_run_config('/work/train.yaml')
    tc = cfg.training
    tc.distributed.sp_size = 1
    tc.model.enable_gradient_checkpointing_type = None
    model = instantiate(cfg.models['student'], training_config=tc)
    states = {'roles.student.transformer':ModelWrapper(model.transformer)}
    checkpoint = _resolve_resume_checkpoint('/work/checkpoints', output_dir='/work/checkpoints')
    dcp.load(states, checkpoint_id=str(checkpoint/'dcp'))
    result = _save_role_pretrained(role='student', base_model_path='/models/h3',
        output_dir='/work/student', overwrite=False, model=model, inference_contract=inference_contract())
    del states, model
    gc.collect(); torch.cuda.empty_cache()
    _strict_reload_verify(output_dir=result, training_config=tc, module_type='transformer')
    dist.barrier()
    if dist.get_rank() == 0:
        Path('/work/h3-export-reloaded.json').write_text(json.dumps({'strict_reload':True,
            'world_size':int(os.environ['WORLD_SIZE']), 'lora':'merged'}))
    dist.destroy_process_group()


if __name__ == '__main__': main()
