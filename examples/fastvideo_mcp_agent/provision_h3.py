"""Operator-only, resumable H3 download; verify every file against pinned HF metadata.

Usage: python provision_h3.py MANIFEST DESTINATION
Downloads data/configs only. Never executes model repository Python.
"""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.request

BLOCK = 16 * 1024 * 1024
REPO = 'MiniMaxAI/MiniMax-H3'
REVISION = '42ed227ee7df40d41602854ae760620d6eb651fe'
COMPONENTS = {'transformer', 'text_encoder', 'tokenizer', 'processor',
              'scheduler', 'audio_scheduler', 'vae', 'audio_vae'}


def digest(path, git=False):
    h = hashlib.sha1() if git else hashlib.sha256()
    if git:
        h.update(f'blob {path.stat().st_size}\0'.encode())
    with path.open('rb') as stream:
        while chunk := stream.read(BLOCK):
            h.update(chunk)
    return h.hexdigest()


def main():
    manifest = json.loads(Path(sys.argv[1]).read_text())
    root = Path(sys.argv[2]).resolve()
    assert manifest['repo'] == REPO and manifest['revision'] == REVISION
    root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    for item in manifest['files']:
        relative = item['rfilename']
        if relative.split('/')[0] not in COMPONENTS and relative not in (
                'model_index.json', 'modular_model_index.json', 'LICENSE', 'README.md'):
            raise ValueError('Unapproved component')
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or relative.endswith(('.py', '.pkl', '.bin')):
            raise ValueError('Unsafe model file')
        size = item['size']
        git = 'lfs' not in item
        expected = item['blobId'] if git else item['lfs']['sha256']
        if path.exists() and path.stat().st_size == size and digest(path, git) == expected:
            print('VERIFIED_EXISTING', relative, flush=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + '.partial')
        completed = path.with_name(path.name + '.chunks.json')
        done = set(json.loads(completed.read_text())) if partial.exists() and completed.exists() else set()
        if not partial.exists(): partial.touch()

        def fetch(offset):
            end = min(offset + BLOCK, size) - 1
            for attempt in range(5):
                try:
                    url = ('https://modelscope.cn/models/MiniMax/MiniMax-H3/resolve/master/'
                           + relative + '?nexus_h3_chunk=' + str(offset) + '-' + str(time.time_ns()))
                    req = urllib.request.Request(url, headers={'Range': f'bytes={offset}-{end}'})
                    with urllib.request.urlopen(req, timeout=90) as response:
                        if response.status == 206:
                            if response.headers.get('Content-Range') != f'bytes {offset}-{end}/{size}':
                                raise ValueError('Range mismatch')
                        elif not (response.status == 200 and offset == 0 and end + 1 == size):
                            raise ValueError('Range not honored')
                        payload = response.read(end - offset + 2)
                    if len(payload) != end - offset + 1: raise ValueError('Length mismatch')
                    fd = os.open(partial, os.O_WRONLY)
                    try:
                        view = memoryview(payload)
                        position = offset
                        while view:
                            n = os.pwrite(fd, view, position)
                            position += n
                            view = view[n:]
                    finally: os.close(fd)
                    return offset
                except Exception as exc:
                    print('RETRY',relative,offset,attempt,type(exc).__name__,flush=True)
                    if attempt == 4: raise
                    time.sleep(min(2 ** attempt, 8))

        todo = [offset for offset in range(0, size, BLOCK) if offset not in done]
        print('DOWNLOADING', relative, 'chunks', len(todo), flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
            for future in concurrent.futures.as_completed([executor.submit(fetch, x) for x in todo]):
                done.add(future.result())
                temp = completed.with_suffix('.tmp')
                temp.write_text(json.dumps(sorted(done)))
                temp.replace(completed)
        if partial.stat().st_size != size or digest(partial, git) != expected:
            completed.unlink(missing_ok=True)
            raise ValueError('Pinned hash mismatch: ' + relative)
        partial.replace(path)
        completed.unlink(missing_ok=True)
        print('VERIFIED', relative, size, flush=True)
    # The public modular manifest also declares the separate Ref2VA transformer.
    # This service installs the T2VA/FL2VA transformer only. Keep the verified
    # original manifests for provenance and remove only that unused declaration.
    for filename in ('model_index.json','modular_model_index.json'):
        path = root/filename
        original = path.read_text()
        (root/(filename+'.upstream')).write_text(original)
        index = json.loads(original)
        index.pop('transformer_ref',None)
        path.write_text(json.dumps(index,indent=2))
    marker = root / 'WEIGHTS_VERIFIED.json'
    temporary = marker.with_suffix('.tmp')
    temporary.write_text(json.dumps({
        'repo': REPO, 'revision': REVISION, 'files': len(manifest['files']),
        'bytes': sum(f['size'] for f in manifest['files']),
        'elapsed_seconds': time.monotonic() - started,
        'manifest_transformation':'omit unused transformer_ref declaration; originals retained',
        'runtime_validated': False}, indent=2))
    temporary.replace(marker)
    print('ALL_WEIGHTS_VERIFIED', flush=True)


if __name__ == '__main__': main()
