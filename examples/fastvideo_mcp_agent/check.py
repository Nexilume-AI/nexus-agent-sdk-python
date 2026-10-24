"""No GPU/network: exercise authorization, persistence, readiness and artifact boundaries."""
import hashlib
import json
from pathlib import Path
import tempfile
import service


def main():
    with tempfile.TemporaryDirectory() as tmp:
        service.DATA = Path(tmp) / 'data'
        service.MODEL = Path(tmp) / 'models'
        service.ROOT = Path(tmp)
        first = {'version': 1, 'principal_type': 'api_key', 'owner_scope': 'a' * 64, 'quota_scope': 'b' * 64}
        other = {**first, 'owner_scope': 'c' * 64, 'quota_scope': 'd' * 64}
        app = service.Service()
        checks = []
        try:
            assert app.call('create_fastvideo_workspace', {})['isError']; checks.append('verified identity required')
            created = app.call('create_fastvideo_workspace', {}, first)['structuredContent']
            auth = {k: created[k] for k in ('workspace_id', 'workspace_key')}
            request = {**auth, 'prompts': ['A calm lake.'] * 4}
            assert app.call('validate_distillation', request, other)['isError']; checks.append('cross-caller denial')
            assert app.call('validate_distillation', {**request, 'workspace_key': 'wrong'}, first)['isError']; checks.append('capability denial')
            assert not app.call('validate_distillation', request, first)['isError']; checks.append('valid request')
            for extra in ({'num_frames': 2}, {'width': 67}, {'max_train_steps': True}, {'timeout_seconds': 999999}, {'prompts': ['x']}):
                assert app.call('validate_distillation', {**request, **extra}, first)['isError']
            checks.append('bounded training configuration')
            assert app.call('start_distillation', request, first)['isError']; checks.append('unready runtime rejected')
            w = app.workspaces[auth['workspace_id']]
            assert auth['workspace_key'] not in (w['folder'] / 'workspace.json').read_text(); checks.append('no plaintext workspace capability persisted')
            folder = w['folder'] / 'jobs' / 'test'; folder.mkdir(parents=True)
            payload = b'native result\n'; (folder / 'result.bin').write_bytes(payload)
            j = {'id': 'test', 'quota': w['quota'], 'state': 'succeeded', 'folder': folder, 'container': 'unused'}
            w['jobs']['test'] = j; app.save_job(j)
            got = app.call('read_artifact', {**auth, 'job_id': 'test', 'path': 'result.bin', 'include_sha256': True}, first)
            assert got['structuredContent']['sha256'] == hashlib.sha256(payload).hexdigest(); checks.append('artifact digest')
            for path in ('../../workspace.json', 'job.json', str(Path(tmp) / 'secret')):
                assert app.call('read_artifact', {**auth, 'job_id': 'test', 'path': path}, first)['isError']
            checks.append('artifact traversal and metadata denial')
        finally: app.close()
        restored = service.Service()
        try:
            assert not restored.call('job_status', {**auth, 'job_id': 'test'}, first)['isError']; checks.append('persisted caller binding')
            assert not restored.call('close_fastvideo_workspace', auth, first)['isError']
            assert restored.call('job_status', {**auth, 'job_id': 'test'}, first)['isError']; checks.append('close revokes capability')
        finally: restored.close()
        print(json.dumps({'passed': checks}, indent=2))


if __name__ == '__main__': main()
