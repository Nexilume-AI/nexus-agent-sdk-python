"""High-level Agent-owned public IPv6 server without router registration."""

import ipaddress
import os
import re
import socket
import threading
import time
from typing import Any, Callable, Iterable, Optional, Union

from .errors import NexusAgentError
from .host_alias import HostAliasLease, LocalAddressdClient
from .models import AgentEnvelope, PublicAgentEndpoint
from .server import AgentRequestError, NexusAgentServer
from .server_auth import NoServerAuth, ServerAuthPolicy

BusinessHandler = Callable[[Any], Any]
BusinessStreamHandler = Callable[[Any], Iterable[Any]]
_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._~-]{0,93}[A-Za-z0-9])?$")


class PublicIPv6AgentHandle:
    """Running Agent-owned IPv6 listener."""

    def __init__(self, owner: "PublicIPv6Agent", thread: threading.Thread) -> None:
        self.owner = owner
        self.thread = thread
        self._closed = False
        self._lock = threading.Lock()

    def wait(self, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.thread.is_alive():
            native = self.owner._mcp_listener
            lease = self.owner.address_lease
            if not self._closed and ((native is not None and not native.is_healthy()) or
                                     (lease is not None and lease.last_error is not None)):
                self.close()
                raise NexusAgentError("Agent listener or IPv6 lease is no longer healthy")
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self.thread.join(0.1 if remaining is None else min(0.1, remaining))
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        # Stop native sessions before relinquishing the shared /128. If shutdown
        # fails, do not release an address that may still have a live listener.
        if self.owner._mcp_listener is not None:
            try:
                self.owner._mcp_listener.close()
            except BaseException:
                with self._lock:
                    self._closed = False  # Allow retry; retain the lease meanwhile.
                raise
        if self.thread.is_alive():
            self.owner.server.shutdown()
        self.thread.join(timeout=3.0)
        self.owner.server.server_close()
        if self.owner.address_lease is not None:
            self.owner.address_lease.close()
        self.owner._handle = None
        self.owner._closed = True

    def __enter__(self) -> "PublicIPv6AgentHandle":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


class PublicIPv6Agent:
    """Serve capabilities directly from a host-owned global IPv6 address."""

    def __init__(
        self,
        address: str,
        *,
        auth: Union[str, ServerAuthPolicy],
        port: int = 9443,
        tenant: str = "default",
        agent_id: Optional[str] = None,
        address_mode: str = "auto",
        allocator: Optional[LocalAddressdClient] = None,
        interface: str = "auto",
        prefix: str = "auto",
        address_lease_seconds: int = 300,
        cert_file: Optional[str] = None,
        key_file: Optional[str] = None,
        client_ca_file: Optional[str] = None,
        tls_server_name: Optional[str] = None,
        ca_bundle_id: Optional[str] = None,
        max_request_bytes: int = 65536,
        max_response_bytes: int = 262144,
        max_stream_event_bytes: int = 65536,
        request_timeout: float = 15.0,
        resumable_streams: bool = True,
        mcp: Optional[Any] = None,
        mcp_port: Optional[int] = None,
    ) -> None:
        if not _ID.fullmatch(tenant):
            raise ValueError("tenant must be a safe identifier up to 95 characters")
        agent_id = agent_id or self._default_agent_id()
        if not _ID.fullmatch(agent_id):
            raise ValueError("agent_id must be a safe identifier up to 95 characters")
        if address_mode not in ("auto", "existing", "host-alias"):
            raise ValueError("address_mode must be auto, existing or host-alias")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if mcp is None and mcp_port is not None:
            raise ValueError("mcp_port requires an MCP server")
        if mcp is not None:
            mcp_port = port + 1 if mcp_port is None else mcp_port
            if (isinstance(mcp_port, bool) or not isinstance(mcp_port, int) or
                    not 1 <= mcp_port <= 65535 or mcp_port == port):
                raise ValueError("mcp_port must be 1..65535 and different from the Invoke port")
        if auth == "none":
            auth_policy: ServerAuthPolicy = NoServerAuth()
        elif isinstance(auth, str):
            raise ValueError("auth must be 'none' or a configured ServerAuthPolicy")
        else:
            auth_policy = auth

        tls_enabled = bool(cert_file or key_file)
        if tls_enabled and not (cert_file and key_file):
            raise ValueError("TLS requires both cert_file and key_file")
        if tls_enabled and (not tls_server_name or not ca_bundle_id):
            raise ValueError(
                "TLS direct mode requires tls_server_name and ca_bundle_id"
            )
        if not tls_enabled and any((client_ca_file, tls_server_name, ca_bundle_id)):
            raise ValueError("plain HTTP direct mode does not accept TLS settings")

        self.address_lease: Optional[HostAliasLease] = None
        requested_address = str(address).strip()
        if requested_address == "auto":
            if address_mode == "existing":
                raise ValueError("address='auto' requires host-alias allocation")
            address_client = allocator or LocalAddressdClient()
            self.address_lease = address_client.allocate(
                tenant=tenant,
                agent_id=agent_id,
                interface=interface,
                prefix=prefix,
                lease_seconds=address_lease_seconds,
            )
            requested_address = self.address_lease.address
        elif address_mode == "host-alias":
            raise ValueError("host-alias mode requires address='auto'")

        text = requested_address.strip().strip("[]")
        try:
            if "%" in text:
                raise ValueError("public IPv6 address must not contain a scope ID")
            parsed = ipaddress.ip_address(text)
        except ValueError as exc:
            if self.address_lease is not None:
                self.address_lease.close()
            raise ValueError("address must be a global IPv6 address") from exc
        if not isinstance(parsed, ipaddress.IPv6Address) or not parsed.is_global:
            if self.address_lease is not None:
                self.address_lease.close()
            raise ValueError("address must be a global IPv6 address owned by this host")

        self.address = parsed.compressed
        self.tenant = tenant
        self.agent_id = agent_id
        self.origin = f"agent://{tenant}/{agent_id}"
        self.auth = auth_policy
        self._mcp_listener = None
        try:
            self.server = NexusAgentServer(
                self.address,
                port,
                path="/agent/v1/invoke",
                stream_path="/agent/v1/invoke-stream",
                auth=auth_policy,
                cert_file=cert_file,
                key_file=key_file,
                client_ca_file=client_ca_file,
                address_family="ipv6",
                dual_stack=False,
                max_request_bytes=max_request_bytes,
                max_response_bytes=max_response_bytes,
                max_stream_event_bytes=max_stream_event_bytes,
                request_timeout=request_timeout,
                resumable_streams=resumable_streams,
            )
            if mcp is not None:
                from .public_ipv6_mcp import NativeMCPListener

                self._mcp_listener = NativeMCPListener(
                    mcp, address=self.address, port=mcp_port, auth=auth_policy,
                    tenant=tenant, cert_file=cert_file, key_file=key_file,
                    client_ca_file=client_ca_file, tls_server_name=tls_server_name,
                    max_request_bytes=max_request_bytes, request_timeout=request_timeout,
                )
        except BaseException as exc:
            if hasattr(self, "server"):
                self.server.server_close()
            if self.address_lease is not None:
                self.address_lease.close()
            if not isinstance(exc, OSError):
                raise
            if os.name == "nt" and getattr(exc, "winerror", None) == 10013:
                detail = (
                    "Windows denied the port; check reserved ranges with "
                    "'netsh interface ipv6 show excludedportrange protocol=tcp'"
                )
            else:
                detail = (
                    "confirm the address belongs to this host and the port is unused"
                )
            raise NexusAgentError(
                f"cannot initialize IPv6 listener (Invoke {port}, MCP {mcp_port}); {detail}"
            ) from exc
        self.endpoint = PublicAgentEndpoint(
            address=self.address,
            port=self.server.port,
            scheme="https" if tls_enabled else "http",
            tls_server_name=tls_server_name,
            ca_bundle_id=ca_bundle_id,
        )
        self._handle: Optional[PublicIPv6AgentHandle] = None
        self._closed = False
        self._capabilities = set()

    @property
    def mcp_url(self) -> Optional[str]:
        """Native MCP URL; TLS uses the certificate's configured DNS identity."""
        if self._mcp_listener is None:
            return None
        host = self.endpoint.tls_server_name or self.address
        if ":" in host:
            host = f"[{host.strip('[]')}]"
        return f"{self.endpoint.scheme}://{host}:{self._mcp_listener.port}/mcp"

    @staticmethod
    def _default_agent_id() -> str:
        value = re.sub(r"[^A-Za-z0-9._~-]+", "-", socket.gethostname().lower())
        value = value.strip("-._~")[:95]
        return value or "python-agent"

    def capability(
        self,
        intent: str,
        *,
        pass_envelope: bool = False,
    ) -> Callable[[BusinessHandler], BusinessHandler]:
        """Expose a synchronous function on the direct IPv6 endpoint."""

        def decorate(function: BusinessHandler) -> BusinessHandler:
            def invoke(envelope: AgentEnvelope) -> Any:
                if envelope.target_agent not in (None, self.origin):
                    raise AgentRequestError(
                        404, "TARGET_AGENT_MISMATCH", "target Agent is not served here"
                    )
                return function(envelope if pass_envelope else envelope.payload)

            self.server.add_handler(intent, invoke)
            self._capabilities.add(intent)
            return function

        return decorate

    def stream_capability(
        self,
        intent: str,
        *,
        pass_envelope: bool = False,
    ) -> Callable[[BusinessStreamHandler], BusinessStreamHandler]:
        """Expose a streaming function on the direct IPv6 endpoint."""

        def decorate(function: BusinessStreamHandler) -> BusinessStreamHandler:
            def invoke(envelope: AgentEnvelope) -> Iterable[Any]:
                if envelope.target_agent not in (None, self.origin):
                    raise AgentRequestError(
                        404, "TARGET_AGENT_MISMATCH", "target Agent is not served here"
                    )
                return function(envelope if pass_envelope else envelope.payload)

            self.server.stream_handler(intent)(invoke)
            self._capabilities.add(intent)
            return function

        return decorate

    def start(
        self,
        *,
        announce: bool = True,
        print_fn: Callable[[str], Any] = print,
    ) -> PublicIPv6AgentHandle:
        if self._closed:
            raise NexusAgentError("Agent has been stopped; create a new instance")
        if self._handle is not None:
            raise NexusAgentError("Agent is already running")
        if not self._capabilities and self._mcp_listener is None:
            raise NexusAgentError("declare at least one capability before start()")
        thread = None
        try:
            if self._mcp_listener is not None:
                self._mcp_listener.start()
            thread = self.server.serve_in_thread(daemon=True)
            if not self.server.is_healthy():
                raise NexusAgentError("public IPv6 Agent listener did not start")
            if self.address_lease is not None:
                self.address_lease.confirm()
                self.address_lease.start_auto_renew()
        except BaseException:
            if self._mcp_listener is not None:
                self._mcp_listener.close()
            if thread is not None and thread.is_alive():
                self.server.shutdown()
                thread.join(timeout=3.0)
            self.server.server_close()
            if self.address_lease is not None:
                self.address_lease.close()
            self._closed = True
            raise
        handle = PublicIPv6AgentHandle(self, thread)
        self._handle = handle
        if announce:
            print_fn("Nexus public IPv6 Agent ready")
            print_fn(f"Agent: {self.origin}")
            print_fn(f"Endpoint: {self.endpoint.url}")
            if self.mcp_url is not None:
                print_fn(f"MCP: {self.mcp_url}")
            print_fn(f"Authentication: {self.auth.mode}")
        return handle

    def run(self, **start_options: Any) -> PublicAgentEndpoint:
        handle = self.start(**start_options)
        try:
            while not handle.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            handle.close()
            self._closed = True
        return self.endpoint
