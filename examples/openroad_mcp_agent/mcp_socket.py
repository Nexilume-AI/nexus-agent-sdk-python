"""Run the official OpenROAD MCP stdio server behind a local Unix socket."""

import os
import json
import signal
import socket
import subprocess
import threading

from eda_workflows import EdaWorkflows, TOOLS


SOCKET_PATH = "/ipc/mcp.sock"
stop = threading.Event()
workflows = None
signal.signal(signal.SIGTERM, lambda *_: stop.set())
signal.signal(signal.SIGINT, lambda *_: stop.set())


def serve(connection):
    output_lock = threading.Lock()
    catalogs = set()
    def send(message):
        with output_lock:
            connection.sendall(json.dumps(message, separators=(",", ":")).encode() + b"\n")
    process = subprocess.Popen(
        ["node", "/app/dist/main.js"],
        cwd="/work",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    def from_mcp():
        try:
            while line := process.stdout.readline():
                message = json.loads(line)
                if message.get('id') in catalogs:
                    catalogs.discard(message['id'])
                    if 'result' in message:
                        message['result'].setdefault('tools', []).extend(TOOLS)
                send(message)
        except OSError:
            pass
        finally:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    reader = threading.Thread(target=from_mcp, daemon=True)
    reader.start()
    try:
        with connection.makefile("rb") as incoming:
            while line := incoming.readline():
                message = json.loads(line)
                if message.get('method') == 'tools/list' and not (message.get('params') or {}).get('cursor'):
                    catalogs.add(message.get('id'))
                params = message.get('params') or {}
                if message.get('method') == 'tools/call' and params.get('name') in {t['name'] for t in TOOLS}:
                    try:
                        result = workflows.call(params['name'], params.get('arguments') or {})
                        response = {'content': [{'type': 'text', 'text': json.dumps(result)}], 'structuredContent': result}
                    except Exception as exc:
                        response = {'content': [{'type': 'text', 'text': str(exc)[:500]}], 'isError': True}
                    send({'jsonrpc': '2.0', 'id': message.get('id'), 'result': response})
                    continue
                process.stdin.write(line)
                process.stdin.flush()
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        connection.close()
        reader.join(timeout=5)


def main():
    global workflows
    workflows = EdaWorkflows()
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        listener.listen(1)
        listener.settimeout(1)
        print("OpenROAD MCP socket ready", flush=True)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            serve(connection)
    os.unlink(SOCKET_PATH)


if __name__ == "__main__":
    main()
