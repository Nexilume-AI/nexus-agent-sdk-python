"""Optional native FastMCP listener sharing a PublicIPv6Agent address lease.

No protocol translation: FastMCP owns HTTP sessions, resources, prompts, tools,
callbacks and cancellation. Imports stay lazy so ordinary Invoke has no extras.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import socket
import ssl
import sys
import threading
import time
from typing import Any, Optional

from .errors import NexusAgentError
from .server_auth import HmacJwtServerAuth, NoServerAuth, ServerAuthenticationError


class _ResetSafeSocket(socket.socket):
    """Allow Proactor to finish closing a socket whose peer has reset it.

    CPython's Proactor calls shutdown before close/detach, but does not handle
    WSAECONNRESET there. That exception otherwise skips the actual cleanup.
    Only the already-reset shutdown case is terminal success; I/O errors and
    all other shutdown errors retain their normal behavior.
    """

    def shutdown(self, how: int) -> None:
        try:
            super().shutdown(how)
        except ConnectionResetError:
            if how != socket.SHUT_RDWR:
                raise


def _listener_loop() -> asyncio.AbstractEventLoop:
    if sys.platform != "win32":
        return asyncio.new_event_loop()

    def owned_socket(sock):
        timeout = sock.gettimeout()
        safe = _ResetSafeSocket(sock.family, sock.type, sock.proto, fileno=sock.detach())
        safe.settimeout(timeout)
        return safe

    class ListenerProactorLoop(asyncio.ProactorEventLoop):
        # Confine the CPython compatibility workaround to this owned loop.
        # Keep Proactor (including subprocess support); do not change global
        # loop policy, exception handlers, or sockets belonging to the caller.
        def _make_socket_transport(self, sock, protocol, waiter=None, extra=None, server=None):
            # Outbound sockets are already registered with IOCP during ConnectEx;
            # replacing their identity would attempt to register the handle twice.
            if server is not None:
                sock = owned_socket(sock)
            return super()._make_socket_transport(sock, protocol, waiter, extra, server)

        def _make_ssl_transport(self, sock, *args, **kwargs):
            if kwargs.get("server") is not None:
                sock = owned_socket(sock)
            return super()._make_ssl_transport(sock, *args, **kwargs)

    return ListenerProactorLoop()


class _DrainRequests:
    """Cancel only this listener's HTTP streams before Uvicorn drains them."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.draining = False
        self.active = set()

    def drain(self) -> None:
        # Called on the listener loop, never from the closing caller's thread.
        self.draining = True
        for cancel_scope in tuple(self.active):
            cancel_scope.cancel()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        import anyio

        started = complete = False

        async def track(message):
            nonlocal started, complete
            await send(message)
            if message["type"] == "http.response.start":
                started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True

        if not self.draining:
            with anyio.CancelScope() as cancel_scope:
                self.active.add(cancel_scope)
                try:
                    await self.app(scope, receive, track)
                finally:
                    self.active.discard(cancel_scope)
            if not cancel_scope.cancel_called:
                return
        # The SSE writer/reader and producer have now exited their contexts.
        # Finish HTTP framing instead of aborting a chunked response mid-body.
        # No JSON-RPC success is fabricated and no operation is replayed.
        if not complete:
            if not started:
                await send({"type": "http.response.start", "status": 503,
                            "headers": [(b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})


class _SessionBinding:
    """Bind opaque MCP session IDs to authenticated callers and this listener.

    The wrapper works with either Nexus JWT or native FastMCP auth, without an
    unbounded ownership cache. Every session operation is still delegated to MCP.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self._key = secrets.token_bytes(32)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = getattr(scope.get("user"), "access_token", None)
        claims = getattr(token, "claims", {}) or {}
        principal = json.dumps([getattr(token, "client_id", None), claims.get("iss"),
                                claims.get("sub"), claims.get("tenant"),
                                claims.get("source_agent")], separators=(",", ":")).encode()

        def signature(value):
            return hmac.new(self._key, principal + b"\0" + value, hashlib.sha256).hexdigest().encode()

        sessions = [v for k, v in scope["headers"] if k.lower() == b"mcp-session-id"]
        if sessions:
            try:
                encoded, mac = sessions[0].rsplit(b".", 1)
                if len(sessions) != 1 or len(encoded) > 512 or not hmac.compare_digest(mac, signature(encoded)):
                    raise ValueError
                raw_id = base64.b64decode(encoded, altchars=b"-_", validate=True)
            except (ValueError, TypeError):
                await send({"type": "http.response.start", "status": 404,
                            "headers": [(b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
                return
            scope = dict(scope, headers=[(k, raw_id if k.lower() == b"mcp-session-id" else v)
                                         for k, v in scope["headers"]])

        async def bound_send(message):
            if message["type"] == "http.response.start":
                headers = []
                for key, value in message.get("headers", []):
                    if key.lower() == b"mcp-session-id":
                        encoded = base64.urlsafe_b64encode(value)
                        value = encoded + b"." + signature(encoded)
                    headers.append((key, value))
                message = dict(message, headers=headers)
            await send(message)

        await self.app(scope, receive, bound_send)


class _BoundedRequest:
    """Bound even chunked MCP request bodies without changing response streams."""

    def __init__(self, app: Any, *, maximum: int, timeout: float) -> None:
        self.app, self.maximum, self.timeout = app, maximum, timeout

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def read_body():
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return None
                body.extend(message.get("body", b""))
                if len(body) > self.maximum:
                    raise OverflowError
                if not message.get("more_body", False):
                    return bytes(body)

        try:
            body = await asyncio.wait_for(read_body(), timeout=self.timeout)
        except (OverflowError, asyncio.TimeoutError) as exc:
            status = 413 if isinstance(exc, OverflowError) else 408
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        if body is None:
            return
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def _application(mcp: Any, auth: Any, tenant: str, host: str,
                 tls_server_name: Optional[str], maximum: int, timeout: float) -> Any:
    try:
        from importlib.metadata import version
        from packaging.version import Version
        from fastmcp import FastMCP
        from fastmcp.server.auth import AccessToken, TokenVerifier
        from fastmcp.server.http import create_streamable_http_app
        from starlette.middleware import Middleware
    except ImportError as exc:
        raise NexusAgentError("Native MCP requires nexilume[fastmcp] on Python 3.10+") from exc
    if not Version("1.30") <= Version(version("mcp")) < Version("2"):
        raise NexusAgentError("Native IPv6 MCP requires mcp>=1.30,<2 for safe SSE cleanup; "
                              "upgrade nexilume[fastmcp]")
    from .fastmcp import NexusMCPServer

    raw = mcp.fastmcp if isinstance(mcp, NexusMCPServer) else mcp
    if not isinstance(raw, FastMCP):
        raise TypeError("mcp must be a FastMCP or NexusMCPServer instance")
    provider = raw.auth
    if not isinstance(auth, NoServerAuth):
        if not isinstance(auth, HmacJwtServerAuth):
            raise ValueError("Native MCP requires NoServerAuth or HmacJwtServerAuth; "
                             "configure other MCP authentication on FastMCP explicitly")
        if provider is not None:
            raise ValueError("Choose either FastMCP authentication or inherited Nexus JWT, not both")

        class InheritedJWT(TokenVerifier):
            async def verify_token(self, token: str) -> Optional[AccessToken]:
                try:
                    caller = auth.authenticate_mcp("Bearer " + token, tenant=tenant)
                except ServerAuthenticationError:
                    return None
                # Stable across token renewal, distinct across callers and tenants.
                identity = json.dumps([caller.subject, tenant, caller.claims["source_agent"]])
                return AccessToken(
                    token=token,
                    client_id=hashlib.sha256(identity.encode()).hexdigest(),
                    scopes=list(caller.scopes),
                    expires_at=int(caller.claims["exp"]),
                    claims=dict(caller.claims),
                )

        provider = InheritedJWT(required_scopes=[auth.required_scope] if auth.required_scope else [])

    # Use FastMCP's app factory to supply inherited auth without mutating the
    # caller's FastMCP instance or replacing its tools/lifespan with a proxy.
    try:
        return create_streamable_http_app(
            server=raw, streamable_http_path="/mcp", auth=provider,
            stateless_http=False, json_response=False,
            host_origin_protection=True,
            allowed_hosts=[host] + ([tls_server_name] if tls_server_name else []),
            middleware=[Middleware(_SessionBinding),
                        Middleware(_BoundedRequest, maximum=maximum, timeout=timeout)],
        )
    except TypeError as exc:
        raise NexusAgentError("Native IPv6 MCP requires FastMCP >=3.4.7,<4; upgrade nexilume[fastmcp]") from exc


class NativeMCPListener:
    """An IPv6-only, optionally TLS/mTLS, lifecycle-managed ASGI listener."""

    def __init__(self, mcp: Any, *, address: str, port: int, auth: Any, tenant: str,
                 cert_file: Optional[str] = None, key_file: Optional[str] = None,
                 client_ca_file: Optional[str] = None, tls_server_name: Optional[str] = None,
                 max_request_bytes: int = 65536, request_timeout: float = 15.0) -> None:
        try:
            import uvicorn
        except ImportError as exc:
            raise NexusAgentError("Native MCP requires nexilume[fastmcp]") from exc
        app = _application(mcp, auth, tenant, address, tls_server_name,
                           max_request_bytes, request_timeout)
        self._app = _DrainRequests(app)
        config = uvicorn.Config(
            self._app, host=address, port=port, loop="asyncio", http="h11", ws="none",
            interface="asgi3",
            lifespan="on", access_log=False, log_config=None, proxy_headers=False,
            timeout_graceful_shutdown=1,
            ssl_certfile=cert_file, ssl_keyfile=key_file,
            ssl_ca_certs=client_ca_file,
            ssl_cert_reqs=ssl.CERT_REQUIRED if client_ca_file else ssl.CERT_NONE,
        )
        config.load()  # Validate TLS before reserving a listening socket.
        self._server = uvicorn.Server(config)
        self._socket = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            self._socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self._socket.bind((address, port))
            self._socket.setblocking(False)
        except BaseException:
            self._socket.close()
            raise
        self.port = self._socket.getsockname()[1]
        self.thread: Optional[threading.Thread] = None
        self._error: Optional[BaseException] = None
        self._closed = False
        self._close_lock = threading.Lock()
        self._loop = None
        self._serve_task = None

    def start(self, timeout: float = 10.0) -> None:
        if self._closed or self.thread is not None:
            raise NexusAgentError("MCP listener cannot be restarted; create a new Agent")

        def run():
            async def serve():
                self._loop = asyncio.get_running_loop()
                self._serve_task = asyncio.current_task()
                await self._server.serve(sockets=[self._socket])

            try:
                # The loop is owned by this thread. Explicit lifecycle also
                # supports Python 3.10 (before asyncio.Runner/loop_factory).
                loop = _listener_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(serve())
                finally:
                    try:
                        pending = asyncio.all_tasks(loop)
                        for task in pending:
                            task.cancel()
                        if pending:
                            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                        loop.run_until_complete(loop.shutdown_asyncgens())
                        loop.run_until_complete(loop.shutdown_default_executor())
                    finally:
                        asyncio.set_event_loop(None)
                        loop.close()
            except BaseException as exc:
                self._error = exc

        self.thread = threading.Thread(target=run, name="nexus-ipv6-mcp", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + timeout
        while self.thread.is_alive() and not self._server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not self.is_healthy():
            self.close()
            raise NexusAgentError("Native MCP listener did not start") from self._error

    def is_healthy(self) -> bool:
        return bool(not self._closed and self.thread and self.thread.is_alive()
                    and self._server.started and not self._server.should_exit)

    def close(self) -> None:
        with self._close_lock:
            self._close()

    def _close(self) -> None:
        if self._closed:
            return
        if self.thread is threading.current_thread():
            raise NexusAgentError("Close the MCP listener from outside its handler thread")

        def begin_shutdown():
            self._app.drain()
            self._server.should_exit = True

        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(begin_shutdown)
        else:
            self._server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive() and self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._serve_task.cancel)
                self.thread.join(timeout=2)
        self._socket.close()
        if self.thread is not None and self.thread.is_alive():
            raise NexusAgentError("MCP shutdown timed out; address lease has not been released")
        self._closed = True
