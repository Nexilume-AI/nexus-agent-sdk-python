"""Caller-bound, bounded FastVideo jobs. No caller-supplied commands or YAML targets."""
import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
import uuid
from model_registry import Registry, BASE_ID
from file_bridge import sha256
from h3_recipe import H3_ID, H3_GPUS, H3_REVISION, status as h3_status, validate as validate_h3

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
MODEL = ROOT / 'models' / 'wan2.1-1.3b'
IMAGE = 'nexus-fastvideo-mcp:fa2d1324c600-h3-v3'
COMMIT = 'fa2d1324c60050cd65209c6aaeac470e7fb412f5'
AUTH = {'workspace_id': {'type': 'string'}, 'workspace_key': {'type': 'string'}}
S = {'type': 'string'}
PARAMS = {
    'model_id': {'type':'string', 'default':BASE_ID, 'description':'Use list_models: official base or your validated private checkpoint.'},
    'prompts': {'type': 'array', 'items': {'type': 'string', 'maxLength': 1500}, 'minItems': 4, 'maxItems': 64},
    'max_train_steps': {'type': 'integer', 'minimum': 5, 'maximum': 4000, 'default': 10},
    'height': {'type': 'integer', 'minimum': 64, 'maximum': 256, 'default': 128},
    'width': {'type': 'integer', 'minimum': 64, 'maximum': 256, 'default': 128},
    'num_frames': {'type': 'integer', 'minimum': 1, 'maximum': 124, 'default': 5,
        'description':'Wan: 1,5,9,13,17. H3: exactly 124; omitted H3 defaults to 124.'},
    'seed': {'type': 'integer', 'minimum': 0, 'maximum': 2147483647, 'default': 42},
    'timeout_seconds': {'type': 'integer', 'minimum': 60, 'maximum': 86400, 'default': 3600},
}


def result(value, error=False):
    return {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value, 'isError': error}


def tool(name, description, props=None, required=(), auth=True):
    return {'name': name, 'description': description, 'inputSchema': {'type': 'object',
        'properties': {**(AUTH if auth else {}), **(props or {})},
        'required': [*(AUTH if auth else {}), *required], 'additionalProperties': False}}


