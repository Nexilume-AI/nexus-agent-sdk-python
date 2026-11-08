"""Outbound-only file transport. Runtime credentials never leave the Agent host."""
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit
import requests

CHUNK = 4 * 1024 * 1024
ORIGIN = 'https://cloud.nexilume.com'


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        while data := f.read(CHUNK): h.update(data)
    return h.hexdigest()


def save(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as f:
        os.chmod(temp, 0o600); json.dump(value, f)
    temp.replace(path)


class Bridges:
    def __init__(self, workspace):
        self.w = workspace
        self.folder = workspace['folder'] / 'private_transfers'
        self.workers = {}
        self.lock = threading.RLock()
        self.file_locks = {}
        for p in self.folder.glob('*.json'):
            row = json.loads(p.read_text())
            if row['expires'] > time.time() and not row.get('revoked'): self.start(row)

    def start(self, row):
        self.workers[row['bridge_id']] = row
        threading.Thread(target=self.loop, args=(row,), daemon=True).start()

    def create(self, ctx, path, mode, digest, size, label=None):
        path = Path(path).resolve()
        if not path.is_relative_to(self.w['folder'].resolve()) or self.w['closed']: raise ValueError('FILE_NOT_AVAILABLE')
        if ctx is None: raise ValueError('ACTIVE_MCP_CONTEXT_REQUIRED')
        if sum(r['expires'] > time.time() and not r.get('revoked') for r in self.workers.values()) >= 24: raise ValueError('FILE_LINK_LIMIT')
        # Validate callback origin before using the short-lived interaction token.
        if not str(ctx.interaction_url).startswith(ORIGIN + '/api/v1/internal/agent-runs/'): raise ValueError('INVALID_FILE_ORIGIN')
        offset = path.stat().st_size if mode == 'upload' and path.exists() else 0
        # Upstream video filenames may contain prompt punctuation. Keep the disk
        # path unchanged and issue a safe download filename for HTTP headers.
        filename = re.sub(r'[^\w .()\-]', '_', label or path.name)[:200]
        reply = ctx._interaction_request('file-bridges/', method='POST', payload={
            'name': filename, 'mode': mode, 'size_bytes': size, 'sha256': digest, 'offset': offset})
        runtime = reply.pop('_runtime')
        url = runtime['url']
        if not re.fullmatch(re.escape(ORIGIN) + r'/api/v1/agent-file-bridges/[0-9a-f-]{36}/runtime/', url): raise ValueError('INVALID_FILE_ORIGIN')
        row = {**runtime, 'bridge_id': reply['bridge_id'], 'mode': mode, 'path': str(path.relative_to(self.w['folder'])),
            'size': size, 'sha256': digest, 'expires': time.time()+reply['session_expires_in']-5}
        if mode == 'download': row['snapshot'] = list(self.snapshot(path))
        save(self.folder / (row['bridge_id'] + '.json'), row)
        self.start(row)
        return reply

    @staticmethod
    def snapshot(path):
        st = path.stat(); return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

    def operation(self, row, command, session):
        if self.w['closed'] or row.get('revoked') or row['expires'] <= time.time(): raise ValueError('Closed')
        path = (self.w['folder'] / row['path']).resolve()
        if not path.is_relative_to(self.w['folder'].resolve()): raise ValueError('Invalid file')
        offset, length = command['offset'], command['length']
        if type(offset) is not int or type(length) is not int or not 0 <= offset < row['size'] or not 0 < length <= CHUNK or offset + length > row['size']: raise ValueError('Invalid range')
        cid = command['id']
        if not re.fullmatch('[0-9a-f-]{36}', cid): raise ValueError('Invalid command')
        with self.lock: lock = self.file_locks.setdefault(str(path), threading.Lock())
        with lock:
            if self.w['closed'] or row.get('revoked'): raise ValueError('Closed')
            if row['mode'] == 'download' and command['operation'] == 'read':
                if list(self.snapshot(path)) != row['snapshot']: raise ValueError('File changed')
                with path.open('rb') as f: f.seek(offset); data = f.read(length)
                if len(data) != length: raise ValueError('Short read')
                return data
            if row['mode'] != 'upload' or command['operation'] != 'write': raise ValueError('Operation denied')
            r = session.get(row['url'] + cid + '/', timeout=(5, 25), allow_redirects=False)
            if r.status_code != 200: raise ValueError('Chunk unavailable')
            data = r.content
            if len(data) != length or hashlib.sha256(data).hexdigest() != command['sha256']: raise ValueError('Chunk corrupt')
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with path.open('r+b' if path.exists() else 'w+b') as f:
                os.chmod(path, 0o600); f.seek(0, 2); current = f.tell()
                if offset < current:
                    f.seek(offset)
                    if f.read(length) != data: raise ValueError('Conflicting upload retry')
                elif offset == current:
                    f.write(data); f.flush(); os.fsync(f.fileno())
                else: raise ValueError('Non-contiguous upload')
            return json.dumps({'offset': offset + length}).encode()

    def loop(self, row):
        session = requests.Session(); session.trust_env = False
        session.headers['X-Nexus-File-Worker'] = row['token']
        while not row.get('revoked') and not self.w['closed'] and time.time() < row['expires']:
            try:
                r = session.get(row['url'], timeout=(5, 25), allow_redirects=False)
                if r.status_code in (403, 404): break
                if r.status_code != 200: time.sleep(3); continue
                envelope = r.json()
                command = envelope.get('data', envelope).get('command')
                if not command: continue
                try: body = b'O' + self.operation(row, command, session)
                except Exception: body = b'E'
                session.post(row['url'] + command['id'] + '/', data=body, headers={'Content-Type':'application/octet-stream'}, timeout=(5, 25), allow_redirects=False)
            except requests.RequestException: time.sleep(3)
        session.close()

    def revoke(self, bid):
        row = self.workers.get(bid)
        if not row: raise ValueError('FILE_LINK_NOT_FOUND')
        # Serialize revocation with any pending disk write before sealing imports.
        path = str((self.w['folder'] / row['path']).resolve())
        with self.lock: lock = self.file_locks.setdefault(path, threading.Lock())
        with lock:
            row['revoked'] = True; save(self.folder / (bid + '.json'), row)
        try:
            with requests.Session() as s:
                s.trust_env = False
                s.delete(row['url'], headers={'X-Nexus-File-Worker':row['token']}, timeout=(5, 8), allow_redirects=False)
        except requests.RequestException: pass

    def revoke_path(self, path):
        for bid, row in list(self.workers.items()):
            if (self.w['folder'] / row['path']).resolve() == Path(path).resolve() and not row.get('revoked'): self.revoke(bid)

    def close(self):
        for bid, row in list(self.workers.items()):
            if not row.get('revoked') and row['expires'] > time.time(): self.revoke(bid)
