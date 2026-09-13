"""Dependency-free HTTP/SSE runtime for a callable Nexus Python Agent."""

from contextlib import contextmanager
from dataclasses import replace
import asyncio
import base64
import hashlib
import inspect
import json
import queue
import re
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)
from urllib.parse import urlsplit

from .client import AgentLease, NexusAgentClient
from .browser import (
    NexusBrowserActionFailed,
    NexusBrowserError,
    NexusBrowserSessionLost,
    NexusBrowserStaleObservation,
    NexusBrowserUnavailable,
    NexusBrowserComputerRequired,
    NexusBrowserPermissionRequired,
    NexusBrowserTunnelUnavailable,
)
from .direct_tasks import DirectTask, DirectTaskStore, TERMINAL_STATUSES
from .models import (
    AgentEnvelope,
    AgentResponse,
    CapabilityRegistration,
    SseEvent,
)
from .reporting import (
    _DELEGATE_FAILURE_CODES,
    NexusChatError,
    NexusCheckpointError,
    NexusComputerError,
    NexusMemoryError,
    NexusMobileActionFailed,
    NexusMobileBusy,
    NexusMobileError,
    NexusMobilePermissionRequired,
    NexusRunContext,
    NexusRunContextExchangeError,
)
from .resume import StreamResumeError, StreamResumeStore
from .server_auth import (
    NoServerAuth,
    ServerAuthenticationError,
    ServerAuthPolicy,
)

SyncHandler = Callable[[AgentEnvelope], Any]
StreamItem = Union[SseEvent, Mapping[str, Any], Sequence[Any], str, bytes]
StreamHandler = Callable[[AgentEnvelope], Iterable[StreamItem]]

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_ALLOWED_CONTENT_TYPES = {
    "application/json",
    "application/vnd.nexus.agent-envelope+json",
}