CATALOG = [
    tool('list_models', 'List Wan 2.1 T2V 1.3B, MiniMax H3 T2VA and this workspace\'s private checkpoints. Check per-family ready/state before use; provisioning does not mean the model can run.'),
    tool('begin_model_import', 'Import compatible full transformer safetensors for the selected ready family (Wan 1.3B or H3 T2VA). Supply exact sizes and SHA256 hashes. Returns one-time authenticated browser upload links with resumable chunks. Shared text encoder/VAE/tokenizer come from the verified base. No pickle, Python, arbitrary URLs or paths.', {'name':S,'family':{'type':'string','enum':[BASE_ID,H3_ID],'default':BASE_ID},'weight_files':{'type':'array','minItems':1,'maxItems':64,'items':{'type':'object','properties':{'name':S,'size_bytes':{'type':'integer'},'sha256':S},'required':['name','size_bytes','sha256'],'additionalProperties':False}}}, ('name','weight_files')),
    tool('get_model_import', 'Read upload byte offsets and asynchronous safetensors validation status. Ready means structurally compatible and finite, not quality certified.', {'model_id':S}, ('model_id',)),
    tool('renew_model_upload', 'Renew an expired upload link, preserving uploaded bytes. Old link is revoked. Continue from offset returned by the new link.', {'model_id':S,'name':S}, ('model_id','name')),
    tool('finalize_model_import', 'Seal uploaded files and start background CPU validation: full SHA256, tensor names/shapes, finite values. Poll get_model_import until ready before training.', {'model_id':S}, ('model_id',)),
    tool('register_model_from_job', 'Register your completed distilled student as a selectable private checkpoint. Validates it in the background; returns model_id.', {'job_id':S,'name':S}, ('job_id','name')),
    tool('get_artifact_download', 'Create a one-time authenticated HTTPS download link for a completed job artifact. Redeem within 10 minutes; download session lasts one hour. Supports HTTP Range, ETag and SHA256. Renew via this tool after expiry and resume at the same offset. Large weights stay on the Agent host.', {'job_id':S,'path':S}, ('job_id','path')),
    tool('revoke_file_link', 'Immediately revoke one of this workspace\'s upload/download capabilities.', {'bridge_id':S}, ('bridge_id',)),
    tool('get_fastvideo_status', 'Inspect runtime readiness, the installed recipe and GPU resource limits. Readiness is separate from model quality.', auth=False),
    tool('list_recipes', 'List installed model-to-model acceleration recipes, input requirements and output contracts. Not a general model converter.', auth=False),
    tool('fastvideo_tool_help', 'Get the complete input schema and description of a tool.', {'tool_name': S}, ('tool_name',), auth=False),
    tool('create_fastvideo_workspace', 'Create a private workspace bound to the verified Nexus caller. Same API key means the same caller. Save workspace_id/key. Maximum two workspaces per caller.', auth=False),
    tool('validate_distillation', 'Validate a selected-model DMD2 request without launching training. Prompts become text-only conditioning data; no arbitrary Python or YAML is accepted. Wan uses four GPUs; H3 uses sixteen GPUs. Dense SDPA compatibility mode. A few training steps are a pipeline check, not evidence of a useful distilled model.', PARAMS, ('prompts',)),
    tool('start_distillation', 'Queue native FastVideo DMD2: Wan 1.3B -> four-step student, or ready H3 T2VA -> eight-forward student with merged LoRA. Same model size; check readiness with list_models. Returns job_id immediately. Saves native checkpoints, student safetensors, configuration and training report. One global GPU job, FIFO with at most one active/queued job per caller. Actual quality depends on prompts and training budget.', PARAMS, ('prompts',)),
    tool('job_status', 'Read your job state, actual elapsed time, bounded log tail and available reports. queued/running/succeeded/failed/cancelled/timed_out; never implies an acceleration or quality target was met.', {'job_id': S}, ('job_id',)),
    tool('cancel_job', 'Cancel your queued or running job; preserve files already written.', {'job_id': S}, ('job_id',)),
    tool('list_artifacts', 'List your job artifacts and byte sizes. Use get_artifact_download for large weights and resumable HTTPS downloads. Workspace files survive service restarts.', {'job_id': S}, ('job_id',)),
    tool('read_artifact', 'Read up to 65536 bytes as base64 with offset/next_offset/eof. Obtain a full-file SHA256 only when include_sha256=true on a completed job. Download binary safetensors with a programmatic MCP client.', {'job_id': S, 'path': S, 'offset': {'type': 'integer', 'minimum': 0, 'default': 0}, 'include_sha256': {'type': 'boolean', 'default': False}}, ('job_id', 'path')),
    tool('close_fastvideo_workspace', 'Close your workspace after downloading outputs. Refuses while jobs run or remain queued. This revokes further MCP access; files are retained for operator recovery.'),
]


def validated(args, family=BASE_ID):
    values = {k: args.get(k, v.get('default')) for k, v in PARAMS.items()}
    if family == H3_ID and 'num_frames' not in args: values['num_frames'] = 124
    if family == H3_ID and 'timeout_seconds' not in args: values['timeout_seconds'] = 21600
    prompts = values['prompts']
    if not isinstance(prompts, list) or not 4 <= len(prompts) <= 64 or any(not isinstance(p, str) or not p.strip() or len(p) > 1500 for p in prompts):
        raise ValueError('Provide 4..64 nonempty prompts, at most 1500 characters each.')
    for key, schema in PARAMS.items():
        if schema['type'] == 'integer' and (type(values[key]) is not int or not schema['minimum'] <= values[key] <= schema['maximum']):
            raise ValueError('Invalid ' + key)
    if family == H3_ID: return validate_h3(values)
    if values['height'] % 16 or values['width'] % 16 or (values['num_frames'] - 1) % 4 or values['num_frames'] > 17:
        raise ValueError('Width/height must be multiples of 16; num_frames must be 1,5,9,13,17.')
    return values


