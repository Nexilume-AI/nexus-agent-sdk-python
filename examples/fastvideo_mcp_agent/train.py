"""Native training entry point with cache release after transient text encoding."""
import gc
import json
import runpy
from pathlib import Path
import torch
from fastvideo.train.models.wan import wan
from fastvideo.train.methods.distribution_matching.dmd2 import DMD2Method

encode_negative_prompt = wan.encode_negative_prompt


def encode_and_release(*args, **kwargs):
    result = encode_negative_prompt(*args, **kwargs)
    # The native encoder loader briefly uses >20 GiB per rank. NCCL allocates
    # outside PyTorch's allocator and cannot reclaim its unused cached blocks.
    before = torch.cuda.mem_get_info()[0]
    gc.collect()
    torch.cuda.empty_cache()
    after = torch.cuda.mem_get_info()[0]
    if torch.distributed.get_rank() == 0:
        print(json.dumps({'stage': 'released_text_encoder_cache',
            'free_bytes_before': before, 'free_bytes_after': after}), flush=True)
    return result


wan.encode_negative_prompt = encode_and_release
native_step = DMD2Method.single_train_step


def checked_step(self, batch, iteration):
    losses, outputs, metrics = native_step(self, batch, iteration)
    values = {key: float(value.detach().float().item()) for key, value in losses.items()}
    import math
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError('Nonfinite native DMD2 loss at iteration ' + str(iteration))
    if torch.distributed.get_rank() == 0:
        record = {'stage': 'training_step', 'iteration': iteration, 'loss_scope': 'rank0',
            'losses': values, 'update_student': bool(metrics.get('update_student'))}
        with Path('/work/training-metrics.jsonl').open('a') as output:
            output.write(json.dumps(record) + '\n')
        print(json.dumps(record), flush=True)
    return losses, outputs, metrics


DMD2Method.single_train_step = checked_step
runpy.run_module('fastvideo.train.entrypoint.train', run_name='__main__')