class AgentRequestError(Exception):
    """A bounded application error that may be returned to the caller."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        if status < 400 or status > 599:
            raise ValueError("AgentRequestError status must be 400..599")
        self.status = status
        self.code = code
        self.message = message


def _managed_service_error(exc: Exception) -> Tuple[str, str]:
    """Map safe SDK service failures without exposing Run credentials."""

    code = getattr(exc, "code", "")
    if isinstance(exc, (NexusBrowserError, NexusComputerError)) and isinstance(code, str) and code in _DELEGATE_FAILURE_CODES:
        return code, "The Run delegate operation is unavailable."
    if isinstance(exc, NexusBrowserComputerRequired):
        return "BROWSER_COMPUTER_REQUIRED", str(exc)
    if isinstance(exc, NexusBrowserPermissionRequired):
        return "BROWSER_PERMISSION_REQUIRED", str(exc)
    if isinstance(exc, NexusBrowserTunnelUnavailable):
        return "BROWSER_TUNNEL_UNAVAILABLE", str(exc)
    if isinstance(exc, NexusBrowserUnavailable):
        return "BROWSER_UNAVAILABLE", str(exc)
    if isinstance(exc, NexusBrowserStaleObservation):
        return "BROWSER_STALE_OBSERVATION", str(exc)
    if isinstance(exc, NexusBrowserSessionLost):
        return "BROWSER_SESSION_LOST", str(exc)
    if isinstance(exc, NexusBrowserActionFailed):
        return "BROWSER_ACTION_FAILED", str(exc)
    if isinstance(exc, NexusBrowserError):
        return "BROWSER_UNAVAILABLE", str(exc)
    if isinstance(exc, NexusComputerError):
        return "WORKSPACE_UNAVAILABLE", str(exc)
    if isinstance(exc, NexusMemoryError):
        return "MEMORY_UNAVAILABLE", str(exc)
    if isinstance(exc, NexusChatError):
        return "INTERACTION_UNAVAILABLE", str(exc)
    if isinstance(exc, NexusCheckpointError):
        return "CHECKPOINT_UNAVAILABLE", str(exc)
    if isinstance(exc, NexusMobileBusy):
        return "MOBILE_BUSY", str(exc)
    if isinstance(exc, NexusMobilePermissionRequired):
        return "MOBILE_PERMISSION_REQUIRED", str(exc)
    if isinstance(exc, NexusMobileActionFailed):
        code = getattr(exc, "code", "")
        messages = {
            "MOBILE_ACTION_FAILED": "Mobile action failed on the attached device",
            "MOBILE_ACTION_REJECTED": "Mobile action was rejected by the caller",
            "MOBILE_ACTION_CANCELLED": "Mobile action was canceled before completion",
        }
        if code in messages:
            return code, messages[code]
        return "MOBILE_ACTION_FAILED", "Mobile action failed on the attached device"
    if isinstance(exc, NexusMobileError):
        return "MOBILE_UNAVAILABLE", str(exc)
    return "", ""


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _required_string(root: Mapping[str, Any], name: str, maximum: int) -> str:
    value = root.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AgentRequestError(400, "INVALID_ENVELOPE", f"{name} is invalid")
    return value


def _parse_envelope(root: Any, route_id: Optional[str]) -> AgentEnvelope:
    if not isinstance(root, dict):
        raise AgentRequestError(400, "INVALID_ENVELOPE", "Envelope must be an object")
    version = _required_string(root, "version", 16)
    if version != "1.0":
        raise AgentRequestError(400, "INVALID_ENVELOPE", "version must be '1.0'")
    intent = _required_string(root, "intent", 127)
    task_id = _required_string(root, "task_id", 127)
    source_agent = _required_string(root, "source_agent", 255)
    tenant = _required_string(root, "tenant", 95)
    intent_version = root.get("intent_version")
    hop_limit = root.get("hop_limit")
    constraints = root.get("constraints", {})
    target_agent = root.get("target_agent")
    resume_from_event_id = root.get("resume_from_event_id", 0)
    if not isinstance(intent_version, int) or isinstance(intent_version, bool) or intent_version < 1:
        raise AgentRequestError(400, "INVALID_ENVELOPE", "intent_version is invalid")
    if not isinstance(hop_limit, int) or isinstance(hop_limit, bool) or not 1 <= hop_limit <= 255:
        raise AgentRequestError(400, "INVALID_ENVELOPE", "hop_limit is invalid")
    if "payload" not in root or root["payload"] is None:
        raise AgentRequestError(400, "INVALID_ENVELOPE", "payload is required")
    if not isinstance(constraints, dict):
        raise AgentRequestError(400, "INVALID_ENVELOPE", "constraints must be an object")
    if target_agent is not None and (
        not isinstance(target_agent, str) or not target_agent or len(target_agent) > 255
    ):
        raise AgentRequestError(400, "INVALID_ENVELOPE", "target_agent is invalid")
    if (
        not isinstance(resume_from_event_id, int)
        or isinstance(resume_from_event_id, bool)
        or resume_from_event_id < 0
        or resume_from_event_id > 9223372036854775807
    ):
        raise AgentRequestError(
            400, "INVALID_RESUME_CURSOR", "resume_from_event_id is invalid"
        )
    return AgentEnvelope(
        version=version,
        intent=intent,
        intent_version=intent_version,
        task_id=task_id,
        source_agent=source_agent,
        tenant=tenant,
        hop_limit=hop_limit,
        payload=root["payload"],
        constraints=constraints,
        target_agent=target_agent,
        route_id=route_id,
        resume_from_event_id=resume_from_event_id,
        raw=root,
    )


def _safe_response_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    reserved = {"content-length", "content-type", "connection", "transfer-encoding"}
    for name, value in headers.items():
        text_name = str(name)
        text_value = str(value)
        if (not _HEADER_NAME.fullmatch(text_name) or
                text_name.lower() in reserved or
                "\r" in text_value or "\n" in text_value):
            raise AgentRequestError(500, "INVALID_RESPONSE", "handler returned an unsafe header")
        result[text_name] = text_value
    return result


def _sse_event(item: StreamItem) -> SseEvent:
    event: Optional[str] = None
    event_id: Optional[str] = None
    retry_ms: Optional[int] = None
    if isinstance(item, SseEvent):
        data = item.data
        event = item.event
        event_id = item.event_id
        retry_ms = item.retry_ms
    elif isinstance(item, bytes):
        data = item.decode("utf-8")
    elif isinstance(item, str):
        data = item
    else:
        data = json.dumps(item, separators=(",", ":"), ensure_ascii=False)
    for value in (event, event_id):
        if value is not None and ("\r" in value or "\n" in value):
            raise AgentRequestError(500, "INVALID_SSE_EVENT", "SSE metadata contains a newline")
    if retry_ms is not None and retry_ms < 0:
        raise AgentRequestError(500, "INVALID_SSE_EVENT", "SSE retry must be non-negative")
    return SseEvent(data=data, event=event, event_id=event_id, retry_ms=retry_ms)


def _sse_bytes(item: StreamItem) -> bytes:
    normalized = _sse_event(item)
    event = normalized.event
    event_id = normalized.event_id
    retry_ms = normalized.retry_ms
    lines: List[str] = []
    if event is not None:
        lines.append(f"event: {event}")
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if retry_ms is not None:
        lines.append(f"retry: {retry_ms}")
    data_lines = normalized.data.splitlines() or [""]
    lines.extend(f"data: {line}" for line in data_lines)
    return ("\n".join(lines) + "\n\n").encode("utf-8")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _IPv6Server(_Server):
    address_family = socket.AF_INET6

    def __init__(self, server_address, handler, *, dual_stack: bool) -> None:
        self._nexus_dual_stack = dual_stack
        super().__init__(server_address, handler)

    def server_bind(self) -> None:
        if hasattr(socket, "IPV6_V6ONLY"):
            self.socket.setsockopt(
                socket.IPPROTO_IPV6,
                socket.IPV6_V6ONLY,
                0 if self._nexus_dual_stack else 1,
            )
        super().server_bind()


class NexusAgentServer:
    """Serve Nexus Envelope invokes and dispatch them by exact intent."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        path: str = "/invoke",
        stream_path: Optional[str] = None,
        auth: Optional[ServerAuthPolicy] = None,
        cert_file: Optional[str] = None,
        key_file: Optional[str] = None,
        client_ca_file: Optional[str] = None,
        address_family: str = "auto",
        dual_stack: bool = False,
        max_request_bytes: int = 65536,
        max_response_bytes: int = 262144,
        max_stream_event_bytes: int = 65536,
        request_timeout: float = 15.0,
        resumable_streams: bool = True,
        resume_max_tasks: int = 128,
        resume_max_events: int = 256,
        resume_max_history_bytes: int = 262144,
        resume_retention_seconds: float = 300.0,
        direct_tasks: bool = True,
        direct_task_max_tasks: int = 128,
        direct_task_ttl_seconds: float = 3600.0,
        direct_task_heartbeat_seconds: float = 10.0,
        run_context_opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("path must be an absolute URL path")
        if stream_path is not None and (
            not stream_path.startswith("/")
            or "?" in stream_path
            or "#" in stream_path
            or stream_path == path
        ):
            raise ValueError("stream_path must be a distinct absolute URL path")
        if bool(cert_file) != bool(key_file):
            raise ValueError("cert_file and key_file must be supplied together")
        if client_ca_file and not cert_file:
            raise ValueError("client_ca_file requires TLS server credentials")
        if address_family not in ("auto", "ipv4", "ipv6"):
            raise ValueError("address_family must be 'auto', 'ipv4' or 'ipv6'")
        if not isinstance(dual_stack, bool):
            raise ValueError("dual_stack must be a boolean")
        selected_family = address_family
        if selected_family == "auto":
            selected_family = "ipv6" if ":" in host else "ipv4"
        if dual_stack and selected_family != "ipv6":
            raise ValueError("dual_stack is only valid for an IPv6 listener")
        for name, value in (
            ("max_request_bytes", max_request_bytes),
            ("max_response_bytes", max_response_bytes),
            ("max_stream_event_bytes", max_stream_event_bytes),
        ):
            if value < 256:
                raise ValueError(f"{name} must be at least 256")
        if max_stream_event_bytes > max_response_bytes:
            raise ValueError("max_stream_event_bytes must not exceed max_response_bytes")
        self.path = path
        self.stream_path = stream_path
        self.auth = auth or NoServerAuth()
        if (
            not callable(getattr(self.auth, "authenticate", None))
            or not isinstance(getattr(self.auth, "mode", None), str)
        ):
            raise TypeError("auth must implement ServerAuthPolicy")
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.max_stream_event_bytes = max_stream_event_bytes
        self.request_timeout = request_timeout
        self.resumable_streams = bool(resumable_streams)
        self.direct_tasks = bool(direct_tasks)
        if not 1 <= direct_task_heartbeat_seconds <= 60:
            raise ValueError("direct_task_heartbeat_seconds must be between 1 and 60")
        self.direct_task_heartbeat_seconds = float(direct_task_heartbeat_seconds)
        self.run_context_opener = run_context_opener
        self._direct_tasks = DirectTaskStore(
            max_tasks=direct_task_max_tasks,
            ttl_seconds=direct_task_ttl_seconds,
        ) if self.direct_tasks else None
        self._resume_store = StreamResumeStore(
            max_tasks=resume_max_tasks,
            max_events_per_task=resume_max_events,
            max_history_bytes_per_task=resume_max_history_bytes,
            retention_seconds=resume_retention_seconds,
        ) if self.resumable_streams else None
        self._handlers: Dict[str, SyncHandler] = {}
        self._stream_handlers: Dict[str, StreamHandler] = {}
        self._lock = threading.RLock()
        self._serving = threading.Event()
        self._closed = threading.Event()
        owner = self

        class RequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                if owner._is_direct_task_path(self.path):
                    owner._handle_direct_task_api(self)
                else:
                    owner._handle(self)

            def do_GET(self) -> None:  # noqa: N802
                if owner._is_direct_task_path(self.path):
                    owner._handle_direct_task_api(self)
                elif urlsplit(self.path).path == "/healthz":
                    health: Dict[str, Any] = {
                        "status": "ok",
                        "serving": owner.is_healthy(),
                        "handlers": len(owner._handlers),
                        "stream_handlers": len(owner._stream_handlers),
                        "resumable_streams": owner.resumable_streams,
                        "authentication": owner.auth.mode,
                    }
                    if owner._resume_store is not None:
                        health["resume"] = owner._resume_store.snapshot()
                    if owner._direct_tasks is not None:
                        health["direct_tasks"] = owner._direct_tasks.snapshot()
                    owner._send_json(self, 200, health)
                else:
                    owner._send_error(self, 404, "NOT_FOUND", "endpoint not found")

            def do_DELETE(self) -> None:  # noqa: N802
                if owner._is_direct_task_path(self.path):
                    owner._handle_direct_task_api(self)
                else:
                    owner._send_error(self, 404, "NOT_FOUND", "endpoint not found")

            def log_message(self, format: str, *args: Any) -> None:
                return

        if selected_family == "ipv6":
            self._httpd = _IPv6Server(
                (host, port), RequestHandler, dual_stack=dual_stack
            )
        else:
            self._httpd = _Server((host, port), RequestHandler)
        if cert_file and key_file:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert_file, key_file)
            if client_ca_file:
                context.load_verify_locations(cafile=client_ca_file)
                context.verify_mode = ssl.CERT_REQUIRED
            self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
        address = self._httpd.server_address
        self.host = str(address[0])
        self.port = int(address[1])
        self.tls_enabled = bool(cert_file)
        self.address_family = selected_family
        self.dual_stack = bool(dual_stack)

    def add_handler(
        self,
        intent: str,
        handler: SyncHandler,
        *,
        stream_handler: Optional[StreamHandler] = None,
    ) -> None:
        if not intent or len(intent) > 127:
            raise ValueError("intent must be 1..127 characters")
        with self._lock:
            self._handlers[intent] = handler
            if stream_handler is not None:
                self._stream_handlers[intent] = stream_handler

    def handler(self, intent: str) -> Callable[[SyncHandler], SyncHandler]:
        def decorate(function: SyncHandler) -> SyncHandler:
            self.add_handler(intent, function)
            return function
        return decorate

    def stream_handler(self, intent: str) -> Callable[[StreamHandler], StreamHandler]:
        def decorate(function: StreamHandler) -> StreamHandler:
            if not intent or len(intent) > 127:
                raise ValueError("intent must be 1..127 characters")
            with self._lock:
                self._stream_handlers[intent] = function
            return function
        return decorate

    def remove_handler(self, intent: str) -> None:
        with self._lock:
            self._handlers.pop(intent, None)
            self._stream_handlers.pop(intent, None)

    def has_handler(self, intent: str) -> bool:
        """Return whether an exact synchronous intent handler is installed."""

        with self._lock:
            return intent in self._handlers

    def _read_envelope(self, request: BaseHTTPRequestHandler) -> AgentEnvelope:
        transfer_encoding = request.headers.get("Transfer-Encoding")
        if transfer_encoding:
            raise AgentRequestError(400, "INVALID_FRAMING", "Transfer-Encoding is not accepted")
        content_type = request.headers.get_content_type()
        if content_type not in _ALLOWED_CONTENT_TYPES:
            raise AgentRequestError(415, "UNSUPPORTED_MEDIA_TYPE", "expected a Nexus JSON Envelope")
        length_text = request.headers.get("Content-Length")
        try:
            length = int(length_text) if length_text is not None else -1
        except ValueError as exc:
            raise AgentRequestError(400, "INVALID_FRAMING", "Content-Length is invalid") from exc
        if length < 1 or length > self.max_request_bytes:
            raise AgentRequestError(413, "REQUEST_TOO_LARGE", "Envelope size is outside server bounds")
        request.connection.settimeout(self.request_timeout)
        raw = request.rfile.read(length)
        if len(raw) != length:
            raise AgentRequestError(400, "INVALID_FRAMING", "request body is truncated")
        try:
            root = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey) as exc:
            raise AgentRequestError(400, "INVALID_JSON", "request body is not one valid JSON object") from exc
        return _parse_envelope(root, request.headers.get("X-Nexus-Route-Id"))

    @staticmethod
    def _is_direct_task_path(raw_path: str) -> bool:
        path = urlsplit(raw_path).path
        return path == "/agent/v1/tasks" or path.startswith("/agent/v1/tasks/")

    def _read_json_object(self, request: BaseHTTPRequestHandler) -> Dict[str, Any]:
        length_text = request.headers.get("Content-Length")
        try:
            length = int(length_text) if length_text is not None else 0
        except ValueError as exc:
            raise AgentRequestError(400, "INVALID_FRAMING", "Content-Length is invalid") from exc
        if length < 0 or length > self.max_request_bytes:
            raise AgentRequestError(413, "REQUEST_TOO_LARGE", "request exceeds server bounds")
        if length == 0:
            return {}
        raw = request.rfile.read(length)
        if len(raw) != length:
            raise AgentRequestError(400, "INVALID_FRAMING", "request body is truncated")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey) as exc:
            raise AgentRequestError(400, "INVALID_JSON", "request body must be one JSON object") from exc
        if not isinstance(value, dict):
            raise AgentRequestError(400, "INVALID_JSON", "request body must be one JSON object")
        return value

    @staticmethod
    def _direct_task_parts(raw_path: str) -> Tuple[str, Tuple[str, ...]]:
        path = urlsplit(raw_path).path.strip("/")
        parts = tuple(part for part in path.split("/") if part)
        if len(parts) < 4 or parts[:3] != ("agent", "v1", "tasks"):
            return "", ()
        return parts[3], parts[4:]

    def _direct_task(self, request: BaseHTTPRequestHandler, task_id: str) -> DirectTask:
        store = self._direct_tasks
        token = request.headers.get("X-Nexus-Task-Token") or ""
        task = store.get(task_id, token) if store is not None else None
        if task is None:
            raise AgentRequestError(404, "NOT_FOUND", "task not found")
        return task

    def _handle_direct_task_api(self, request: BaseHTTPRequestHandler) -> None:
        try:
            parsed = urlsplit(request.path)
            if parsed.fragment:
                raise AgentRequestError(404, "NOT_FOUND", "task not found")
            task_id, suffix = self._direct_task_parts(request.path)
            task = self._direct_task(request, task_id)
            if request.command == "GET" and not suffix:
                self._send_json(request, 200, task.public_status())
                return
            if request.command == "GET" and suffix == ("events",):
                try:
                    query = dict(
                        item.split("=", 1) if "=" in item else (item, "")
                        for item in parsed.query.split("&") if item
                    )
                    after = int(query.get("after") or 0)
                except (TypeError, ValueError):
                    raise AgentRequestError(400, "INVALID_CURSOR", "after must be an integer")
                if after < 0:
                    raise AgentRequestError(400, "INVALID_CURSOR", "after must be non-negative")
                self._send_json(
                    request,
                    200,
                    {
                        "task_id": task.task_id,
                        "status": task.status,
                        "events": task.events_after(after),
                    },
                )
                return
            if (
                request.command == "POST"
                and len(suffix) == 3
                and suffix[0] == "interactions"
                and suffix[2] == "reply"
            ):
                payload = self._read_json_object(request)
                if "value" not in payload and "text" not in payload:
                    raise AgentRequestError(400, "INVALID_REPLY", "reply value is required")
                value = payload.get("value", payload.get("text"))
                try:
                    interaction = task.reply(suffix[1], value)
                except KeyError:
                    raise AgentRequestError(404, "NOT_FOUND", "interaction not found")
                self._send_json(request, 200, interaction)
                return
            if request.command == "DELETE" and not suffix:
                task.cancel()
                self._send_json(request, 200, task.public_status())
                return
            if (
                request.command == "GET"
                and len(suffix) == 2
                and suffix[0] == "assets"
            ):
                asset = task.asset(suffix[1])
                if asset is None:
                    raise AgentRequestError(404, "NOT_FOUND", "asset not found")
                self._send_bytes(
                    request,
                    200,
                    asset.content,
                    content_type=asset.content_type,
                    file_name=asset.file_name,
                )
                return
            raise AgentRequestError(404, "NOT_FOUND", "task endpoint not found")
        except AgentRequestError as exc:
            self._send_error(request, exc.status, exc.code, exc.message)
        except Exception:
            self._send_error(request, 500, "TASK_CONTROL_FAILED", "task control failed")

    def _send_bytes(
        self,
        request: BaseHTTPRequestHandler,
        status: int,
        content: bytes,
        *,
        content_type: str,
        file_name: str,
    ) -> None:
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", str(file_name or "asset"))[:128]
        request.send_response(status)
        request.send_header("Content-Type", str(content_type or "application/octet-stream"))
        request.send_header("Content-Length", str(len(content)))
        request.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
        request.send_header("Cache-Control", "private, no-store")
        request.end_headers()
        if request.command != "HEAD":
            request.wfile.write(content)

    def _handle(self, request: BaseHTTPRequestHandler) -> None:
        parsed_path = urlsplit(request.path)
        request_path = parsed_path.path
        stream_endpoint = self.stream_path is not None and request_path == self.stream_path
        if (request_path != self.path and not stream_endpoint) or parsed_path.query:
            self._send_error(request, 404, "NOT_FOUND", "endpoint not found")
            return
        try:
            envelope = self._read_envelope(request)
            try:
                caller = self.auth.authenticate(
                    request.headers.get("Authorization"), envelope
                )
            except ServerAuthenticationError as exc:
                raise AgentRequestError(exc.status, exc.code, exc.message) from exc
            raw_envelope = dict(envelope.raw)
            exchange = raw_envelope.pop("nexus_run_context", None)
            if exchange is not None:
                if not isinstance(exchange, Mapping):
                    raise AgentRequestError(
                        400, "INVALID_RUN_CONTEXT", "Run context reference is invalid"
                    )
                try:
                    cloud_context = NexusRunContext.from_exchange(
                        str(exchange.get("exchange_url") or ""),
                        str(exchange.get("exchange_token") or ""),
                        cloud_opener=self.run_context_opener,
                    )
                except NexusRunContextExchangeError as exc:
                    raise AgentRequestError(
                        502,
                        exc.code,
                        str(exc),
                    ) from exc
                envelope = replace(envelope, raw=raw_envelope)
            else:
                cloud_context = NexusRunContext.from_env(headers=request.headers)
            envelope = replace(
                envelope,
                authenticated_subject=caller.subject,
                authenticated_scopes=caller.scopes,
                auth_claims=caller.claims,
                run_context=cloud_context,
            )
            try:
                direct_control = (
                    envelope.payload.get("nexus_direct_task_control")
                    if isinstance(envelope.payload, Mapping)
                    else None
                )
                if direct_control is not None:
                    if cloud_context.enabled:
                        raise AgentRequestError(404, "NOT_FOUND", "task not found")
                    self._handle_routed_direct_task_control(request, direct_control)
                    return
                wants_stream = stream_endpoint or (
                    "text/event-stream" in
                    (request.headers.get("Accept") or "").lower()
                )
                with self._lock:
                    handler = self._handlers.get(envelope.intent)
                    stream_handler = self._stream_handlers.get(envelope.intent)
                wants_direct_async = (
                    not cloud_context.enabled
                    and self._direct_tasks is not None
                    and "respond-async" in (request.headers.get("Prefer") or "").lower()
                )
                wants_direct_stream = (
                    not cloud_context.enabled
                    and self._direct_tasks is not None
                    and wants_stream
                    and (request.headers.get("X-Nexus-Interactive") or "").lower()
                    in {"1", "true", "yes"}
                )
                if wants_direct_async or wants_direct_stream:
                    if handler is None and stream_handler is None:
                        raise AgentRequestError(404, "INTENT_NOT_SERVED", "intent is not served here")
                    task = self._direct_tasks.create()
                    self._start_direct_task(
                        task,
                        envelope,
                        handler=handler,
                        stream_handler=stream_handler if handler is None else None,
                        interaction_mode="stream" if wants_direct_stream else "task",
                    )
                    if wants_direct_stream:
                        self._send_direct_task_stream(request, task)
                    else:
                        self._send_json(
                            request,
                            202,
                            {
                                "task_id": task.task_id,
                                "status": task.status,
                            },
                            {
                                "X-Nexus-Task-Id": task.task_id,
                                "X-Nexus-Task-Token": task.token,
                                "Location": f"/agent/v1/tasks/{task.task_id}",
                            },
                        )
                    return
                if wants_stream:
                    if stream_handler is None:
                        if handler is not None and cloud_context.enabled:
                            self._send_cloud_handler_stream(request, envelope, handler)
                            return
                        raise AgentRequestError(406, "STREAM_NOT_SUPPORTED", "intent has no streaming handler")
                    if self._resume_store is not None:
                        self._send_stream(
                            request,
                            self._resumable_stream(envelope, stream_handler),
                        )
                    else:
                        if envelope.resume_from_event_id:
                            raise AgentRequestError(
                                409,
                                "STREAM_RESUME_DISABLED",
                                "this Agent does not retain stream history",
                            )
                        self._send_stream(request, stream_handler(envelope))
                else:
                    if handler is None:
                        raise AgentRequestError(404, "INTENT_NOT_SERVED", "intent is not served here")
                    result = self._resolve_value(handler(envelope))
                    # A Cloud handler may enqueue its final Plan, Chat, Files,
                    # or Logs events immediately before returning.  Deliver
                    # those events while the Cloud still considers the Run
                    # open; closing the MCP response first races Run
                    # finalization and causes the trailing events to be
                    # rejected as writes to a completed Run.
                    cloud_context.flush()
                    if isinstance(result, AgentResponse):
                        self._send_json(request, result.status, result.body, result.headers)
                    else:
                        self._send_json(request, 200, result)
            finally:
                cloud_context.close()
        except AgentRequestError as exc:
            self._send_error(request, exc.status, exc.code, exc.message)
        except StreamResumeError as exc:
            self._send_error(request, exc.status, exc.code, exc.message)
        except Exception as exc:
            code, message = _managed_service_error(exc)
            if code:
                self._send_error(request, 502, code, message)
            else:
                self._send_error(request, 500, "HANDLER_FAILED", "Agent handler failed")

    def _handle_routed_direct_task_control(
        self,
        request: BaseHTTPRequestHandler,
        payload: Any,
    ) -> None:
        store = self._direct_tasks
        payload = payload if isinstance(payload, Mapping) else {}
        task_id = str(payload.get("direct_task_id") or "")
        token = str(payload.get("direct_task_token") or "")
        task = store.get(task_id, token) if store is not None else None
        if task is None:
            raise AgentRequestError(404, "NOT_FOUND", "task not found")
        action = str(payload.get("action") or "")
        if action == "status":
            self._send_json(request, 200, task.public_status())
            return
        if action == "events":
            try:
                after = max(int(payload.get("after") or 0), 0)
            except (TypeError, ValueError):
                raise AgentRequestError(404, "NOT_FOUND", "task not found")
            self._send_json(
                request,
                200,
                {
                    "task_id": task.task_id,
                    "status": task.public_status()["status"],
                    "events": task.events_after(after),
                },
            )
            return
        if action == "reply":
            try:
                result = task.reply(str(payload.get("key") or ""), payload.get("value"))
            except KeyError:
                raise AgentRequestError(404, "NOT_FOUND", "task not found")
            self._send_json(request, 200, result)
            return
        if action == "cancel":
            task.cancel()
            self._send_json(request, 200, task.public_status())
            return
        if action == "asset":
            asset = task.asset(str(payload.get("asset_id") or ""))
            if asset is None:
                raise AgentRequestError(404, "NOT_FOUND", "task not found")
            try:
                offset = max(int(payload.get("offset") or 0), 0)
                limit = min(max(int(payload.get("limit") or 192 * 1024), 1), 192 * 1024)
            except (TypeError, ValueError):
                raise AgentRequestError(404, "NOT_FOUND", "task not found")
            if offset > len(asset.content):
                raise AgentRequestError(404, "NOT_FOUND", "task not found")
            chunk = asset.content[offset:offset + limit]
            next_offset = offset + len(chunk)
            self._send_json(
                request,
                200,
                {
                    "asset_id": asset.asset_id,
                    "content_base64": base64.b64encode(chunk).decode("ascii"),
                    "next_offset": next_offset,
                    "eof": next_offset >= len(asset.content),
                    "content_type": asset.content_type,
                    "file_name": asset.file_name,
                },
            )
            return
        raise AgentRequestError(404, "NOT_FOUND", "task not found")

    @staticmethod
    def _resolve_value(value: Any) -> Any:
        if inspect.isawaitable(value):
            return asyncio.run(value)
        return value

    @classmethod
    def _iter_values(cls, value: Any) -> Iterator[Any]:
        if inspect.isawaitable(value):
            yield from cls._iter_values(cls._resolve_value(value))
            return
        if hasattr(value, "__aiter__"):
            iterator = value.__aiter__()
            loop = asyncio.new_event_loop()
            try:
                while True:
                    try:
                        item = loop.run_until_complete(iterator.__anext__())
                    except StopAsyncIteration:
                        break
                    yield item
            finally:
                close = getattr(iterator, "aclose", None)
                if callable(close):
                    try:
                        loop.run_until_complete(close())
                    except Exception:
                        pass
                loop.close()
            return
        yield from value

    def _start_direct_task(
        self,
        task: DirectTask,
        envelope: AgentEnvelope,
        *,
        handler: Optional[SyncHandler],
        stream_handler: Optional[StreamHandler],
        interaction_mode: str,
    ) -> threading.Thread:
        def emit_display(event: Mapping[str, Any]) -> bool:
            task.emit("display", {"event": dict(event)})
            return True

        def interaction_request(
            path: str,
            method: str,
            payload: Optional[Mapping[str, Any]],
        ) -> Dict[str, Any]:
            return task.interaction_request(path, method=method, payload=payload)

        def upload_asset(
            content: bytes,
            content_type: str,
            file_name: str,
            width: Optional[int],
            height: Optional[int],
        ) -> Dict[str, Any]:
            return task.add_asset(
                content=content,
                content_type=content_type,
                file_name=file_name,
                sha256=hashlib.sha256(content).hexdigest(),
                width=width,
                height=height,
            )

        context = NexusRunContext(
            run_id=task.task_id,
            interaction_mode=interaction_mode,
            _event_sink=emit_display,
            _interaction_request=interaction_request,
            _asset_uploader=upload_asset,
        )
        local_envelope = replace(envelope, run_context=context)

        def execute() -> None:
            task.start()
            try:
                if handler is not None:
                    result = self._resolve_value(handler(local_envelope))
                    if isinstance(result, AgentResponse):
                        if result.status >= 400:
                            task.fail("HANDLER_REJECTED", "Agent handler rejected the request")
                        else:
                            task.complete(result.body)
                    else:
                        task.complete(result)
                    return
                if stream_handler is None:
                    task.fail("INTENT_NOT_SERVED", "intent is not served here")
                    return
                final_result: Any = {"ok": True}
                for raw_item in self._iter_values(stream_handler(local_envelope)):
                    if task.cancelled:
                        return
                    item = _sse_event(raw_item)
                    try:
                        data: Any = json.loads(item.data)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        data = item.data
                    event_name = str(item.event or "progress")
                    task.emit(event_name, {"value": data})
                    if event_name == "result":
                        final_result = data
                task.complete(final_result)
            except AgentRequestError as exc:
                task.fail(exc.code, exc.message)
            except Exception:
                task.fail("HANDLER_FAILED", "Agent handler failed")
            finally:
                context.close()

        thread = threading.Thread(
            target=execute,
            name="nexus-direct-task-" + task.task_id[:12],
            daemon=True,
        )
        thread.start()
        return thread

    def _send_direct_task_stream(
        self,
        request: BaseHTTPRequestHandler,
        task: DirectTask,
    ) -> None:
        request.send_response(200)
        request.send_header("Content-Type", "text/event-stream")
        request.send_header("Cache-Control", "no-store")
        request.send_header("X-Accel-Buffering", "no")
        request.send_header("Connection", "close")
        request.send_header("X-Nexus-Task-Id", task.task_id)
        request.send_header("X-Nexus-Task-Token", task.token)
        request.end_headers()
        request.close_connection = True
        cursor = 0
        total = 0
        try:
            while True:
                events = task.wait_events(
                    cursor,
                    timeout=self.direct_task_heartbeat_seconds,
                )
                if not events:
                    heartbeat = b": nexus-heartbeat\n\n"
                    request.wfile.write(heartbeat)
                    request.wfile.flush()
                    total += len(heartbeat)
                for item in events:
                    cursor = int(item["seq"])
                    raw = _sse_bytes(
                        SseEvent(
                            event=str(item["event"]),
                            event_id=str(cursor),
                            data=json.dumps(
                                item["data"],
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ),
                        )
                    )
                    if len(raw) > self.max_stream_event_bytes:
                        raise AgentRequestError(
                            500,
                            "SSE_EVENT_TOO_LARGE",
                            "SSE event exceeds server bounds",
                        )
                    total += len(raw)
                    if total > self.max_response_bytes:
                        raise AgentRequestError(
                            500,
                            "STREAM_TOO_LARGE",
                            "SSE response exceeds server bounds",
                        )
                    request.wfile.write(raw)
                    request.wfile.flush()
                if task.status in TERMINAL_STATUSES and cursor >= task.public_status()["last_seq"]:
                    break
            request.wfile.write(b": nexus-stream-complete\n\n")
            request.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            request.close_connection = True

    def _send_cloud_handler_stream(
        self,
        request: BaseHTTPRequestHandler,
        envelope: AgentEnvelope,
        handler: SyncHandler,
    ) -> None:
        """Run a normal cloud handler behind an SSE response.

        OpenWrt IPv6 adapters use ``/invoke-stream`` for interactive cloud
        calls even when the Agent capability returns one final JSON value.
        Running the handler on a worker keeps the transport alive while
        ``ctx.chat.ask()`` waits for a Display or MCP reply.
        """

        outcome: "queue.Queue[Tuple[str, Any]]" = queue.Queue(maxsize=1)

        def execute() -> None:
            try:
                value = self._resolve_value(handler(envelope))
                if isinstance(value, AgentResponse):
                    if value.status >= 400:
                        result = ("error", {
                            "code": "HANDLER_REJECTED",
                            "message": "Agent handler rejected the request",
                        })
                    else:
                        result = ("result", value.body)
                else:
                    result = ("result", value)
            except AgentRequestError as exc:
                result = ("error", {"code": exc.code, "message": exc.message})
            except Exception as exc:
                code, message = _managed_service_error(exc)
                result = ("error", {
                    "code": code or "HANDLER_FAILED",
                    "message": message or "Agent handler failed",
                })
            # Keep the SSE transport alive until all Agent-authored state is
            # durable.  The result event is the Cloud's completion boundary.
            envelope.run_context.flush()
            outcome.put(result)

        worker = threading.Thread(
            target=execute,
            name="nexus-cloud-stream-" + envelope.task_id[:12],
            daemon=True,
        )
        worker.start()
        request.send_response(200)
        request.send_header("Content-Type", "text/event-stream")
        request.send_header("Cache-Control", "no-store")
        request.send_header("X-Accel-Buffering", "no")
        request.send_header("Connection", "close")
        request.end_headers()
        request.close_connection = True
        total = 0
        sequence = 0
        try:
            while True:
                try:
                    event_name, data = outcome.get(
                        timeout=self.direct_task_heartbeat_seconds,
                    )
                except queue.Empty:
                    heartbeat = b": nexus-heartbeat\n\n"
                    request.wfile.write(heartbeat)
                    request.wfile.flush()
                    total += len(heartbeat)
                    continue
                sequence += 1
                raw = _sse_bytes(SseEvent(
                    event=event_name,
                    event_id=str(sequence),
                    data=json.dumps(data, separators=(",", ":"), ensure_ascii=False),
                ))
                if len(raw) > self.max_stream_event_bytes:
                    raise AgentRequestError(
                        500, "SSE_EVENT_TOO_LARGE", "SSE event exceeds server bounds"
                    )
                total += len(raw)
                if total > self.max_response_bytes:
                    raise AgentRequestError(
                        500, "STREAM_TOO_LARGE", "SSE response exceeds server bounds"
                    )
                request.wfile.write(raw)
                request.wfile.flush()
                break
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            request.close_connection = True

    def _send_json(
        self,
        request: BaseHTTPRequestHandler,
        status: int,
        body: Any,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        if status < 100 or status > 599:
            raise AgentRequestError(500, "INVALID_RESPONSE", "handler returned an invalid status")
        try:
            raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise AgentRequestError(500, "INVALID_RESPONSE", "handler response is not JSON serializable") from exc
        if len(raw) > self.max_response_bytes:
            raise AgentRequestError(500, "RESPONSE_TOO_LARGE", "handler response exceeds server bounds")
        safe_headers = _safe_response_headers(headers or {})
        request.send_response(status)
        request.send_header("Content-Type", "application/json; charset=utf-8")
        request.send_header("Content-Length", str(len(raw)))
        request.send_header("Cache-Control", "no-store")
        for name, value in safe_headers.items():
            request.send_header(name, value)
        request.end_headers()
        if request.command != "HEAD":
            request.wfile.write(raw)

    def _send_error(self, request: BaseHTTPRequestHandler, status: int, code: str, message: str) -> None:
        if request.wfile.closed:
            return
        self._send_json(request, status, {"code": code, "message": message})

    def _send_stream(self, request: BaseHTTPRequestHandler, items: Iterable[StreamItem]) -> None:
        iterator = self._iter_values(items)
        try:
            first = next(iterator)
        except StopIteration:
            first = None
        request.send_response(200)
        request.send_header("Content-Type", "text/event-stream")
        request.send_header("Cache-Control", "no-store")
        request.send_header("X-Accel-Buffering", "no")
        request.send_header("Connection", "close")
        request.end_headers()
        request.close_connection = True
        total = 0
        try:
            sequence: Iterator[StreamItem]
            if first is None:
                sequence = iter(())
            else:
                def with_first() -> Iterator[StreamItem]:
                    yield first
                    yield from iterator
                sequence = with_first()
            for item in sequence:
                raw = _sse_bytes(item)
                if len(raw) > self.max_stream_event_bytes:
                    raise AgentRequestError(500, "SSE_EVENT_TOO_LARGE", "SSE event exceeds server bounds")
                total += len(raw)
                if total > self.max_response_bytes:
                    raise AgentRequestError(500, "STREAM_TOO_LARGE", "SSE response exceeds server bounds")
                request.wfile.write(raw)
                request.wfile.flush()
            complete = b": nexus-stream-complete\n\n"
            if total + len(complete) <= self.max_response_bytes:
                request.wfile.write(complete)
                request.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            # The SSE status has already been committed. Close instead of
            # attempting to emit a malformed second HTTP response.
            request.close_connection = True
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()

    def _resumable_stream(
        self,
        envelope: AgentEnvelope,
        handler: StreamHandler,
    ) -> Iterator[SseEvent]:
        store = self._resume_store
        if store is None:
            raise StreamResumeError(
                409, "STREAM_RESUME_DISABLED", "resumable streams are disabled"
            )
        identity = "\0".join((
            envelope.tenant,
            envelope.source_agent,
            envelope.intent,
            envelope.task_id,
        ))
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        request_data = dict(envelope.raw)
        request_data.pop("resume_from_event_id", None)
        fingerprint = hashlib.sha256(
            json.dumps(
                request_data,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

        def produce() -> Iterator[SseEvent]:
            for item in self._iter_values(handler(envelope)):
                yield _sse_event(item)

        return store.subscribe(
            key,
            fingerprint,
            produce,
            after_event_id=envelope.resume_from_event_id,
        )

    def serve_forever(self, poll_interval: float = 0.25) -> None:
        if self._closed.is_set():
            raise RuntimeError("Agent server is closed")
        self._serving.set()
        try:
            self._httpd.serve_forever(poll_interval=poll_interval)
        finally:
            self._serving.clear()

    def is_healthy(self) -> bool:
        """Return whether the local Agent listener is actively serving."""

        return (
            self._serving.is_set()
            and not self._closed.is_set()
            and self._httpd.socket.fileno() >= 0
        )

    def serve_in_thread(self, *, daemon: bool = True) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, daemon=daemon)
        thread.start()
        self._serving.wait(timeout=2.0)
        return thread

    def shutdown(self) -> None:
        self._httpd.shutdown()

    def server_close(self) -> None:
        self._closed.set()
        self._serving.clear()
        if self._resume_store is not None:
            self._resume_store.close()
        self._httpd.server_close()

    @contextmanager
    def registered(
        self,
        client: NexusAgentClient,
        registrations: Iterable[CapabilityRegistration],
        *,
        auto_renew: bool = True,
        renew_fraction: float = 0.6,
        health_check: Optional[Callable[[], bool]] = None,
        reregister_on_not_found: bool = True,
    ) -> Iterator[Tuple[AgentLease, ...]]:
        leases: List[AgentLease] = []
        try:
            for registration in registrations:
                leases.append(client.register(
                    registration,
                    auto_renew=auto_renew,
                    renew_fraction=renew_fraction,
                    health_check=health_check or self.is_healthy,
                    reregister_on_not_found=reregister_on_not_found,
                ))
            yield tuple(leases)
        finally:
            for lease in reversed(leases):
                try:
                    lease.close()
                except Exception:
                    pass

    def serve_registered(
        self,
        client: NexusAgentClient,
        registrations: Iterable[CapabilityRegistration],
        *,
        auto_renew: bool = True,
        renew_fraction: float = 0.6,
        health_check: Optional[Callable[[], bool]] = None,
        reregister_on_not_found: bool = True,
    ) -> None:
        with self.registered(
            client,
            registrations,
            auto_renew=auto_renew,
            renew_fraction=renew_fraction,
            health_check=health_check,
            reregister_on_not_found=reregister_on_not_found,
        ):
            self.serve_forever()

    def __enter__(self) -> "NexusAgentServer":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.server_close()
