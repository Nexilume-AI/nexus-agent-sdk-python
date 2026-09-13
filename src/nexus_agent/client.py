import json
import base64
import http.client
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Union

from .auth import AutoTokenProvider, NoTokenProvider, RouterAuthMetadata, TokenProvider
from .errors import (
    NexusAgentError,
    NexusAuthenticationError,
    NexusAuthorizationError,
    NexusHttpError,
)
from .models import (
    BackendTlsIdentity,
    CapabilityRegistration,
    CloudRegistrationStatus,
    LeaseInfo,
    PublicAgentEndpoint,
    SseEvent,
)

JsonObject = Dict[str, Any]


class NexusDirectTaskError(NexusAgentError):
    """A router-to-router Direct Task failed or became unavailable."""


@dataclass(frozen=True)
class NexusInteractionRequest:
    key: str
    prompt: str
    kind: str = "text"
    choices: tuple = ()


@dataclass
class NexusInvokeEvent:
    seq: int
    event: str
    data: Any
    _task: Optional["NexusDirectTask"] = field(default=None, repr=False, compare=False)

    @property
    def input_required(self) -> bool:
        return self.event == "input_required"

    @property
    def interaction(self) -> Optional[NexusInteractionRequest]:
        if not self.input_required or not isinstance(self.data, Mapping):
            return None
        return NexusInteractionRequest(
            key=str(self.data.get("key") or ""),
            prompt=str(self.data.get("prompt") or ""),
            kind=str(self.data.get("kind") or "text"),
            choices=tuple(dict(item) for item in self.data.get("choices") or ()),
        )

    def reply(self, value: Any) -> Mapping[str, Any]:
        interaction = self.interaction
        if self._task is None or interaction is None:
            raise NexusDirectTaskError("event is not a pending Direct Task interaction")
        return self._task.reply(key=interaction.key, value=value)


