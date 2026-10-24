"""Export only the student DCP state, using native FastVideo weight mapping."""
import gc
from pathlib import Path


def main():
    import torch
    import torch.distributed.checkpoint as dcp
    from fastvideo.distributed import maybe_init_distributed_environment_and_model_parallel
    from fastvideo.train.entrypoint.dcp_to_diffusers import (
        _ensure_distributed, _save_role_pretrained, _strict_reload_verify,
    )
    from fastvideo.train.utils.checkpoint import _resolve_resume_checkpoint
    from fastvideo.train.utils.config import load_run_config
    from fastvideo.train.utils.instantiate import instantiate
    from fastvideo.training.checkpointing_utils import ModelWrapper

    _ensure_distributed()
    maybe_init_distributed_environment_and_model_parallel(tp_size=1, sp_size=1)
    cfg = load_run_config('/work/train.yaml')
    tc = cfg.training
    tc.distributed.tp_size = tc.distributed.sp_size = tc.distributed.num_gpus = 1
    tc.distributed.hsdp_replicate_dim = tc.distributed.hsdp_shard_dim = 1
    tc.model.enable_gradient_checkpointing_type = None
    # No method construction: it initializes teacher, critic, optimizers and T5.
    model = instantiate(cfg.models['student'], training_config=tc)
    states = {'roles.student.transformer': ModelWrapper(model.transformer)}
    expected = states['roles.student.transformer'].state_dict()
    if not expected:
        raise RuntimeError('Student export requested an empty checkpoint state')
    del expected
    checkpoint = _resolve_resume_checkpoint('/work/checkpoints', output_dir='/work/checkpoints')
    # Default strict DCP planner rejects any requested tensor missing from disk.
    dcp.load(states, checkpoint_id=str(checkpoint / 'dcp'))
    result = _save_role_pretrained(role='student', base_model_path='/models/wan',
        output_dir='/work/student', overwrite=False, model=model)
    del states, model
    gc.collect(); torch.cuda.empty_cache()
    _strict_reload_verify(output_dir=result, training_config=tc, module_type='transformer')
    from verify_weights import verify_student
    import json
    validation = verify_student(Path(result), Path('/models/wan'))
    Path('/work/weights-manifest.json').write_text(json.dumps(validation, indent=2))
    print('STUDENT_EXPORT_VERIFIED', json.dumps(validation), flush=True)


if __name__ == '__main__':
    main()
