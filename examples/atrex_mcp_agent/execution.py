"""Private Unix-socket control client. Never sends provider secrets to MCP clients."""
import http.client
import json
import os
import socket


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=20):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Execution:
    def __init__(self, path=None):
        self.path = path or os.environ.get('ATREX_EXECUTION_SOCKET', '')

    def request(self, operation, payload=None):
        if not self.path:
            raise RuntimeError('EXECUTION_NOT_CONFIGURED')
        connection = UnixConnection(self.path)
        try:
            connection.request('POST', '/' + operation, json.dumps(payload or {}).encode(), {'Content-Type': 'application/json'})
            response = connection.getresponse()
            result = json.loads(response.read(2 * 1024 * 1024))
            if response.status != 200:
                raise RuntimeError(result.get('error', 'EXECUTION_FAILED'))
            return result
        finally:
            connection.close()

    def status(self):
        if not self.path:
            return {'execution_enabled': False, 'blockers': ['GPU_RUNNER_NOT_CONFIGURED']}
        try:
            return self.request('health')
        except (OSError, ValueError, RuntimeError):
            return {'execution_enabled': False, 'blockers': ['EXECUTION_BROKER_UNAVAILABLE']}

    def events(self,payload):
        connection=UnixConnection(self.path,timeout=60)
        try:
            connection.request('POST','/events',json.dumps(payload).encode(),{'Content-Type':'application/json'})
            response=connection.getresponse()
            if response.status!=200:raise RuntimeError('EVENT_STREAM_UNAVAILABLE')
            for line in response:
                if len(line)>131072:raise RuntimeError('EVENT_TOO_LARGE')
                yield json.loads(line)
        finally:connection.close()