@dataclass
class NexusDirectTask:
    """Handle for one private Direct Invoke task.

    The task token is intentionally excluded from repr and public status
    payloads.  It is only sent to task-scoped control endpoints.
    """

    client: "NexusAgentClient" = field(repr=False)
    task_id: str
    _token: str = field(repr=False)
    _route_envelope: Optional[Mapping[str, Any]] = field(
        default=None, repr=False, compare=False
    )

    def _headers(self) -> Dict[str, str]:
        return {"X-Nexus-Task-Token": self._token}

    def _control(self, action: str, **payload: Any) -> Mapping[str, Any]:
        if self._route_envelope is None:
            raise NexusDirectTaskError("Direct Task has no routed control context")
        source = dict(self._route_envelope)
        direct_control = {
            "action": str(action),
            "direct_task_id": self.task_id,
            "direct_task_token": self._token,
            **payload,
        }
        control: JsonObject = {
            "version": "1.0",
            "intent": str(source.get("intent") or ""),
            "intent_version": int(source.get("intent_version") or 1),
            "task_id": str(uuid.uuid4()),
            "source_agent": str(source.get("source_agent") or ""),
            "tenant": str(source.get("tenant") or ""),
            "hop_limit": int(source.get("hop_limit") or 8),
            "constraints": {},
            "payload": {"nexus_direct_task_control": direct_control},
        }
        target = source.get("target_agent")
        if target:
            control["target_agent"] = target
        return self.client.invoke(control)

    def status(self) -> Mapping[str, Any]:
        if self._route_envelope is not None:
            return self._control("status")
        return self.client._request_json(
            "GET",
            f"/agent/v1/tasks/{urllib.parse.quote(self.task_id, safe='')}",
            headers=self._headers(),
        )

    def events(
        self,
        *,
        after: int = 0,
        follow: bool = False,
        poll_interval: float = 0.25,
    ) -> Iterator[NexusInvokeEvent]:
        cursor = int(after)
        while True:
            payload = (
                self._control("events", after=cursor)
                if self._route_envelope is not None
                else self.client._request_json(
                    "GET",
                    f"/agent/v1/tasks/{urllib.parse.quote(self.task_id, safe='')}/events?after={cursor}",
                    headers=self._headers(),
                )
            )
            for raw in payload.get("events") or ():
                if not isinstance(raw, Mapping):
                    continue
                cursor = int(raw.get("seq") or cursor)
                yield NexusInvokeEvent(
                    seq=cursor,
                    event=str(raw.get("event") or "message"),
                    data=raw.get("data"),
                    _task=self,
                )
            status = str(payload.get("status") or "")
            if not follow or status in {"completed", "failed", "cancelled"}:
                return
            time.sleep(max(0.02, min(float(poll_interval), 5.0)))

    def reply(self, *, key: str, value: Any) -> Mapping[str, Any]:
        if self._route_envelope is not None:
            return self._control("reply", key=str(key), value=value)
        return self.client._request_json(
            "POST",
            (
                f"/agent/v1/tasks/{urllib.parse.quote(self.task_id, safe='')}"
                f"/interactions/{urllib.parse.quote(str(key), safe='')}/reply"
            ),
            payload={"value": value},
            headers=self._headers(),
        )

    def cancel(self) -> Mapping[str, Any]:
        if self._route_envelope is not None:
            return self._control("cancel")
        return self.client._request_json(
            "DELETE",
            f"/agent/v1/tasks/{urllib.parse.quote(self.task_id, safe='')}",
            headers=self._headers(),
        )

    def asset(self, asset_id: str) -> bytes:
        """Download one private Browser asset produced by this task."""

        normalized = str(asset_id or "").strip()
        if not normalized:
            raise ValueError("asset_id is required")
        if self._route_envelope is not None:
            content = bytearray()
            offset = 0
            while True:
                payload = self._control(
                    "asset", asset_id=normalized, offset=offset, limit=192 * 1024
                )
                try:
                    chunk = base64.b64decode(str(payload.get("content_base64") or ""), validate=True)
                except (ValueError, TypeError) as exc:
                    raise NexusDirectTaskError("Agent returned an invalid Direct Task asset") from exc
                content.extend(chunk)
                if bool(payload.get("eof")):
                    return bytes(content)
                next_offset = int(payload.get("next_offset") or 0)
                if next_offset <= offset:
                    raise NexusDirectTaskError("Agent returned an invalid Direct Task asset cursor")
                offset = next_offset
        request = urllib.request.Request(
            (
                self.client.base_url
                + f"/agent/v1/tasks/{urllib.parse.quote(self.task_id, safe='')}"
                + f"/assets/{urllib.parse.quote(normalized, safe='')}"
            ),
            headers={**self.client._headers(), **self._headers()},
            method="GET",
        )
        with self.client._open(request) as response:
            return response.read()

    def result(self, timeout: float = 300.0) -> Any:
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while True:
            value = self.status()
            status = str(value.get("status") or "")
            if status == "completed":
                return value.get("result")
            if status == "failed":
                error = value.get("error") if isinstance(value.get("error"), Mapping) else {}
                raise NexusDirectTaskError(str(error.get("message") or "Direct Task failed"))
            if status == "cancelled":
                raise NexusDirectTaskError("Direct Task was cancelled")
            if time.monotonic() >= deadline:
                raise NexusDirectTaskError("Direct Task result timed out")
            time.sleep(0.1)


class _TlsServerNameHTTPSConnection(http.client.HTTPSConnection):
    """Connect to one authority while verifying a separate TLS identity."""

    def __init__(self, host: str, *, tls_server_name: str, **kwargs: Any) -> None:
        self._nexus_tls_server_name = tls_server_name
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        # HTTPConnection.connect() retains normal source-address, timeout and
        # CONNECT-tunnel behavior, but does not wrap the resulting socket.
        http.client.HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=self._nexus_tls_server_name,
        )


class _TlsServerNameHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, *, context: ssl.SSLContext, tls_server_name: str) -> None:
        super().__init__(context=context)
        self._nexus_tls_server_name = tls_server_name

    def _connection(self, host: str, **kwargs: Any):
        return _TlsServerNameHTTPSConnection(
            host,
            tls_server_name=self._nexus_tls_server_name,
            **kwargs,
        )

    def https_open(self, request: urllib.request.Request):
        arguments = {"context": self._context}
        # Python 3.9-3.11 exposed this deprecated handler attribute; newer
        # runtimes rely solely on SSLContext.check_hostname.
        if hasattr(self, "_check_hostname"):
            arguments["check_hostname"] = self._check_hostname
        return self.do_open(self._connection, request, **arguments)


