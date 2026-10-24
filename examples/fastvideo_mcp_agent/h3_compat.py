"""H3-only CPU source staging before FSDP sharding on 32 GiB devices.

The pinned loader otherwise accumulates the complete 66 GB checkpoint on each
GPU before distributing individual tensors. CPU safetensors mappings keep the
source shared through the host page cache. No numerical weights are changed.
"""

def configure_worker_threads():
    """Bound per-rank pools for 16 ranks inside a 24-CPU/1024-PID container."""
    import os
    for variable in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                     'NUMEXPR_NUM_THREADS', 'TORCHINDUCTOR_COMPILE_THREADS',
                     'RAYON_NUM_THREADS'):
        os.environ[variable] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    import torch
    torch.set_num_threads(1)
    if torch.get_num_interop_threads() != 1:
        torch.set_num_interop_threads(1)

def disable_optional_sparse_kernels():
    """Fail closed for optional CUDA kernels in this dense-SDPA-only process.

    On SM60 the optional package initializer can fail after leaving namespace
    children in sys.modules. A later import then raises KeyError rather than
    ImportError, bypassing upstream's optional-backend guard. Reject imports
    before package initialization; never substitute a numerical kernel.
    """
    configure_worker_threads()
    import importlib.abc
    import sys

    class DenseOnly(importlib.abc.MetaPathFinder):
        _nexus_h3_dense_only = True

        def find_spec(self, fullname, path=None, target=None):
            if fullname == 'fastvideo_kernel' or fullname.startswith('fastvideo_kernel.'):
                raise ModuleNotFoundError('Optional sparse kernels disabled for H3 dense SDPA', name=fullname)
            return None

    for name in list(sys.modules):
        if name == 'fastvideo_kernel' or name.startswith('fastvideo_kernel.'):
            del sys.modules[name]
    if not any(getattr(finder, '_nexus_h3_dense_only', False) for finder in sys.meta_path):
        sys.meta_path.insert(0, DenseOnly())

def enable_cpu_weight_staging():
    disable_optional_sparse_kernels()
    from fastvideo.models.loader import fsdp_load
    # This module is imported only by the dedicated H3 training/export processes.
    # Keep the staging decision visible in the upstream loader's own log.
    fsdp_load._should_stage_transformer_weights_on_cpu = lambda **kwargs: True
