"""Runs the official pinned Atrex optimizer inside one disposable offline container."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import math
import os
from pathlib import Path
import shutil
import signal
import statistics
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request


class Proxy(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_POST(self):
        connection = http.client.HTTPConnection('localhost', timeout=120)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect('/model/model.sock')
        connection.sock = sock
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 8 * 1024 * 1024:
                self.send_error(413); return
            connection.request('POST', self.path, self.rfile.read(size), {
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + os.environ['ATREX_JOB_CAPABILITY'],
            })
            response = connection.getresponse()
            self.send_response(response.status)
            self.send_header('Content-Type', response.getheader('Content-Type', 'application/json'))
            self.send_header('Connection', 'close')
            self.end_headers()
            while chunk := response.read1(8192):
                self.wfile.write(chunk); self.wfile.flush()
        finally:
            connection.close()
            self.close_connection = True


def measure_reference(operator):
    """Seed speedup denominators from the official evaluator on this actual GPU."""
    shapes = json.loads((operator/'shapes.json').read_text())
    metadata_path = operator/'metadata.json'
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {'id':'nexus_input'}
    Path('/work/output/submitted_metadata.json').write_text(json.dumps(metadata))
    metadata_path.write_text(json.dumps(metadata))
    directory = Path('/work/output/reference_baseline')
    print(json.dumps({'event':'reference_measurement_started'}),flush=True)
    command = [sys.executable,'/opt/atrex-bench/scripts/run_eval.py','--input',str(operator/'reference.py'),
               '--reference-dir',str(operator),'--output',str(directory),'--num-correctness-cases','1','--bench-iters','5']
    with Path('/work/output/reference_baseline.log').open('w') as log:
        subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=300,check=True)
    results=list(directory.rglob('eval_result.json'))
    if len(results)!=1: raise RuntimeError('REFERENCE_RESULT_MISSING_OR_AMBIGUOUS')
    result = json.loads(results[0].read_text())
    if result.get('error'): raise RuntimeError('REFERENCE_EVALUATION_FAILED')
    measured = {}
    for shape in shapes:
        if result.get('passed',{}).get('correctness',{}).get(shape,{}).get('status') != 'passed':
            raise RuntimeError('REFERENCE_CORRECTNESS_FAILED')
        samples=result.get('performance',{}).get('shapes',{}).get(shape,{}).get('samples',[])
        values=[item['end_to_end_time_ms']*1000 for item in samples if isinstance(item.get('end_to_end_time_ms'),(int,float)) and math.isfinite(item['end_to_end_time_ms']) and item['end_to_end_time_ms']>0]
        if not values: raise RuntimeError('REFERENCE_TIMING_UNAVAILABLE')
        measured[shape]=statistics.median(values)
    metadata.setdefault('shapes',{})
    for shape,value in measured.items():
        metadata['shapes'].setdefault(shape,{})
        metadata['shapes'][shape]['production_performance']={'performance_us':value}
    metadata['nexus_reference_measurement']={'source':'official Atrex-Bench evaluation on assigned GPU',
        'timing_compatibility':'Triton 3.1 CUDA-event median sample; no per-iteration sample claims',
        'reference_sha256':hashlib.sha256((operator/'reference.py').read_bytes()).hexdigest(),
        'latency_us_by_shape':measured}
    metadata_path.write_text(json.dumps(metadata))
    Path('/work/output/measured_metadata.json').write_text(json.dumps(metadata))
    print(json.dumps({'event':'reference_measurement_completed','shapes':len(measured)}),flush=True)


def main():
    spec = json.loads(Path('/input/spec.json').read_text())
    work = Path('/work')
    for name in ('home', 'output', 'gateway'):
        (work / name).mkdir(exist_ok=True)
    os.environ.update(HOME='/work/home', CUDA_CACHE_PATH='/work/home/cuda-cache',
                      TORCH_EXTENSIONS_DIR='/work/home/torch-extensions',
                      ATREX_BENCH_ROOT='/opt/atrex-bench',
                      ATREX_SANDBOX_URL='http://127.0.0.1:18080', ATREX_SANDBOX_GPU='local',
                      GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='user.name', GIT_CONFIG_VALUE_0='Atrex Worker',
                      GIT_CONFIG_KEY_1='user.email', GIT_CONFIG_VALUE_1='atrex@localhost')
    server = ThreadingHTTPServer(('127.0.0.1', 18081), Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if spec.get('protocol',spec['provider']) == 'anthropic':
        agent_cli = 'claude'
        os.environ.update(ANTHROPIC_BASE_URL='http://127.0.0.1:18081',
                          ANTHROPIC_API_KEY='job-proxy',
                          ATREX_CLAUDE_SESSION_SETTINGS=json.dumps({'model': spec['model']}))
    else:
        agent_cli = 'codex'
        # Long-horizon sessions use this persistent home directly; unlike the
        # baseline's temporary home, Codex requires it to exist before startup.
        (work / 'home' / '.codex').mkdir(mode=0o700, parents=True, exist_ok=True)
        os.environ.update(OPENAI_API_KEY='job-proxy', CODEX_HOME='/work/home/.codex',
            ATREX_CODEX_SESSION_SETTINGS=json.dumps({
                'model': spec['model'], 'model_reasoning_effort': 'high',
                'model_provider': 'nexus_job',
                'model_providers.nexus_job.name': 'Caller-authorized job proxy',
                'model_providers.nexus_job.base_url': 'http://127.0.0.1:18081/v1',
                'model_providers.nexus_job.env_key': 'OPENAI_API_KEY',
                'model_providers.nexus_job.wire_api': 'responses',
                'model_providers.nexus_job.supports_websockets': False,
            }))
    # Input files are immutable snapshots; native layouts need the official evaluator ancestor.
    shutil.copytree('/opt/atrex-bench', '/work/bench', ignore=shutil.ignore_patterns('.git', '__pycache__'))
    subprocess.run(['git', 'init', '-q', '/work/bench'], check=True)
    operator = Path('/work/bench/data/nexus_input')
    operator.mkdir(parents=True, exist_ok=True)
    for source in Path('/input/files').iterdir():
        if source.is_file() and not source.is_symlink():
            shutil.copyfile(source, operator / source.name)
    measure_reference(operator)
    gateway = subprocess.Popen([sys.executable, '/opt/atrex/tools/local_gateway.py', 'serve',
        '--port', '18080', '--state-dir', '/work/gateway', '--gpu-alias', spec['platform'],
        '--atrex-bench-root', '/opt/atrex-bench'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            if gateway.poll() is not None:
                raise RuntimeError('GPU_GATEWAY_START_FAILED')
            try:
                with urllib.request.urlopen('http://127.0.0.1:18080/healthz', timeout=1) as response:
                    if response.status == 200: break
            except OSError: time.sleep(1)
        else: raise RuntimeError('GPU_GATEWAY_TIMEOUT')
        command = [sys.executable, '-m', 'orchestrator.optimize', '--op-dir', str(operator),
            '--platform', spec['platform'], '--sandbox-hardware', 'local',
            '--sandbox-url', 'http://127.0.0.1:18080', '--framework', spec['framework'],
            '--agent-cli', agent_cli, '--workspace', '/work/output', '--optimization-mode', 'production',
            '--max-iters', str(spec['max_iters']), '--token-budget', str(spec['token_budget']),
            '--fast-episodes', str(spec['max_iters']),
            '--notes', 'Assigned GPU is Pascal compute capability 6.0. Use CUDA compatible with sm_60. Triton and Nsight Compute hardware counters are unavailable. Use evaluator correctness and CUDA event timing; never claim hardware-counter measurements. Declare all directly used native libraries in solution.json dependencies and dependency_roles: NVRTC (nvrtc) when compiling with nvrtcCompileProgram, and CUDA Driver (cuda-driver) when launching with cuLaunchKernel; ctypes alone is not their declaration. Keep the manifest accurate when changing the loader. Investigate both GPU memory access and host launch overhead; a float4-only trial previously remained slower than the reference. Any claimed speedup must be measured, not inferred.',
            '--fast-trials', '1', '--setup-timeout', str(spec['timeout_seconds']),
            '--sandbox-timeout', '600']
        print(json.dumps({'event':'optimizer_started', 'framework': spec['framework'], 'model': spec['model']}), flush=True)
        return subprocess.call(command, cwd='/opt/atrex')
    finally:
        gateway.terminate()
        server.shutdown()


if __name__ == '__main__':
    # Keep the tmpfs mounted until the broker has collected outputs, including failures.
    try:
        exit_code = main()
    except Exception as exc:
        print('WORKER_FAILED ' + type(exc).__name__, flush=True)
        traceback.print_exc()
        exit_code = 1
    Path('/work/finished.json').write_text(json.dumps({'exit_code': exit_code}))
    while True:
        time.sleep(30)
