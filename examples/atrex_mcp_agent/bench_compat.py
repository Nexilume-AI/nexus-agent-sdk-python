"""Build-time compatibility patch for the pinned evaluator with Torch 2.5/Triton 3.1.

Triton 3.1 rejects return_mode='all' with AssertionError. Keep its real CUDA-event
timing and use an explicitly labelled median sample; never swallow evaluator errors.
"""
from pathlib import Path

path=Path('/opt/atrex-bench/src/atrex_bench/eval/performance.py')
source=path.read_text()
anchor='    raw_times = _bench_all_runs(\n'
assert source.count(anchor)==1,'Pinned evaluator source changed; review compatibility patch'
compat='''    # Nexus Pascal compatibility: Triton 3.1 has no return_mode="all".
    # Its median still comes from real CUDA-event measurements.
    import triton
    if triton.__version__ == "3.1.0":
        elapsed_ms = do_bench(bench_fn, warmup=warmup_iters, rep=bench_iters, return_mode="median")
        return _MeasurementResult(samples=[PerformanceSample(end_to_end_time_ms=float(elapsed_ms))])

'''
path.write_text(source.replace(anchor,compat+anchor))
Path('/opt/atrex-bench/NEXUS_COMPATIBILITY.md').write_text(
    'Pinned Atrex-Bench 22d6a57b98ebb45c8db76e98815158d820c1dca3 with Nexus Pascal compatibility. '
    'Triton 3.1 timing returns one real CUDA-event median sample instead of unsupported return_mode=all. '
    'Correctness checks are unchanged. See bench_compat.py in the Nexus integration.\n')