class NexusAgentClient:
    """Synchronous client for an Agent Access Proxy endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        token: Optional[str] = None,
        token_provider: Optional[TokenProvider] = None,
        auth: Optional[Union[str, TokenProvider]] = None,
        transaction_token: Optional[str] = None,
        ca_file: Optional[str] = None,
        cert_file: Optional[str] = None,
        key_file: Optional[str] = None,
        tls_server_name: Optional[str] = None,
        use_environment_proxy: bool = True,
        timeout: float = 10.0,
        user_agent: str = "nexus-agent-sdk-python/0.35.0",
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        if bool(cert_file) != bool(key_file):
            raise ValueError("cert_file and key_file must be supplied together")
        if auth is not None:
            if any(value is not None for value in (token, token_provider, transaction_token)):
                raise ValueError(
                    "auth, token, token_provider and transaction_token are mutually exclusive"
                )
            if auth == "none":
                token_provider = NoTokenProvider()
            elif auth == "auto":
                token_provider = AutoTokenProvider(
                    base_url,
                    timeout=timeout,
                    router_ca_file=ca_file,
                    use_environment_proxy=use_environment_proxy,
                )
            elif isinstance(auth, str):
                raise ValueError("auth must be 'none', 'auto' or a TokenProvider")
            else:
                token_provider = auth
        elif token is None and token_provider is None and transaction_token is None:
            token_provider = AutoTokenProvider(
                base_url,
                timeout=timeout,
                router_ca_file=ca_file,
                use_environment_proxy=use_environment_proxy,
            )
        if sum(value is not None for value in (token, token_provider, transaction_token)) > 1:
            raise ValueError(
                "token, token_provider and transaction_token are mutually exclusive"
            )
        if tls_server_name is not None:
            if parsed.scheme != "https":
                raise ValueError("tls_server_name requires an https base_url")
            if (
                not isinstance(tls_server_name, str)
                or not tls_server_name
                or tls_server_name != tls_server_name.strip()
                or len(tls_server_name) > 253
            ):
                raise ValueError(
                    "tls_server_name must be a non-empty TLS identity up to 253 characters"
                )
        if not isinstance(use_environment_proxy, bool):
            raise ValueError("use_environment_proxy must be a boolean")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.token_provider = token_provider
        self.transaction_token = transaction_token
        self.timeout = timeout
        self.user_agent = user_agent
        self.tls_server_name = tls_server_name
        self.use_environment_proxy = use_environment_proxy
        self.ssl_context: Optional[ssl.SSLContext] = None
        self._opener = None
        if parsed.scheme == "https":
            self.ssl_context = ssl.create_default_context(cafile=ca_file)
            if cert_file and key_file:
                self.ssl_context.load_cert_chain(cert_file, key_file)
        if tls_server_name is not None or not use_environment_proxy:
            handlers = []
            if not use_environment_proxy:
                handlers.append(urllib.request.ProxyHandler({}))
            if self.ssl_context is not None:
                if tls_server_name is not None:
                    handlers.append(_TlsServerNameHTTPSHandler(
                        context=self.ssl_context,
                        tls_server_name=tls_server_name,
                    ))
                else:
                    handlers.append(urllib.request.HTTPSHandler(
                        context=self.ssl_context
                    ))
            self._opener = urllib.request.build_opener(*handlers)

    def _bind_identity(self, *, tenant: str, origin: str) -> None:
        binder = getattr(self.token_provider, "bind_registration", None)
        if callable(binder):
            binder(tenant=tenant, origin=origin)

    def router_auth_metadata(
        self, *, tenant: str, origin: str
    ) -> Optional[RouterAuthMetadata]:
        """Reuse automatic-auth discovery without acquiring or exposing a token."""

        self._bind_identity(tenant=tenant, origin=origin)
        if not isinstance(self.token_provider, AutoTokenProvider):
            return None
        return self.token_provider.metadata

    def _bind_envelope_identity(self, envelope: Mapping[str, Any]) -> None:
        if not callable(getattr(self.token_provider, "bind_registration", None)):
            return
        tenant = envelope.get("tenant")
        source_agent = envelope.get("source_agent")
        if isinstance(tenant, str) and tenant and isinstance(source_agent, str) \
                and source_agent:
            prefix = f"agent://{tenant}/"
            if source_agent.startswith("agent://"):
                if not source_agent.startswith(prefix) or source_agent == prefix:
                    raise ValueError("source_agent URI must belong to the Envelope tenant")
                origin = source_agent
            else:
                origin = prefix + source_agent
            self._bind_identity(
                tenant=tenant,
                origin=origin,
            )

    def _headers(
        self, *, sse: bool = False, last_event_id: Optional[int] = None
    ) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if sse else "application/json",
            "User-Agent": self.user_agent,
        }
        token = self.token
        if self.token_provider is not None:
            token = self.token_provider.get_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif self.transaction_token:
            headers["Txn-Token"] = self.transaction_token
        if last_event_id is not None and last_event_id > 0:
            headers["Last-Event-ID"] = str(last_event_id)
        return headers

    def _open_once(self, request: urllib.request.Request):
        if self._opener is not None:
            return self._opener.open(request, timeout=self.timeout)
        return urllib.request.urlopen(request, timeout=self.timeout,
                                      context=self.ssl_context)

    def _replace_provider_token(self, request: urllib.request.Request) -> None:
        assert self.token_provider is not None
        token = self.token_provider.get_token(force_refresh=True)
        request.remove_header("Authorization")
        if token:
            request.add_header("Authorization", f"Bearer {token}")

    def _open(self, request: urllib.request.Request):
        try:
            try:
                return self._open_once(request)
            except urllib.error.HTTPError as first:
                can_refresh = self.token_provider is not None and bool(
                    getattr(self.token_provider, "refreshable", False)
                )
                if first.code != 401 or not can_refresh:
                    raise
                first.close()
                self._replace_provider_token(request)
                return self._open_once(request)
        except urllib.error.HTTPError as exc:
            code = "HTTP_ERROR"
            message = str(exc.reason)
            try:
                raw = exc.read()
                message = raw.decode("utf-8", "replace") or exc.reason
                try:
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        error = payload.get("error")
                        if isinstance(error, dict):
                            code = str(error.get("code", code))
                            message = str(error.get("message", message))
                        else:
                            code = str(payload.get("code", code))
                            message = str(payload.get("message", message))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            finally:
                exc.close()
            error_class = NexusHttpError
            if exc.code == 401 or code == "AUTHENTICATION_REQUIRED":
                error_class = NexusAuthenticationError
            elif exc.code == 403 or code == "INSUFFICIENT_SCOPE":
                error_class = NexusAuthorizationError
            raise error_class(exc.code, code, message) from exc
        except urllib.error.URLError as exc:
            raise NexusAgentError(f"Agent Access Proxy is unavailable: {exc.reason}") from exc
        except (OSError, http.client.HTTPException) as exc:
            # Custom TLS-name connections can surface handshake and socket
            # failures directly instead of wrapping them in URLError.
            raise NexusAgentError(f"Agent Access Proxy is unavailable: {exc}") from exc

    def _post_json(self, path: str, payload: Mapping[str, Any]) -> JsonObject:
        return self._request_json("POST", path, payload=payload)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        return_headers: bool = False,
    ):
        body = None if payload is None else json.dumps(
            payload, separators=(",", ":")
        ).encode("utf-8")
        request_headers = self._headers()
        request_headers.update({str(k): str(v) for k, v in (headers or {}).items()})
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=request_headers,
            method=str(method).upper(),
        )
        with self._open(request) as response:
            raw = response.read()
            response_headers = dict(response.headers.items())
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise NexusAgentError("Proxy returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise NexusAgentError("Proxy response must be a JSON object")
        return (result, response_headers) if return_headers else result

    @staticmethod
    def _lease_info(payload: Mapping[str, Any]) -> LeaseInfo:
        try:
            endpoint_payload = payload.get("public_endpoint")
            endpoint = None
            if endpoint_payload is not None:
                if not isinstance(endpoint_payload, Mapping):
                    raise ValueError("public_endpoint must be an object")
                endpoint = PublicAgentEndpoint.from_dict(endpoint_payload)
            else:
                flat_names = ("public_port", "tls_server_name", "ca_bundle_id")
                present = [payload.get(name) is not None for name in flat_names]
                if any(present):
                    if not all(present) or payload.get("public_ipv6") is None:
                        raise ValueError("public endpoint fields are incomplete")
                    endpoint = PublicAgentEndpoint(
                        address=str(payload["public_ipv6"]),
                        port=int(payload["public_port"]),
                        tls_server_name=str(payload["tls_server_name"]),
                        ca_bundle_id=str(payload["ca_bundle_id"]),
                    )
            public_ipv6 = (
                str(payload["public_ipv6"])
                if payload.get("public_ipv6") is not None
                else None
            )
            if endpoint is not None and public_ipv6 != endpoint.address:
                raise ValueError("public endpoint address does not match public_ipv6")
            return LeaseInfo(
                route_id=str(payload["route_id"]),
                generation=int(payload["generation"]),
                lease_seconds=int(payload.get("lease_seconds", 0)),
                removed=bool(payload.get("removed", False)),
                public_ipv6=public_ipv6,
                public_endpoint=endpoint,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NexusAgentError("Proxy returned an invalid lease response") from exc

    def register(
        self,
        registration: CapabilityRegistration,
        *,
        auto_renew: bool = False,
        renew_fraction: float = 0.6,
        health_check: Optional[Callable[[], bool]] = None,
        reregister_on_not_found: bool = True,
    ) -> "AgentLease":
        self._bind_identity(tenant=registration.tenant, origin=registration.origin)
        info = self._lease_info(
            self._post_json("/agent/v1/register", registration.to_dict())
        )
        lease = AgentLease(
            self,
            info,
            registration=registration,
            renew_fraction=renew_fraction,
            health_check=health_check,
            reregister_on_not_found=reregister_on_not_found,
        )
        if auto_renew:
            lease.start_auto_renew()
        return lease

    def renew(
        self,
        route_id: str,
        *,
        lease_seconds: Optional[int] = None,
        latency_ms: Optional[int] = None,
        load_permille: Optional[int] = None,
        healthy: Optional[bool] = None,
        backend_tls: Optional[BackendTlsIdentity] = None,
        endpoint: Optional[str] = None,
    ) -> LeaseInfo:
        payload: JsonObject = {"route_id": route_id}
        for name, value in (
            ("lease_seconds", lease_seconds),
            ("latency_ms", latency_ms),
            ("load_permille", load_permille),
            ("healthy", healthy),
            ("endpoint", endpoint),
        ):
            if value is not None:
                payload[name] = value
        if backend_tls is not None:
            payload["backend_tls"] = backend_tls.to_dict()
        return self._lease_info(self._post_json("/agent/v1/renew", payload))

    def unregister(self, route_id: str) -> LeaseInfo:
        return self._lease_info(
            self._post_json("/agent/v1/unregister", {"route_id": route_id})
        )

    def cloud_status(
        self, *, tenant: str, origin: str
    ) -> CloudRegistrationStatus:
        """Return this trusted-LAN identity's asynchronous Cloud state."""

        self._bind_identity(tenant=tenant, origin=origin)
        try:
            value = self._request_json("GET", "/agent/v1/cloud-registration")
        except NexusHttpError as exc:
            if exc.status != 404:
                raise
            return CloudRegistrationStatus(
                state="unsupported",
                origin=origin,
                message="router does not support managed Cloud Agent registration",
            )
        try:
            return CloudRegistrationStatus.from_dict(value)
        except (TypeError, ValueError) as exc:
            raise NexusAgentError(
                "Router returned an invalid Cloud registration status"
            ) from exc

    def route(self, envelope: Mapping[str, Any]) -> JsonObject:
        self._bind_envelope_identity(envelope)
        return self._post_json("/agent/v1/route", envelope)

    def invoke(self, envelope: Mapping[str, Any]) -> JsonObject:
        self._bind_envelope_identity(envelope)
        return self._post_json("/agent/v1/invoke", envelope)

    def invoke_async(self, envelope: Mapping[str, Any]) -> NexusDirectTask:
        """Start a router-to-router Direct Task without blocking the caller."""

        self._bind_envelope_identity(envelope)
        payload, headers = self._request_json(
            "POST",
            "/agent/v1/invoke",
            payload=envelope,
            headers={"Prefer": "respond-async"},
            return_headers=True,
        )
        normalized = {str(name).lower(): str(value) for name, value in headers.items()}
        task_id = str(normalized.get("x-nexus-task-id") or payload.get("task_id") or "")
        task_token = str(normalized.get("x-nexus-task-token") or "")
        if not task_id or not task_token:
            raise NexusDirectTaskError("Agent did not return Direct Task control headers")
        return NexusDirectTask(self, task_id, task_token, dict(envelope))

    def invoke_interactive(
        self,
        envelope: Mapping[str, Any],
    ) -> Iterator[NexusInvokeEvent]:
        """Stream one interactive Direct Invoke.

        Every yielded interaction event carries a private task handle so
        ``event.reply(value)`` can resume the waiting Agent handler.
        """

        self._bind_envelope_identity(envelope)
        body = json.dumps(dict(envelope), separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/agent/v1/invoke-stream",
            data=body,
            headers={**self._headers(sse=True), "X-Nexus-Interactive": "1"},
            method="POST",
        )
        with self._open(request) as response:
            normalized = {
                str(name).lower(): str(value) for name, value in response.headers.items()
            }
            task_id = normalized.get("x-nexus-task-id", "")
            task_token = normalized.get("x-nexus-task-token", "")
            if not task_id or not task_token:
                raise NexusDirectTaskError("Agent did not return interactive task headers")
            task = NexusDirectTask(self, task_id, task_token, dict(envelope))
            event: Dict[str, Any] = {"data": []}
            for raw_line in response:
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if not line:
                    if event["data"]:
                        raw_data = "\n".join(event["data"])
                        try:
                            data: Any = json.loads(raw_data)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            data = raw_data
                        try:
                            sequence = int(event.get("id") or 0)
                        except (TypeError, ValueError):
                            sequence = 0
                        yield NexusInvokeEvent(
                            seq=sequence,
                            event=str(event.get("event") or "message"),
                            data=data,
                            _task=task,
                        )
                    event = {"data": []}
                    continue
                if line.startswith(":"):
                    continue
                field_name, separator, value = line.partition(":")
                if separator and value.startswith(" "):
                    value = value[1:]
                if field_name == "data":
                    event["data"].append(value)
                elif field_name in {"event", "id"}:
                    event[field_name] = value

    def invoke_intent(
        self,
        intent: str,
        payload: Any,
        *,
        tenant: str,
        source_agent: str,
        target_agent: Optional[str] = None,
        intent_version: int = 1,
        task_id: Optional[str] = None,
        hop_limit: int = 8,
        constraints: Optional[Mapping[str, Any]] = None,
    ) -> JsonObject:
        envelope: JsonObject = {
            "version": "1.0",
            "intent": intent,
            "intent_version": intent_version,
            "task_id": task_id or str(uuid.uuid4()),
            "source_agent": source_agent,
            "tenant": tenant,
            "hop_limit": hop_limit,
            "constraints": dict(constraints or {}),
            "payload": payload,
        }
        if target_agent is not None:
            if (
                not isinstance(target_agent, str)
                or not target_agent
                or len(target_agent) > 255
            ):
                raise ValueError(
                    "target_agent must be a non-empty string up to 255 characters"
                )
            envelope["target_agent"] = target_agent
        return self.invoke(envelope)

    def invoke_stream(
        self,
        envelope: Mapping[str, Any],
        *,
        resume: bool = True,
        last_event_id: int = 0,
        max_reconnects: int = 3,
        reconnect_delay: float = 0.25,
    ) -> Iterator[SseEvent]:
        """Invoke an SSE capability and resume safely after transport loss.

        P8.9 Agents assign monotonic event IDs and retain a bounded task
        history.  Reconnects reuse the original task_id and send both the
        standard ``Last-Event-ID`` header and the routed Envelope cursor.
        """

        self._bind_envelope_identity(envelope)
        if not isinstance(last_event_id, int) or isinstance(last_event_id, bool) \
                or last_event_id < 0:
            raise ValueError("last_event_id must be a non-negative integer")
        if not 0 <= max_reconnects <= 100:
            raise ValueError("max_reconnects must be between 0 and 100")
        if not 0 <= reconnect_delay <= 60:
            raise ValueError("reconnect_delay must be between 0 and 60")
        if resume and self.transaction_token and max_reconnects > 0:
            raise ValueError(
                "resumable streams require an access JWT; a one-time "
                "Transaction Token cannot authenticate a reconnect"
            )
        if resume:
            task_id = envelope.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("resumable stream Envelope requires task_id")

        cursor = last_event_id
        reconnects = 0
        suggested_delay = reconnect_delay
        while True:
            payload = dict(envelope)
            if cursor > 0:
                payload["resume_from_event_id"] = cursor
            else:
                payload.pop("resume_from_event_id", None)
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request = urllib.request.Request(
                self.base_url + "/agent/v1/invoke-stream",
                data=body,
                headers=self._headers(
                    sse=True,
                    last_event_id=cursor if cursor > 0 else None,
                ),
                method="POST",
            )
            completed = False
            saw_numeric_id = cursor > 0
            try:
                with self._open(request) as response:
                    event: Dict[str, Any] = {"data": []}
                    for raw_line in response:
                        line = raw_line.decode("utf-8").rstrip("\r\n")
                        if line == "":
                            if event["data"]:
                                retry = event.get("retry")
                                item = SseEvent(
                                    data="\n".join(event["data"]),
                                    event=event.get("event"),
                                    event_id=event.get("id"),
                                    retry_ms=int(retry) if retry is not None else None,
                                )
                                if item.event_id is not None:
                                    try:
                                        next_cursor = int(item.event_id)
                                    except ValueError as exc:
                                        if resume:
                                            raise NexusAgentError(
                                                "resumable stream returned a non-numeric event ID"
                                            ) from exc
                                    else:
                                        if next_cursor <= cursor:
                                            raise NexusAgentError(
                                                "resumable stream event IDs are not increasing"
                                            )
                                        cursor = next_cursor
                                        saw_numeric_id = True
                                if item.retry_ms is not None:
                                    suggested_delay = min(60.0, item.retry_ms / 1000.0)
                                yield item
                            event = {"data": []}
                            continue
                        if line.startswith(":"):
                            if line.strip() == ": nexus-stream-complete":
                                completed = True
                            continue
                        field, separator, value = line.partition(":")
                        if separator and value.startswith(" "):
                            value = value[1:]
                        if field == "data":
                            event["data"].append(value)
                        elif field in ("event", "id", "retry"):
                            event[field] = value
            except NexusHttpError:
                raise
            except (
                NexusAgentError,
                http.client.IncompleteRead,
                ConnectionError,
                OSError,
            ) as exc:
                if not resume or reconnects >= max_reconnects or not saw_numeric_id:
                    raise NexusAgentError(
                        f"Agent stream disconnected and could not resume: {exc}"
                    ) from exc
            else:
                if completed or not resume:
                    return
                if not saw_numeric_id:
                    raise NexusAgentError(
                        "stream ended without the P8.9 completion marker or event IDs"
                    )
                if reconnects >= max_reconnects:
                    raise NexusAgentError(
                        "stream ended before completion and reconnect limit was reached"
                    )
            reconnects += 1
            if suggested_delay > 0:
                time.sleep(suggested_delay)


