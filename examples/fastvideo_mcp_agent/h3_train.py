"""H3-specific source staging with the common finite-loss training reporter."""
from h3_compat import enable_cpu_weight_staging
enable_cpu_weight_staging()
import runpy
import json
import os
from pathlib import Path
import torch
if int(os.environ.get('LOCAL_RANK', '0')) == 0:
    print(json.dumps({'stage':'h3_thread_budget', 'intra':torch.get_num_threads(),
        'interop':torch.get_num_interop_threads(),
        'compile_threads':os.environ['TORCHINDUCTOR_COMPILE_THREADS']}), flush=True)
try:
    runpy.run_path('/opt/nexus/train.py', run_name='__main__')
except BaseException:
    counters = {}
    for name in ('pids.current', 'pids.max', 'pids.events', 'memory.events'):
        path = Path('/sys/fs/cgroup') / name
        if path.exists(): counters[name] = path.read_text().strip()
    print(json.dumps({'stage':'h3_failure_resources', 'rank':os.environ.get('RANK'),
                      'cgroup':counters}), flush=True)
    raise