class Service:
    def __init__(self):
        DATA.mkdir(mode=0o700, exist_ok=True)
        self.catalog = copy.deepcopy(CATALOG)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.workspaces = {}
        self.pending = []
        self.active = None
        operator_job = ROOT/'h3-probe/job.json'
        if operator_job.is_file():
            old = json.loads(operator_job.read_text())
            if old['state'] in ('running','queued'):
                subprocess.run(['docker','rm','-f',old['container']],capture_output=True,timeout=30)
                old['state']='interrupted'
                operator_job.write_text(json.dumps(old))
        for p in DATA.glob('*/workspace.json'):
            w = json.loads(p.read_text())
            w['folder'] = p.parent
            w['jobs'] = {}
            for f in p.parent.glob('jobs/*/job.json'):
                j = json.loads(f.read_text()); j['folder'] = f.parent
                if j['state'] in ('running', 'queued'):
                    subprocess.run(['docker', 'rm', '-f', j['container']], capture_output=True, timeout=30)
                    j['state'] = 'interrupted'; self.save_job(j)
                w['jobs'][j['id']] = j
            if not w.get('closed'):
                self.workspaces[w['id']] = w
                w['registry'] = Registry(w, MODEL, IMAGE, ROOT)
        threading.Thread(target=self.run_queue, daemon=True).start()

    def save_workspace(self, w):
        fields = {k: w[k] for k in ('id', 'key_hash', 'owner', 'quota', 'closed')}
        p = w['folder'] / 'workspace.json'; tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(fields)); tmp.replace(p)

    def save_job(self, j):
        fields = {k: v for k, v in j.items() if k not in ('folder', 'process')}
        p = j['folder'] / 'job.json'; tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(fields)); tmp.replace(p)

    def authorized(self, args, identity):
        w = self.workspaces.get(args.get('workspace_id'))
        key = args.get('workspace_key')
        if not w or not isinstance(key, str) or not hmac.compare_digest(w['owner'], identity['owner_scope']) or not hmac.compare_digest(w['key_hash'], hashlib.sha256(key.encode()).hexdigest()):
            raise ValueError('WORKSPACE_NOT_FOUND')
        return w

    def call(self, name, args, identity=None, context=None):
        try:
            if name not in ('get_fastvideo_status', 'list_recipes', 'fastvideo_tool_help'):
                if not isinstance(identity, dict) or identity.get('version') != 1 or identity.get('principal_type') not in ('api_key', 'user', 'service_account') or any(not isinstance(identity.get(k), str) or not re.fullmatch('[0-9a-f]{64}', identity[k]) for k in ('owner_scope', 'quota_scope')):
                    raise ValueError('VERIFIED_CALLER_REQUIRED')
            with self.lock:
                return result(self.dispatch(name, args, identity, context))
        except (ValueError, KeyError, OSError) as exc:
            return result({'error': 'FASTVIDEO_REQUEST_FAILED', 'message': str(exc)[:800]}, True)

    def dispatch(self, name, a, identity, context=None):
        model_ready = (MODEL / 'READY.json').is_file()
        runtime_ready = (ROOT / 'runtime-ready.json').is_file()
        ready = model_ready and runtime_ready
        if name == 'get_fastvideo_status':
            return {'upstream': 'hao-ai-lab/FastVideo', 'commit': COMMIT, 'model_ready': model_ready, 'runtime_ready': runtime_ready, 'ready': ready,
                'purpose': 'model distillation', 'recipe': 'wan2.1-1.3b-dmd2-4step', 'tools': len(CATALOG),
                'limits': {'global_running_jobs': 1, 'gpu_count_per_job': 4, 'max_queued_jobs': 8,
                    'max_steps': 4000, 'max_wall_seconds': 86400, 'max_resolution': 256, 'max_frames': 17},
                'hardware': 'SM60 compatibility mode, dense Torch SDPA, no sparse-attention speed claims',
                'minimax_h3': h3_status(ROOT)}
        if name == 'list_recipes':
            return {'recipes': [{'id': 'wan2.1-1.3b-dmd2-4step', 'input_model': 'Wan-AI/Wan2.1-T2V-1.3B-Diffusers',
                'input_revision': '0fad780a534b6463e45facd96134c9f345acfa5b', 'conditioning': '4..64 text prompts',
                'method': 'Native FastVideo DMD2 with teacher, student and critic', 'target_steps': [1000, 750, 500, 250],
                'output': ['student safetensors', 'native resumable checkpoints', 'resolved config', 'training report'],
                'note': 'Same parameter count. Four inference steps is a target schedule, not a measured speedup or quality guarantee.', 'model_ready': ready}, h3_status(ROOT)]}
        if name == 'fastvideo_tool_help':
            item = next((t for t in CATALOG if t['name'] == a.get('tool_name')), None)
            if not item: raise ValueError('Unknown tool')
            return item
        if name == 'create_fastvideo_workspace':
            if sum(w['quota'] == identity['quota_scope'] for w in self.workspaces.values()) >= 2 or len(self.workspaces) >= 16:
                raise ValueError('WORKSPACE_LIMIT')
            wid = uuid.uuid4().hex; key = secrets.token_urlsafe(32); folder = DATA / wid; folder.mkdir(mode=0o700)
            w = {'id': wid, 'owner': identity['owner_scope'], 'quota': identity['quota_scope'], 'key_hash': hashlib.sha256(key.encode()).hexdigest(), 'folder': folder, 'jobs': {}, 'closed': False}
            self.workspaces[wid] = w; self.save_workspace(w)
            w['registry'] = Registry(w, MODEL, IMAGE, ROOT)
            return {'workspace_id': wid, 'workspace_key': key, 'next_step': 'validate_distillation'}
        w = self.authorized(a, identity)
        registry = w['registry']
        if name == 'list_models': return {'models':[registry.resolve(BASE_ID)[1], h3_status(ROOT)] + [registry.public(r) for r in registry.rows.values()], 'families':[BASE_ID,H3_ID], 'arbitrary_architectures_supported':False}
        if name == 'begin_model_import': return registry.begin(a, context)
        if name in ('get_model_import','renew_model_upload','finalize_model_import'):
            row = registry.rows.get(a.get('model_id'))
            if not row: raise ValueError('MODEL_NOT_FOUND')
            if name == 'get_model_import': return registry.public(row)
            if name == 'renew_model_upload': return registry.upload(row, a.get('name'), context)
            return registry.finalize(row)
        if name == 'revoke_file_link':
            registry.bridges.revoke(a.get('bridge_id')); return {'revoked':True}
        if name in ('validate_distillation', 'start_distillation'):
            model_path, provenance = registry.resolve(a.get('model_id', BASE_ID))
            family = provenance['family']
            config = validated(a, family)
            config['model_provenance'] = provenance
            ready = h3_status(ROOT)['ready'] if family == H3_ID else ready
            if name == 'validate_distillation': return {'valid': True, 'model_ready': ready, 'config': config, 'target_inference_steps': 8 if family == H3_ID else 4}
            if not ready: raise ValueError('RUNTIME_NOT_READY: operator is still provisioning and validating the runtime/base checkpoint.')
            alljobs = [j for ws in self.workspaces.values() for j in ws['jobs'].values()]
            if any(j['quota'] == w['quota'] and j['state'] in ('running', 'queued') for j in alljobs): raise ValueError('CALLER_JOB_LIMIT')
            if len(self.pending) >= 8 or len(w['jobs']) >= 4: raise ValueError('JOB_CAPACITY_REACHED')
            jid = uuid.uuid4().hex; folder = w['folder'] / 'jobs' / jid; folder.mkdir(parents=True, mode=0o700)
            (folder / 'request.json').write_text(json.dumps(config))
            j = {'id': jid, 'quota': w['quota'], 'state': 'queued', 'created': time.time(), 'folder': folder, 'container': 'nexus-fastvideo-job-' + jid, 'timeout': config['timeout_seconds']}
            j['model_path'] = str(model_path)
            j['family'] = family
            w['jobs'][jid] = j; self.save_job(j); self.pending.append(j); self.wake.set()
            return {'job_id': jid, 'state': 'queued', 'queue_position': len(self.pending), 'next_step': 'job_status'}
        if name == 'close_fastvideo_workspace':
            if any(j['state'] in ('running', 'queued') for j in w['jobs'].values()): raise ValueError('Cancel active jobs first')
            if any(r['state']=='validating' for r in registry.rows.values()): raise ValueError('Wait for model validation to finish')
            w['closed'] = True; self.save_workspace(w); del self.workspaces[w['id']]
            registry.bridges.close()
            return {'closed': True}
        j = w['jobs'].get(a.get('job_id'))
        if not j: raise ValueError('JOB_NOT_FOUND')
        if name == 'register_model_from_job': return registry.from_job(j, a.get('name'))
        if name == 'get_artifact_download':
            if j['state'] in ('running','queued'): raise ValueError('Wait for job completion')
            path = a.get('path')
            if not isinstance(path,str) or len(path)>300: raise ValueError('Invalid path')
            p = (j['folder']/path).resolve()
            if not p.is_relative_to(j['folder']) or not p.is_file() or p.name=='job.json': raise ValueError('ARTIFACT_NOT_FOUND')
            return registry.bridges.create(context, p, 'download', sha256(p), p.stat().st_size)
        if name == 'cancel_job':
            if j['state'] == 'queued': self.pending.remove(j); j['state'] = 'cancelled'; self.save_job(j)
            elif j['state'] == 'running':
                j['forced'] = 'cancelled'; subprocess.run(['docker', 'rm', '-f', j['container']], capture_output=True, timeout=30)
            return {'job_id': j['id'], 'state': j['state'], 'cancellation_requested': True}
        if name == 'job_status':
            out = {'job_id': j['id'], 'state': j['state'], 'elapsed_seconds': round(j.get('ended', time.time()) - j.get('started', time.time()), 2)}
            log = j['folder'] / 'run.log'
            if log.exists():
                with log.open('rb') as f: f.seek(max(0, log.stat().st_size - 14000)); out['log_tail'] = f.read().decode(errors='replace')
            report = j['folder'] / 'report.json'
            if report.exists(): out['report'] = json.loads(report.read_text())
            return out
        if name == 'list_artifacts':
            return {'files': [{'path': str(p.relative_to(j['folder'])), 'size': p.stat().st_size} for p in j['folder'].rglob('*') if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(j['folder']) and p.name != 'job.json'][:500]}
        if name == 'read_artifact':
            path = a.get('path')
            if not isinstance(path, str) or len(path) > 300: raise ValueError('Invalid path')
            p = (j['folder'] / path).resolve()
            if not p.is_relative_to(j['folder']) or not p.is_file() or p.name == 'job.json': raise ValueError('ARTIFACT_NOT_FOUND')
            offset = a.get('offset', 0); size = p.stat().st_size
            if type(offset) is not int or not 0 <= offset <= size: raise ValueError('Invalid offset')
            with p.open('rb') as f: f.seek(offset); data = f.read(65536)
            out = {'data_base64': base64.b64encode(data).decode(), 'size': size, 'next_offset': offset + len(data), 'eof': offset + len(data) >= size}
            if a.get('include_sha256'):
                if j['state'] in ('queued', 'running'): raise ValueError('Wait for completion before hashing')
                h = hashlib.sha256()
                with p.open('rb') as f:
                    while chunk := f.read(4 * 1024 * 1024): h.update(chunk)
                out['sha256'] = h.hexdigest()
            return out
        raise ValueError('Unknown tool')

    def run_queue(self):
        while not self.stop.is_set():
            self.wake.wait(2); self.wake.clear()
            with self.lock:
                self.enqueue_h3_acceptance()
                if not self.pending: continue
                j = self.pending.pop(0); self.active = j
                j['state'] = 'running'; j['started'] = time.time(); self.save_job(j)
            is_h3 = j.get('family') == H3_ID
            gpus = H3_GPUS if is_h3 else (4,5,6,7)
            cmd = ['docker', 'run', '--rm', '--init', '--name', j['container'], '--network', 'none', '--read-only',
                '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                # H3 has 16 CUDA/autograd/NCCL ranks. Real backward exceeded 1024
                # tasks even with CPU and compiler pools bounded to one per rank.
                '--memory', '384g' if is_h3 else '192g', '--cpus', '24' if is_h3 else '12', '--pids-limit', '4096' if is_h3 else '512', '--shm-size', '4g', '--tmpfs', '/tmp:rw,nosuid,nodev,size=4g',
                '--device', '/dev/nvidiactl', '--device', '/dev/nvidia-uvm',
                '--mount', 'type=bind,src=/usr/lib/x86_64-linux-gnu/libcuda.so.1,dst=/driver/libcuda.so.1,readonly',
                '--mount', 'type=bind,src=/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1,dst=/driver/libnvidia-ml.so.1,readonly',
                '--mount', 'type=bind,src=/usr/lib/x86_64-linux-gnu/libnvidia-ptxjitcompiler.so.1,dst=/driver/libnvidia-ptxjitcompiler.so.1,readonly',
                '--mount', f'type=bind,src={j.get("model_path", MODEL)},dst=/models/{"h3" if is_h3 else "wan"},readonly', '--mount', f'type=bind,src={j["folder"]},dst=/work']
            for gpu in gpus: cmd.extend(['--device', f'/dev/nvidia{gpu}'])
            # CUDA enumerates the four permitted device nodes as local indices 0..3.
            if is_h3: cmd.extend(['--entrypoint','python'])
            cmd.extend(['-e', 'CUDA_VISIBLE_DEVICES='+','.join(str(i) for i in range(len(gpus))), IMAGE])
            if is_h3: cmd.append('/opt/nexus/h3_runner.py')
            try:
                with (j['folder'] / 'run.log').open('wb') as f:
                    with self.lock:
                        p = None if j.get('forced') else subprocess.Popen(cmd, stdout=f, stderr=f)
                    code = -1
                    if p is not None:
                        deadline = time.monotonic() + j['timeout']
                        while p.poll() is None:
                            if time.monotonic() >= deadline: j['forced'] = 'timed_out'
                            if self.stop.is_set(): j['forced'] = 'interrupted'
                            if j.get('forced'):
                                # Retry across the brief CLI-start/container-create race.
                                subprocess.run(['docker', 'rm', '-f', j['container']], capture_output=True, timeout=30)
                            try: p.wait(timeout=1)
                            except subprocess.TimeoutExpired: pass
                        code = p.returncode
                with self.lock:
                    j['state'] = j.get('forced', 'succeeded' if code == 0 else 'failed'); j['exit_code'] = code
            except Exception as exc:
                with self.lock: j['state'] = 'failed'; j['error'] = type(exc).__name__
            finally:
                with self.lock:
                    j['ended'] = time.time(); self.save_job(j); self.active = None
                    if j.get('operator_acceptance'):
                        try: self.finish_h3_acceptance(j)
                        except Exception as exc:
                            j['state']='failed';j['error']='Acceptance evidence: '+type(exc).__name__
                            self.save_job(j)
                self.wake.set()

    def enqueue_h3_acceptance(self):
        """One operator-enabled acceptance job, queued alongside normal jobs.

        The flag is a host-owned file; no MCP request can create or bypass it.
        A failed/interrupted attempt stays failed until the operator reviews it.
        """
        flag = ROOT/'h3-provision-enabled.json'
        folder = ROOT/'h3-probe'
        if not flag.is_file() or (folder/'job.json').exists(): return
        if json.loads(flag.read_text()).get('revision') != H3_REVISION: return
        model = h3_status(ROOT)
        if not model['weights_verified'] or model['ready'] or len(self.pending)>=8: return
        folder.mkdir(exist_ok=True,mode=0o700)
        config = validated({'model_id':H3_ID,'prompts':[
            'A red toy car drives across a wooden table, a quiet motor hum.',
            'Gentle rain falls into a garden pond with soft splashing sounds.',
            'A candle flickers beside an open window, leaves rustling outside.',
            'A small stream flows between smooth stones with clear water sounds.'],
            'height':64,'width':64,'num_frames':124,'max_train_steps':5,
            'timeout_seconds':21600},H3_ID)
        config['model_provenance']=model
        (folder/'request.json').write_text(json.dumps(config))
        job={'id':'h3-operator-acceptance','quota':'operator-h3-acceptance',
             'state':'queued','created':time.time(),'folder':folder,
             'container':'nexus-h3-acceptance','timeout':21600,'family':H3_ID,
             'model_path':str(ROOT/'models/minimax-h3'),'operator_acceptance':True}
        self.save_job(job); self.pending.append(job)

    def finish_h3_acceptance(self, job):
        if job['state']!='succeeded': return
        report=json.loads((job['folder']/'report.json').read_text())
        if (report.get('base_revision')!=H3_REVISION or
            not all(report.get(k) is True for k in ('training_completed','export_reloaded','paired_audio_video_validated')) or
            not report.get('weight_validation',{}).get('changed_from_base') or
            not report.get('weight_validation',{}).get('all_tensors_finite')): return
        evidence={'revision':H3_REVISION,'real_weights':True,'training_completed':True,
                  'export_reloaded':True,'paired_audio_video_validated':True,
                  'quality_validated':False,'image':IMAGE,'job_id':job['id'],
                  'report_sha256':sha256(job['folder']/'report.json')}
        target=ROOT/'h3-acceptance.json'; temporary=target.with_suffix('.tmp')
        temporary.write_text(json.dumps(evidence,indent=2));temporary.replace(target)

    def close(self):
        self.stop.set(); self.wake.set()
        with self.lock:
            if self.active:
                self.active['forced'] = 'interrupted'
                subprocess.run(['docker', 'rm', '-f', self.active['container']], capture_output=True, timeout=30)