class AgentLease:
    """A registered route lease with optional background renewal."""

    def __init__(
        self,
        client: NexusAgentClient,
        info: LeaseInfo,
        *,
        registration: Optional[CapabilityRegistration] = None,
        renew_fraction: float = 0.6,
        health_check: Optional[Callable[[], bool]] = None,
        reregister_on_not_found: bool = True,
    ) -> None:
        if not 0.2 <= renew_fraction <= 0.8:
            raise ValueError("renew_fraction must be between 0.2 and 0.8")
        self.client = client
        self.info = info
        self.registration = registration
        self.renew_fraction = renew_fraction
        self.health_check = health_check
        self.reregister_on_not_found = bool(reregister_on_not_found)
        self.last_error: Optional[BaseException] = None
        self.reregister_count = 0
        self.manifest_refresh_count = 0
        self.health_check_failures = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._closed = False

    @property
    def route_id(self) -> str:
        with self._lock:
            return self.info.route_id

    @property
    def public_ipv6(self) -> Optional[str]:
        with self._lock:
            return self.info.public_ipv6

    @property
    def public_endpoint(self) -> Optional[PublicAgentEndpoint]:
        with self._lock:
            return self.info.public_endpoint

    def _registration_with_updates(self, updates: Mapping[str, Any]) -> CapabilityRegistration:
        if self.registration is None:
            raise NexusAgentError("the original capability registration is unavailable")
        allowed = {
            name: updates[name]
            for name in ("lease_seconds", "latency_ms", "load_permille")
            if name in updates and updates[name] is not None
        }
        return replace(self.registration, **allowed) if allowed else self.registration

    def renew(self, **updates: Any) -> LeaseInfo:
        with self._lock:
            if self._closed:
                raise NexusAgentError("lease is closed")
            if "lease_seconds" not in updates:
                updates["lease_seconds"] = self.info.lease_seconds
            if (
                "backend_tls" not in updates
                and self.registration is not None
                and self.registration.backend_tls is not None
            ):
                updates["backend_tls"] = self.registration.backend_tls
            if (
                "endpoint" not in updates
                and self.registration is not None
                and self.registration.backend_tls is not None
            ):
                updates["endpoint"] = self.registration.endpoint
            try:
                # Cloud tool metadata is carried by registration, not by the
                # lightweight renew request. Refresh the same route_id so an
                # agentd restart or manifest-table repair cannot leave a
                # healthy LAN lease permanently absent from Nexus Cloud.
                if (
                    self.registration is not None
                    and self.registration.cloud is not None
                    and updates.get("healthy") is not False
                ):
                    registration = replace(
                        self._registration_with_updates(updates),
                        route_id=self.info.route_id,
                    )
                    self.info = self.client._lease_info(
                        self.client._post_json(
                            "/agent/v1/register", registration.to_dict()
                        )
                    )
                    self.registration = replace(registration, route_id=None)
                    self.manifest_refresh_count += 1
                else:
                    self.info = self.client.renew(self.info.route_id, **updates)
            except NexusHttpError as exc:
                if (
                    exc.status != 404
                    or not self.reregister_on_not_found
                    or self.registration is None
                    or updates.get("healthy") is False
                ):
                    raise
                registration = self._registration_with_updates(updates)
                self.info = self.client._lease_info(
                    self.client._post_json(
                        "/agent/v1/register", registration.to_dict()
                    )
                )
                self.registration = registration
                self.reregister_count += 1
            self.last_error = None
            return self.info

    def start_auto_renew(self) -> "AgentLease":
        with self._lock:
            if self._closed:
                raise NexusAgentError("lease is closed")
            if self._thread and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._renew_loop,
                name=f"nexus-lease-{self.info.route_id[:8]}",
                daemon=True,
            )
            self._thread.start()
        return self

    def _renew_loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            base = max(1.0, self.info.lease_seconds * self.renew_fraction)
            delay = base * random.uniform(0.9, 1.1)
            if self._stop.wait(delay):
                return
            if self.health_check is not None:
                health_error: Optional[BaseException] = None
                try:
                    healthy = bool(self.health_check())
                except BaseException as exc:
                    healthy = False
                    health_error = exc
                if not healthy:
                    health_error = health_error or NexusAgentError(
                        "local Agent is unhealthy; automatic renewal stopped"
                    )
                    try:
                        with self._lock:
                            if not self._closed:
                                self.info = self.client.unregister(
                                    self.info.route_id
                                )
                    except BaseException as exc:
                        self.last_error = exc
                    else:
                        self.last_error = health_error
                    finally:
                        self.health_check_failures += 1
                    return
            try:
                self.renew()
                failures = 0
            except BaseException as exc:  # retained for operator inspection
                self.last_error = exc
                failures += 1
                retry = min(max(0.5, 2 ** (failures - 1)), max(1.0, base / 2))
                if self._stop.wait(retry):
                    return

    def close(self, *, unregister: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=min(2.0, self.client.timeout))
        if unregister and not self.info.removed:
            self.info = self.client.unregister(self.route_id)

    def __enter__(self) -> "AgentLease":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
