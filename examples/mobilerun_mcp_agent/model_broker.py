"""Per-Run OpenAI protocol bridge; provider secrets stay outside the LLM object."""
import http.server
import hashlib
import json
import secrets
import tempfile
import threading
import time
from urllib.request import Request
from nexus_agent.credentials import ProviderCredentials
from model_transport import open_model_request, validate_base_url
from driver import check_control

MAX_MODEL_RESPONSE_BYTES = 64 * 1024 * 1024
MODEL_RESPONSE_MEMORY_BYTES = 1024 * 1024


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_POST(self):
        try:
            broker = self.server.broker
            if self.path != '/v1/chat/completions' or not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+broker.nonce):
                self.send_error(403); return
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16*1024*1024:
                raise ValueError('MODEL_CONTEXT_LIMIT')
            result = broker.request(json.loads(self.rfile.read(length)))
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(result))); self.end_headers(); self.wfile.write(result)
        except Exception as exc:
            # Invalid local input/output and safety limits cannot be fixed by
            # paying the upstream to generate the same request again.
            permanent=isinstance(exc, ValueError)
            code=str(exc) if permanent and str(exc) in ('MODEL_RESPONSE_LIMIT','MODEL_CONTEXT_LIMIT','MODEL_BUDGET_EXHAUSTED','INVALID_MODEL_MESSAGES','MODEL_AUTHORIZATION_CHANGED','RUN_CANCELLED') else 'MODEL_REQUEST_REJECTED'
            body=json.dumps({'error':{'message':code,'type':'nexus_model_request_failed','code':code}}).encode()
            self.send_response(422 if permanent else 502); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)


REASONING_EFFORTS = ('default', 'low', 'medium', 'high')

def validate_effort(value):
    if value not in REASONING_EFFORTS: raise ValueError('INVALID_REASONING_EFFORT')
    return value


class ModelBroker:
    def __init__(self, ctx, authorization, maximum, reasoning_effort="default", effort_getter=None):
        self.ctx, self.authorization, self.maximum = ctx, authorization, maximum
        self.reasoning_effort = validate_effort(reasoning_effort)
        self.effort_getter = effort_getter
        self.model_seconds = 0.0
        self.failed_requests = 0
        self.efforts_used = []
        self.request_diagnostics = []
        self.previous_payload_digest = None
        self.calls = 0; self.lock = threading.Lock(); self.stopped = threading.Event()
        self.nonce = secrets.token_urlsafe(32)
        self.grant = ProviderCredentials(ctx).resolve_for_runtime(authorization)
        if self.grant.get('provider') not in ('nexus', 'openai', 'custom_openai'):
            self.grant.clear(); raise ValueError('OPENAI_COMPATIBLE_MODEL_REQUIRED')
        validate_base_url(self.grant['base_url'])
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True; self.server.broker = self
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self): return f'http://127.0.0.1:{self.server.server_port}/v1'

    def request(self, value):
        check_control(self.ctx)
        with self.lock:
            if self.stopped.is_set() or self.calls >= self.maximum:
                raise ValueError('MODEL_BUDGET_EXHAUSTED')
            self.calls += 1
        current = ProviderCredentials(self.ctx).resolve_for_runtime(self.authorization)
        try:
            if any(current.get(k) != self.grant.get(k) for k in ('model', 'provider', 'base_url')):
                raise ValueError('MODEL_AUTHORIZATION_CHANGED')
            if not isinstance(value.get('messages'), list) or len(value['messages']) > 100:
                raise ValueError('INVALID_MODEL_MESSAGES')
            payload = {k:value[k] for k in ('messages','tools','tool_choice') if k in value}
            payload.update(model=current['model'],stream=False,max_completion_tokens=2048)
            effort = validate_effort(self.effort_getter() if self.effort_getter else self.reasoning_effort)
            if effort != 'default': payload['reasoning_effort'] = effort
            self.efforts_used.append(effort)
            request = Request(current['base_url'].rstrip('/')+'/chat/completions', data=json.dumps(payload).encode(),
                headers={'Content-Type':'application/json','Authorization':'Bearer '+current['secret'],'User-Agent':'Nexus-Mobilerun/1'})
            started = time.monotonic()
            digest=hashlib.sha256(request.data).digest()
            diagnostic={'attempt':self.calls,'effort':effort,'same_input_as_previous':digest==self.previous_payload_digest}
            self.previous_payload_digest=digest
            try:
                with open_model_request(request, timeout=90,timing=diagnostic) as response:
                    # Encrypted reasoning and provider metadata can exceed the
                    # visible answer by megabytes. Preserve the entire response.
                    with tempfile.SpooledTemporaryFile(max_size=MODEL_RESPONSE_MEMORY_BYTES,mode='w+b') as output:
                        size=0
                        while part := response.read1(65536):
                            diagnostic.setdefault('first_body_seconds',round(time.monotonic()-started,4))
                            size += len(part);diagnostic['response_bytes']=size
                            if size > MAX_MODEL_RESPONSE_BYTES: raise ValueError('MODEL_RESPONSE_LIMIT')
                            output.write(part)
                        diagnostic['spooled_to_disk']=size>MODEL_RESPONSE_MEMORY_BYTES
                        output.seek(0);decoded=json.load(output)
                        output.seek(0);result=output.read()
                choice=(decoded.get('choices') or [{}])[0];message=choice.get('message') or {}
                text=message.get('content') or ''
                diagnostic.update(finish_reason=choice.get('finish_reason'),content_chars=len(text),
                    xml_tool_block=isinstance(text,str) and '<function_calls>' in text,
                    native_tool_calls=len(message.get('tool_calls') or []))
                return result
            except Exception as exc:
                diagnostic['error_type']=type(exc).__name__
                with self.lock:self.failed_requests += 1
                raise
            finally:
                # Include failed upstream attempts: otherwise retries look like phone/UI latency.
                with self.lock:self.model_seconds += time.monotonic() - started
                diagnostic['total_seconds']=round(time.monotonic()-started,4)
                self.request_diagnostics.append(diagnostic)
        finally:
            current.clear()

    def close(self):
        self.stopped.set(); self.server.shutdown(); self.server.server_close(); self.grant.clear()
