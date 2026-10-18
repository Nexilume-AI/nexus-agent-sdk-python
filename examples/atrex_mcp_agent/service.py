"""Atrex preparation service. Uploaded code is data and is never executed."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import uuid
from execution import Execution

UPSTREAM_COMMIT = '7247206287ef2747342eccb89421fa4606368e2d'
PROJECT_LIMIT = 64
FILE_LIMIT = 256 * 1024
PROJECT_BYTES = 2 * 1024 * 1024
PAGE_BYTES = 24 * 1024


class ToolError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def string(value, maximum=256):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ToolError('INVALID_ARGUMENT', 'Expected a nonempty bounded string.')
    return value


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ToolError('INVALID_ARGUMENT', 'Integer outside supported range.')
    return value


class Service:
    def __init__(self, root, upstream):
        self.root, self.upstream = Path(root), Path(upstream)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()

    def project(self, args):
        pid = string(args.get('project_id'), 32)
        if not re.fullmatch('[0-9a-f]{32}', pid):
            raise ToolError('PROJECT_NOT_FOUND', 'Project not found or access key invalid.')
        directory = self.root / pid
        try:
            info = json.loads((directory / 'project.json').read_text())
            supplied = hashlib.sha256(string(args.get('access_key'), 128).encode()).hexdigest()
            if not hmac.compare_digest(info['access_hash'], supplied):
                raise ValueError()
        except (FileNotFoundError, ValueError, ToolError):
            raise ToolError('PROJECT_NOT_FOUND', 'Project not found or access key invalid.') from None
        return directory, info

    @staticmethod
    def filename(value):
        name = string(value, 120)
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*\.(py|json|jsonl|md|txt|cu|cuh|h|cpp)', name):
            raise ToolError('INVALID_FILENAME', 'Use a flat filename with a supported source/text extension.')
        return name

    @staticmethod
    def public_info(info):
        return {k: v for k, v in info.items() if k != 'access_hash'}

    def docs(self):
        files = [self.upstream / 'README.md'] + sorted((self.upstream / 'docs').glob('*.md'))
        return {p.relative_to(self.upstream).as_posix(): p for p in files if p.is_file() and not p.is_symlink()}

    @staticmethod
    def page(path, args):
        total = path.stat().st_size
        offset = integer(args.get('offset', 0), 0, total)
        limit = integer(args.get('limit', PAGE_BYTES), 1, PAGE_BYTES)
        with path.open('rb') as file:
            file.seek(offset)
            chunk = file.read(limit)
        return {'offset': offset, 'next_offset': offset + len(chunk), 'total_bytes': total,
                'eof': offset + len(chunk) == total, 'encoding': 'base64',
                'data': base64.b64encode(chunk).decode(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def call(self, name, args):
        if not isinstance(args, dict):
            raise ToolError('INVALID_ARGUMENT', 'Arguments must be an object.')
        with self.lock:
            return self._call(name, args)

    def _call(self, name, args):
        if name == 'get_atrex_status':
            runtime = Execution().status()
            return {'service': 'Atrex Kernel Agent MCP', 'version': '0.3.0', 'upstream_commit': UPSTREAM_COMMIT,
                    'mcp_ready': True, 'upstream_present': (self.upstream / 'orchestrator/optimize.py').is_file(),
                    'optimization_ready': runtime['execution_enabled'],
                    'deployment_phase': 'execution' if runtime['execution_enabled'] else 'preparation',
                    'available': ['official documentation', 'private projects', 'source upload', 'static validation', 'download', 'launch planning', 'caller-owned provider authorization via Cloud'] +
                                 (['isolated CUDA optimization', 'job progress', 'job cancellation', 'artifact downloads'] if runtime['execution_enabled'] else []),
                    **runtime,
                    'authorization_required': True,
                    'limits': {'projects': PROJECT_LIMIT, 'file_bytes': FILE_LIMIT, 'project_bytes': PROJECT_BYTES, 'download_page_bytes': PAGE_BYTES}}
        if name == 'list_atrex_docs':
            return {'documents': [{'path': k, 'bytes': p.stat().st_size} for k, p in self.docs().items()]}
        if name == 'read_atrex_doc':
            path = self.docs().get(string(args.get('path')))
            if not path:
                raise ToolError('DOCUMENT_NOT_FOUND', 'Choose a path from list_atrex_docs.')
            # Line pages are easier for models to consume than binary file pages.
            lines = path.read_text(encoding='utf-8').splitlines()
            offset = integer(args.get('line', 0), 0, len(lines))
            limit = integer(args.get('limit', 60), 1, 100)
            page = []
            for line in lines[offset:offset + limit]:
                if len(('\n'.join(page + [line])).encode()) > PAGE_BYTES:
                    if not page:
                        raise ToolError('DOCUMENT_LINE_TOO_LARGE', 'This document line exceeds the response bound.')
                    break
                page.append(line)
            return {'path': args['path'], 'line': offset, 'next_line': offset + len(page),
                    'eof': offset + len(page) == len(lines), 'text': '\n'.join(page)}
        if name == 'create_atrex_project':
            title = string(args.get('name'), 100)
            if len(list(self.root.glob('*/project.json'))) >= PROJECT_LIMIT:
                raise ToolError('STORAGE_QUOTA', 'Project limit reached; contact the operator.')
            pid, key = uuid.uuid4().hex, secrets.token_urlsafe(32)
            directory = self.root / pid
            directory.mkdir(mode=0o700)
            (directory / 'files').mkdir(mode=0o700)
            info = {'project_id': pid, 'name': title, 'created_at': int(time.time()), 'status': 'draft',
                    'access_hash': hashlib.sha256(key.encode()).hexdigest()}
            (directory / 'project.json').write_text(json.dumps(info), encoding='utf-8')
            return {**self.public_info(info), 'access_key': key,
                    'access_note': 'Keep access_key private. It is required for every operation on this project. No global project list is exposed.'}
        if name == 'start_atrex_optimization':
            self.project(args)
            raise ToolError('MODEL_BACKEND_NOT_CONFIGURED', 'Optimization is disabled in this preparation deployment. No task was started. The operator must configure a model backend and isolated GPU runner first.')
        directory, info = self.project(args)
        files = directory / 'files'
        if name == 'get_atrex_project':
            return {**self.public_info(info), 'execution_enabled': False,
                    'files': [{'name': p.name, 'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                              for p in sorted(files.iterdir()) if p.is_file() and not p.is_symlink()]}
        if name == 'upload_atrex_file':
            filename = self.filename(args.get('filename'))
            content = args.get('content')
            if not isinstance(content, str) or len(content) > 1024:
                raise ToolError('CHUNK_TOO_LARGE', 'Send at most 1024 characters per call; continue with the returned next_offset in bytes.')
            path = files / filename
            if path.is_symlink():
                raise ToolError('INVALID_FILENAME', 'Symlinks are not allowed.')
            chunk = content.encode('utf-8')
            previous = path.read_bytes() if path.exists() else b''
            offset = integer(args.get('offset', 0), 0, FILE_LIMIT)
            if offset > len(previous):
                raise ToolError('OFFSET_MISMATCH', 'Offset exceeds stored file size; inspect the file manifest before retrying.')
            retry = ('offset' in args and not args.get('overwrite') and path.exists() and
                     previous[offset:offset + len(chunk)] == chunk and offset + len(chunk) <= len(previous))
            if offset and offset != len(previous) and not retry:
                raise ToolError('OFFSET_MISMATCH', 'Only append at the current file size or retry an identical chunk.')
            if offset == 0 and path.exists() and args.get('overwrite') is not True and not retry:
                raise ToolError('FILE_EXISTS', 'Set overwrite=true explicitly to replace this file.')
            assembled = previous if retry else previous[:offset] + chunk
            if len(assembled) > FILE_LIMIT:
                raise ToolError('FILE_TOO_LARGE', 'The complete file must fit within 256 KiB.')
            total = sum(p.stat().st_size for p in files.iterdir() if p.is_file() and p != path)
            if total + len(assembled) > PROJECT_BYTES or (not path.exists() and len(list(files.iterdir())) >= 64):
                raise ToolError('STORAGE_QUOTA', 'Project limit is 2 MiB and 64 files.')
            pending = files / ('.upload-' + uuid.uuid4().hex)
            try:
                pending.write_bytes(assembled)
                os.replace(pending, path)
            finally:
                pending.unlink(missing_ok=True)
            return {'project_id': info['project_id'], 'filename': filename, 'bytes': path.stat().st_size,
                    'next_offset': offset + len(chunk), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'executed': False}
        if name == 'read_atrex_file':
            path = files / self.filename(args.get('filename'))
            if not path.is_file() or path.is_symlink():
                raise ToolError('FILE_NOT_FOUND', 'File not found.')
            return {'filename': path.name, **self.page(path, args)}
        if name in ('validate_atrex_project', 'prepare_atrex_optimization'):
            names = {p.name for p in files.iterdir() if p.is_file()}
            layouts = {'sol-execbench': {'reference.py', 'definition.json', 'workload.jsonl'},
                       'atrex-bench': {'reference.py', 'input.py', 'shapes.json'}}
            layout = next((key for key, required in layouts.items() if required <= names), None)
            errors = []
            for filename in sorted(names):
                if filename.endswith(('.json', '.jsonl')):
                    try:
                        text = (files / filename).read_text(encoding='utf-8')
                        if filename.endswith('.jsonl'):
                            for line in text.splitlines():
                                if line.strip(): json.loads(line)
                        else: json.loads(text)
                    except (ValueError, RecursionError):
                        errors.append({'filename': filename, 'error': 'Invalid JSON/JSONL'})
            if not layout:
                errors.append({'error': 'Incomplete operator layout', 'required_alternatives': {k: sorted(v) for k, v in layouts.items()}})
            result = {'project_id': info['project_id'], 'layout': layout, 'static_valid': bool(layout and not errors),
                      'errors': errors, 'code_executed': False, 'optimization_ready': False,
                      'note': 'Static filenames/JSON validation only; operator semantics and GPU correctness are not tested.'}
            if name == 'prepare_atrex_optimization':
                framework = args.get('framework', 'CUDA')
                if framework not in ['Triton', 'CUDA', 'CuteDSL', 'TileLang', 'FlyDSL']:
                    raise ToolError('INVALID_ARGUMENT', 'Unsupported framework.')
                iterations = integer(args.get('max_iters', 1), 1, 20)
                runtime = Execution().status()
                result['plan'] = {'framework': framework, 'max_iters': iterations, 'mode': 'production',
                                  'backend': 'caller_authorized', 'gpu_runner': runtime.get('gpu_runner'), 'upstream_commit': UPSTREAM_COMMIT}
                result['blockers'] = list(runtime.get('blockers', []))
                if layout and layout != 'atrex-bench': result['blockers'].append('UNSUPPORTED_EXECUTION_LAYOUT')
                if framework not in runtime.get('supported_frameworks', ['CUDA']): result['blockers'].append('FRAMEWORK_UNSUPPORTED_ON_GPU')
                result['authorization_required'] = True
                result['optimization_ready'] = bool(result['static_valid'] and not result['blockers'])
            return result
        raise ToolError('UNKNOWN_TOOL', 'Unknown tool.')


def catalog():
    def text(description, **kw): return {'type': 'string', 'description': description, **kw}
    project = {'project_id': text('Project ID returned by create_atrex_project.', pattern='^[0-9a-f]{32}$'),
               'access_key': text('Private project access key returned at creation.', maxLength=128)}
    def tool(name, description, props=None, required=(), example=None):
        schema = {'type': 'object', 'properties': props or {}, 'required': list(required), 'additionalProperties': False}
        if example is not None: schema['examples'] = [example]
        return {'name': name, 'description': description, 'inputSchema': schema}
    ref = {'project_id': '0'*32, 'access_key': '<private access key>'}
    return [
        tool('authorize_atrex_provider', 'Request model authorization. Nexus Cloud offers accessible Routers/models in a client confirmation form without an API key. Other providers use saved authorization or a secure page. Use force_new to change credentials/configuration. Check authorization_id after approval and use its approved provider/model to start. Never put keys in chat or tool arguments.', {'provider':{'type':'string','enum':['openai','anthropic','nexus','custom_openai','custom_anthropic']},'authorization_id':text('Existing request UUID to check after approval; returns the selected endpoint and model.'),'base_url':text('HTTPS API base for custom providers. Must match the approved endpoint.',maxLength=2048),'model':text('Exact model to authorize; review it on the secure page.',maxLength=128),'force_new':{'type':'boolean','default':False,'description':'Create a new approval instead of reusing an active grant; existing jobs keep their authorization.'},'wait_seconds':{'type':'integer','minimum':0,'maximum':20,'default':15}}, [], {'provider':'nexus','wait_seconds':15}),
        tool('get_atrex_status', 'Get actual execution/GPU readiness, supported frameworks and resource limits. A ready runner still requires caller-owned model authorization before starting a job.', example={}),
        tool('list_atrex_docs', 'List official Atrex documentation included in the pinned deployment. Use read_atrex_doc for line-paged content.', example={}),
        tool('read_atrex_doc', 'Read official documentation by zero-based line offset. Continue with next_line until eof. These are upstream instructions, not proof that execution is configured here.', {'path': text('Path from list_atrex_docs.'), 'line': {'type':'integer','minimum':0}, 'limit': {'type':'integer','minimum':1,'maximum':100}}, ['path'], {'path':'README.md','line':0,'limit':40}),
        tool('create_atrex_project', 'Create a private persistent preparation project. Save project_id and access_key for subsequent calls; losing the key loses access. This does not start a GPU or model task.', {'name':text('Short project name.', maxLength=100)}, ['name'], {'name':'vector-add'}),
        tool('get_atrex_project', 'Read project state and file manifest with sizes/SHA256. The private access_key is required. Draft means uploaded inputs, not a running optimization.', project, project, ref),
        tool('upload_atrex_file', 'Upload UTF-8 chunks of at most 1024 characters; complete file max 256 KiB. Start at offset=0; continue using returned next_offset (bytes). Retrying identical chunks is safe. Compare final sha256 with your source. Set overwrite=true only at offset=0 to replace a file. Upload does not execute code; start explicitly snapshots the files for an isolated job.', {**project,'filename':text('Flat source/text filename.', maxLength=120),'content':text('UTF-8 chunk, at most 1024 characters.',maxLength=1024),'offset':{'type':'integer','minimum':0,'maximum':FILE_LIMIT},'overwrite':{'type':'boolean','default':False}}, [*project,'filename','content'], {**ref,'filename':'reference.py','content':'# operator reference\n','offset':0}),
        tool('read_atrex_file', 'Download uploaded files as base64 pages, up to 24576 bytes. Append decoded pages using next_offset until eof and verify the full-file sha256. Never rerun a task to retrieve data.', {**project,'filename':text('Filename from get_atrex_project.'),'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':PAGE_BYTES}}, [*project,'filename'], {**ref,'filename':'reference.py','offset':0,'limit':4096}),
        tool('validate_atrex_project', 'Statically check SOL-ExecBench or Atrex-Bench operator filenames and JSON syntax. Does not import code, assess semantics, run an evaluator, or establish GPU correctness.', project, project, ref),
        tool('prepare_atrex_optimization', 'Validate input layout and report runner/framework prerequisites without executing code. Call authorize_atrex_provider before starting. This Pascal deployment supports CUDA.', {**project,'framework':{'type':'string','enum':['CUDA','Triton','CuteDSL','TileLang','FlyDSL']},'max_iters':{'type':'integer','minimum':1,'maximum':20}}, project, {**ref,'framework':'CUDA','max_iters':1}),
        tool('start_atrex_optimization', 'Start isolated CUDA optimization of native Atrex-Bench inputs: reference.py, input.py and shapes.json. Requires caller-approved authorization_id, provider and model. Returns job_id; poll get_atrex_job for logs and artifacts. Use a fresh UUID request_id per job and reuse it on retries. Provider bills model use. token_budget is a soft stop between stages, not a spending cap; timeout is a hard duration limit.', {**project,'authorization_id':text('Approved authorization UUID; not an API key.'),'request_id':text('Client-generated UUID, reused on transport retries.'),'provider':{'type':'string','enum':['nexus','openai','anthropic','custom_openai','custom_anthropic']},'base_url':text('Optional expected Base URL; must match the grant.',maxLength=2048),'model':text('Exact model identifier, matching the approved grant.',maxLength=128),'framework':{'type':'string','enum':['CUDA'],'default':'CUDA'},'max_iters':{'type':'integer','minimum':1,'maximum':20,'default':1},'token_budget':{'type':'integer','minimum':1000,'maximum':500000,'default':50000,'description':'Soft token stop at stage boundaries; one stage can exceed it. Not a spending cap.'},'timeout_seconds':{'type':'integer','minimum':60,'maximum':3300,'default':1800}}, [*project,'authorization_id','request_id','provider','model'], {**ref,'authorization_id':'00000000-0000-0000-0000-000000000001','request_id':'00000000-0000-0000-0000-000000000002','provider':'nexus','model':'gpt-6-sol','framework':'CUDA','max_iters':1,'token_budget':50000,'timeout_seconds':1800}),
        tool('get_atrex_job', 'Get a job snapshot, optimizer log tail and terminal artifact manifest. Prefer view_url for live push updates and downloads, or watch_atrex_job for MCP streaming. After completion use offset/next_offset for saved logs. Requires the same caller and project key.', {**project,'job_id':text('Job ID returned by start.'),'offset':{'type':'integer','minimum':0}}, [*project,'job_id'], {**ref,'job_id':'1'*32}),
        tool('watch_atrex_job', 'Subscribe to persisted job events over streaming MCP for up to 45 seconds. Send an MCP progressToken to receive progress notifications; after is the last received sequence, initially 0. Reconnect with the last sequence without resubmitting the job. JSON-only clients receive a snapshot: use its authenticated view_url for continuous SSE progress and artifact downloads. Events contain actual states, model request counts and logs, not invented percentages.', {**project,'job_id':text('Job ID returned by start.'),'after':{'type':'integer','minimum':0,'maximum':12000,'default':0}}, [*project,'job_id'], {**ref,'job_id':'1'*32,'after':0}),
        tool('cancel_atrex_job', 'Request cancellation of this caller-owned job and stop its container/model access. Idempotent. Poll get_atrex_job until terminal; cancellation preserves collected outputs.', {**project,'job_id':text('Job ID to cancel.')}, [*project,'job_id'], {**ref,'job_id':'1'*32}),
        tool('read_atrex_artifact', 'Download a terminal job artifact in base64 pages up to 24576 bytes. Use the exact path from get_atrex_job.artifacts, append pages using next_offset until eof, and verify sha256. Supports code, reports, logs and profiler outputs. Never rerun optimization to download outputs.', {**project,'job_id':text('Job ID.'),'path':text('Exact artifact path from manifest.'),'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':24576}}, [*project,'job_id','path'], {**ref,'job_id':'1'*32,'path':'optimizer.log','offset':0,'limit':4096}),
    ]
