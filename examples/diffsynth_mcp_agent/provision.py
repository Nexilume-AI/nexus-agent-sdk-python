"""Operator-only, resumable official Wan download; verify pinned ModelScope SHA256.

Usage: python provision.py MANIFEST DESTINATION
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
REPO = 'Wan-AI/Wan2.1-T2V-1.3B'
REVISION = 'master-sha256-pinned'
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
        allowed={'diffusion_pytorch_model.safetensors','models_t5_umt5-xxl-enc-bf16.pth','Wan2.1_VAE.pth','google/umt5-xxl/special_tokens_map.json','google/umt5-xxl/spiece.model','google/umt5-xxl/tokenizer.json','google/umt5-xxl/tokenizer_config.json'}
        if relative not in allowed: raise ValueError('Unapproved model file')
        path=(root/relative).resolve()
        if not path.is_relative_to(root): raise ValueError('Unsafe model path')
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
                    url = ('https://modelscope.cn/models/Wan-AI/Wan2.1-T2V-1.3B/resolve/master/'
                           + relative + '?nexus_ds_chunk=' + str(offset) + '-' + str(time.time_ns()))
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
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
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
    marker = root / 'WEIGHTS_VERIFIED.json'
    temporary = marker.with_suffix('.tmp')
    temporary.write_text(json.dumps({
        'repo': REPO, 'revision': REVISION, 'files': len(manifest['files']),
        'bytes': sum(f['size'] for f in manifest['files']),
        'elapsed_seconds': time.monotonic() - started,
        'source':'ModelScope official files with pinned whole-file hashes',
        'runtime_validated': False}, indent=2))
    temporary.replace(marker)
    print('ALL_WEIGHTS_VERIFIED', flush=True)


if __name__ == '__main__': main()
