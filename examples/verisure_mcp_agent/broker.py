"""Bounded model broker; provider credentials never enter the compiler container."""
import http.server
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import threading


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length', '0'))
            if self.path != '/v1/chat/completions' or not 0 < n <= 524288:
                raise ValueError('INVALID_MODEL_REQUEST')
            value = self.server.broker.call(json.loads(self.rfile.read(n)))
            code = 200
        except Exception:
            value, code = {'error': 'MODEL_REQUEST_FAILED'}, 400
        raw = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Content-Type','application/json')
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Broker:
    def __init__(self, directory, grant, check, emit, budget):
        self.grant, self.check, self.emit, self.budget = grant, check, emit, budget
        self.lock, self.cancel = threading.Lock(), threading.Event()
        self.count, self.usage = 0, []
        self.process = None
        ipc = directory/'ipc'
        ipc.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(ipc, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.server = Server(f'/proc/self/fd/{fd}/run.sock', Handler)
        finally:
            os.close(fd)
        (ipc/'run.sock').chmod(0o600)
        self.server.broker = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def call(self, body):
        with self.lock:
            messages = body.get('messages')
            if not isinstance(messages,list) or not 1<=len(messages)<=96 or any(not isinstance(m,dict) or m.get('role') not in ('system','developer','user','assistant','tool') for m in messages):
                raise ValueError('INVALID_MESSAGES')
            if self.cancel.is_set() or self.count >= self.budget:
                raise ValueError('MODEL_BUDGET_OR_CANCELLED')
            self.check()
            self.count += 1
            self.emit('Model request '+str(self.count)+'/'+str(self.budget))
            payload = json.dumps({'grant': self.grant, 'request': body}).encode()
            p = subprocess.Popen([sys.executable, str(Path(__file__).with_name('model_call.py'))],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.process = p
            try:
                first = True
                while True:
                    if self.cancel.is_set():
                        raise ValueError('MODEL_CANCELLED')
                    try:
                        stdout, _ = p.communicate(payload if first else None, timeout=.5)
                        break
                    except subprocess.TimeoutExpired:
                        first = False
                if p.returncode or len(stdout) > 1048576:
                    raise ValueError('MODEL_REQUEST_FAILED')
                data = json.loads(stdout)
                self.check()
                self.usage.append({k:v for k,v in data.get('usage',{}).items() if type(v)==int})
                self.emit('Model request '+str(self.count)+' completed')
                return data
            finally:
                if p.poll() is None:
                    p.terminate()
                    p.communicate(timeout=10)
                self.process = None

    def close(self):
        self.cancel.set()
        self.server.shutdown()
        self.server.server_close()
        with self.lock:
            pass  # in-flight worker observed cancellation and exited
