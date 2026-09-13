"""High-level, batteries-included facade for a callable Nexus Agent."""

from __future__ import annotations

from dataclasses import MISSING, dataclass, fields, is_dataclass, replace
import hashlib
import ipaddress
import inspect
import os
import re
import socket
import ssl
import struct
import subprocess
import threading
import time
import types
import uuid
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Tuple,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)
from urllib.parse import urlsplit

from .client import AgentLease, NexusAgentClient
from .auth import (
    AutoTokenProvider,
    NoTokenProvider,
    StaticCloudTrustResolver,
    TokenProvider,
)
from .errors import NexusAgentError, NexusCloudRegistrationError
from .models import (
    AgentEnvelope,
    BackendTlsIdentity,
    CapabilityRegistration,
    CloudRegistrationManifest,
    CloudRegistrationStatus,
    ComputerRegistrationContract,
    MobileRegistrationContract,
    McpToolDescriptor,
    NexusExecutionProfile,
    PublicAgentEndpoint,
    SseEvent,
)
from .server import AgentRequestError, NexusAgentServer
from .reporting import (
    NexusRunContext,
    reset_current_run,
    set_current_run,
)


BusinessHandler = Callable[[Any], Any]
BusinessStreamHandler = Callable[[Any], Iterable[Any]]
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,94}$")
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*$"
)
_PEM_CERTIFICATE = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL
)
_PERMISSIVE_OBJECT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}
_LAN_SDK_PORT = 7446


def _clip_utf8(value: str, maximum: int) -> str:
    """Trim inferred text without cutting a UTF-8 code point."""

    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", errors="ignore").rstrip()


def _schema_for_annotation(annotation: Any, seen: Optional[set[Any]] = None) -> Mapping[str, Any]:
    """Infer a conservative JSON Schema for common Python input types."""

    if annotation in (Any, inspect.Signature.empty, None):
        return dict(_PERMISSIVE_OBJECT_SCHEMA)
    seen = set() if seen is None else seen
    try:
        if annotation in seen:
            return {}
        seen.add(annotation)
    except TypeError:
        pass

    model_schema = getattr(annotation, "model_json_schema", None)
    if callable(model_schema):
        value = model_schema()
        if isinstance(value, Mapping):
            return dict(value)

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in (Union, types.UnionType):
        non_null = [item for item in arguments if item is not type(None)]
        if len(non_null) == 1:
            return _schema_for_annotation(non_null[0], seen)
        return {"anyOf": [_schema_for_annotation(item, seen) for item in non_null]}
    if str(origin).endswith("Literal"):
        return {"enum": list(arguments)}
    if origin in (list, tuple, set, frozenset):
        item = arguments[0] if arguments else Any
        return {"type": "array", "items": _schema_for_annotation(item, seen)}
    if origin in (dict, Dict, Mapping) or (
        origin is not None and getattr(origin, "__name__", "") == "Mapping"
    ):
        value = arguments[1] if len(arguments) > 1 else Any
        return {
            "type": "object",
            "additionalProperties": _schema_for_annotation(value, seen),
        }
    primitive = {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        dict: dict(_PERMISSIVE_OBJECT_SCHEMA),
        list: {"type": "array", "items": {}},
    }
    if annotation in primitive:
        return primitive[annotation]

    if isinstance(annotation, type) and is_dataclass(annotation):
        properties = {}
        required = []
        for field in fields(annotation):
            properties[field.name] = _schema_for_annotation(field.type, seen.copy())
            if field.default is MISSING and field.default_factory is MISSING:
                required.append(field.name)
        value = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            value["required"] = required
        return value

    annotations = getattr(annotation, "__annotations__", None)
    if isinstance(annotations, Mapping):
        try:
            resolved = get_type_hints(annotation)
        except (NameError, TypeError):
            resolved = annotations
        properties = {
            str(name): _schema_for_annotation(value, seen.copy())
            for name, value in resolved.items()
        }
        required_keys = getattr(annotation, "__required_keys__", None)
        required = (
            sorted(str(item) for item in required_keys)
            if required_keys is not None
            else list(properties)
        )
        value: Dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            value["required"] = required
        return value

    return dict(_PERMISSIVE_OBJECT_SCHEMA)


def _infer_mcp_tool(
    intent: str,
    function: Callable[..., Any],
    *,
    pass_envelope: bool,
) -> McpToolDescriptor:
    description = inspect.getdoc(function) or intent
    parameters = [
        parameter
        for parameter in inspect.signature(function).parameters.values()
        if parameter.kind
        in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    annotation: Any = Any
    if parameters and not pass_envelope:
        try:
            annotation = get_type_hints(function).get(
                parameters[0].name, parameters[0].annotation
            )
        except (NameError, TypeError):
            annotation = parameters[0].annotation
    schema = _schema_for_annotation(annotation)
    if schema.get("type") != "object" and "anyOf" not in schema:
        schema = dict(_PERMISSIVE_OBJECT_SCHEMA)
    try:
        return McpToolDescriptor(
            name=intent,
            title=_clip_utf8(function.__name__, 127),
            description=_clip_utf8(description, 511),
            input_schema=schema,
        )
    except ValueError as exc:
        if "input_schema" in str(exc):
            raise ValueError(
                f"inferred MCP schema for {intent!r} is too large; "
                "pass an explicit simplified McpToolDescriptor"
            ) from exc
        raise


def _business_argument(envelope: AgentEnvelope, *, pass_envelope: bool) -> Any:
    if pass_envelope:
        return envelope
    if envelope.protocol != "mcp":
        return envelope.payload
    request = envelope.protocol_request
    params = request.get("params") if request is not None else None
    arguments = params.get("arguments") if isinstance(params, Mapping) else None
    if arguments is None:
        return {}
    if not isinstance(arguments, Mapping):
        raise AgentRequestError(400, "MCP tools/call arguments must be an object")
    return dict(arguments)


def _injects_run_context(function: Callable[..., Any]) -> bool:
    """Return whether the optional second handler parameter is a RunContext."""

    positional = [
        parameter
        for parameter in inspect.signature(function).parameters.values()
        if parameter.kind
        in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) < 2:
        return False
    annotation = positional[1].annotation
    if annotation is NexusRunContext:
        return True
    if isinstance(annotation, str):
        return annotation.rsplit(".", 1)[-1] == "NexusRunContext"
    return getattr(annotation, "__name__", "") == "NexusRunContext"


def _server_certificate_identity(
    cert_file: str,
    requested_name: str,
) -> Tuple[str, str]:
    """Return the verified DNS SAN and leaf SHA-256 for an HTTPS Agent."""

    try:
        decoded = ssl._ssl._test_decode_cert(cert_file)  # type: ignore[attr-defined]
        with open(cert_file, "r", encoding="ascii") as certificate:
            match = _PEM_CERTIFICATE.search(certificate.read())
    except (OSError, UnicodeError, ssl.SSLError, ValueError) as exc:
        raise ValueError("cert_file must contain a readable PEM certificate") from exc
    if match is None:
        raise ValueError("cert_file does not contain a PEM certificate")
    dns_names = tuple(
        str(value).lower()
        for kind, value in decoded.get("subjectAltName", ())
        if kind == "DNS" and isinstance(value, str)
    )
    if requested_name == "auto":
        requested_name = next(
            (name for name in dns_names if _HOSTNAME.fullmatch(name) and "." in name),
            "",
        )
        if not requested_name:
            raise ValueError(
                "Agent HTTPS certificate needs a lowercase dotted DNS SAN"
            )
    else:
        requested_name = requested_name.lower()
        if requested_name not in dns_names:
            raise ValueError("server_tls_name is not present in the certificate DNS SAN")
    der = ssl.PEM_cert_to_DER_cert(match.group(0))
    return requested_name, hashlib.sha256(der).hexdigest()


def _default_ipv4_gateway() -> Optional[str]:
    """Return the Linux default IPv4 gateway without an extra dependency."""

    try:
        with open("/proc/net/route", "r", encoding="ascii") as routes:
            next(routes, None)
            for line in routes:
                fields = line.split()
                if len(fields) < 4 or fields[1] != "00000000":
                    continue
                try:
                    flags = int(fields[3], 16)
                    packed = struct.pack("<I", int(fields[2], 16))
                except (ValueError, struct.error):
                    continue
                if flags & 0x2:
                    return socket.inet_ntoa(packed)
    except OSError:
        return None
    return None


def _windows_ipv4_router_candidates() -> Tuple[str, ...]:
    """Return bounded Router candidates from the Windows IPv4 route table.

    Agent Serving hosts are often attached to a dedicated Router LAN without a
    default gateway.  In that layout the useful evidence is the on-link subnet,
    so test its conventional first and last usable addresses as well as any
    configured default gateways.  TCP probing in ``resolve_router_url`` keeps a
    normal Internet gateway from being selected just because it is present.
    """

    if os.name != "nt":
        return ()
    try:
        completed = subprocess.run(
            ["route", "print", "-4"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=2.0,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if completed.returncode != 0:
        return ()

    on_link = []
    gateways = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 5:
            continue
        try:
            destination = ipaddress.IPv4Address(fields[0])
            netmask = ipaddress.IPv4Address(fields[1])
            interface = ipaddress.IPv4Address(fields[3])
            int(fields[4])
            network = ipaddress.IPv4Network(
                f"{destination}/{netmask}", strict=False
            )
        except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError):
            continue

        try:
            gateway = ipaddress.IPv4Address(fields[2])
        except ipaddress.AddressValueError:
            gateway = None

        if destination == ipaddress.IPv4Address("0.0.0.0") and int(netmask) == 0:
            if gateway is not None and not (
                gateway.is_unspecified or gateway.is_loopback or gateway.is_multicast
            ):
                gateways.append(str(gateway))
            continue

        if (
            gateway is not None
            and not gateway.is_unspecified
            or destination != network.network_address
            or interface not in network
            or network.prefixlen >= 31
            or not network.is_private
        ):
            continue
        for address in (network.network_address + 1, network.broadcast_address - 1):
            if address != interface:
                on_link.append(str(address))

    ordered = []
    seen = set()
    for candidate in on_link + gateways:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
        if len(ordered) >= 16:
            break
    return tuple(ordered)


def _first_reachable_windows_router() -> Optional[str]:
    for candidate in _windows_ipv4_router_candidates():
        connection = None
        try:
            connection = socket.create_connection(
                (candidate, _LAN_SDK_PORT), timeout=0.25
            )
        except OSError:
            continue
        finally:
            if connection is not None:
                connection.close()
        return candidate
    return None


def resolve_router_url(router: str = "auto") -> str:
    """Resolve the local Agent Access Proxy URL with bounded zero config.

    Resolution order is explicit URL, ``NEXUS_ROUTER_URL``, the resolvable
    ``nexus-router.local`` name, the Linux default IPv4 gateway, then a
    reachable Router on a Windows on-link IPv4 subnet.
    """

    if not isinstance(router, str) or not router:
        raise ValueError("router must be 'auto' or an absolute http(s) URL")
    if router != "auto":
        value = router
    else:
        value = os.environ.get("NEXUS_ROUTER_URL", "").strip()
        if not value:
            try:
                socket.getaddrinfo(
                    "nexus-router.local", _LAN_SDK_PORT, 0, socket.SOCK_STREAM
                )
            except OSError:
                gateway = _default_ipv4_gateway()
                if gateway is None:
                    gateway = _first_reachable_windows_router()
                if gateway is None:
                    raise NexusAgentError(
                        "Router auto-discovery failed; set NEXUS_ROUTER_URL "
                        f"or pass router='http://[router-ipv6]:{_LAN_SDK_PORT}'"
                    )
                value = f"http://{gateway}:{_LAN_SDK_PORT}"
            else:
                value = f"http://nexus-router.local:{_LAN_SDK_PORT}"
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("router must resolve to an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("router URL must not contain credentials, query or fragment")
    return value.rstrip("/")


def _candidate_host_addresses() -> Iterator[
    Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
]:
    seen = set()
    try:
        with open("/proc/net/if_inet6", "r", encoding="ascii") as addresses:
            for line in addresses:
                fields = line.split()
                if len(fields) != 6:
                    continue
                try:
                    address = ipaddress.IPv6Address(int(fields[0], 16))
                except ValueError:
                    continue
                if (
                    address in seen
                    or address.is_unspecified
                    or address.is_loopback
                    or address.is_link_local
                    or address.is_multicast
                ):
                    continue
                seen.add(address)
                yield address
    except OSError:
        pass
    try:
        values = socket.getaddrinfo(
            socket.gethostname(), 0, socket.AF_INET6, socket.SOCK_STREAM
        )
    except OSError:
        values = []
    for value in values:
        try:
            address = ipaddress.IPv6Address(value[4][0].split("%", 1)[0])
        except (IndexError, ValueError):
            continue
        if (
            address not in seen
            and not address.is_unspecified
            and not address.is_loopback
            and not address.is_link_local
            and not address.is_multicast
        ):
            seen.add(address)
            yield address
    try:
        values = socket.getaddrinfo(
            socket.gethostname(), 0, socket.AF_INET, socket.SOCK_STREAM
        )
    except OSError:
        values = []
    for value in values:
        try:
            address = ipaddress.IPv4Address(value[4][0])
        except (IndexError, ValueError):
            continue
        if (
            address not in seen
            and not address.is_unspecified
            and not address.is_loopback
            and not address.is_link_local
            and not address.is_multicast
        ):
            seen.add(address)
            yield address


def _router_selected_address(
    router_url: Optional[str],
) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    """Return the local source address selected by the route to the router."""

    if not router_url:
        return None
    parsed = urlsplit(router_url)
    if parsed.hostname is None:
        return None
    try:
        endpoints = socket.getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            0,
            socket.SOCK_DGRAM,
        )
    except OSError:
        return None
    for family, socktype, protocol, _canonical, endpoint in endpoints:
        route_socket = None
        try:
            route_socket = socket.socket(family, socktype, protocol)
            route_socket.connect(endpoint)
            selected = ipaddress.ip_address(
                route_socket.getsockname()[0].split("%", 1)[0]
            )
        except (OSError, ValueError, IndexError):
            continue
        finally:
            if route_socket is not None:
                route_socket.close()
        if (
            not selected.is_unspecified
            and not selected.is_loopback
            and not selected.is_link_local
            and not selected.is_multicast
        ):
            return selected
    return None


def resolve_advertise_address(
    value: str = "auto", *, router_url: Optional[str] = None
) -> str:
    """Resolve the address placed in the router's backend endpoint."""

    if not isinstance(value, str) or not value:
        raise ValueError("advertise_address must be 'auto' or an IP/hostname")
    if value == "auto":
        configured = os.environ.get("NEXUS_AGENT_ADDRESS", "").strip()
        if not configured:
            configured = os.environ.get("NEXUS_AGENT_IPV6", "").strip()
        if configured:
            value = configured
        else:
            routed = _router_selected_address(router_url)
            if routed is not None:
                value = str(routed)
            else:
                candidates = tuple(_candidate_host_addresses())
                if not candidates:
                    raise NexusAgentError(
                        "Agent address auto-detection failed; set NEXUS_AGENT_ADDRESS "
                        "or pass advertise_address='fd00::20'"
                    )
                # Prefer private LAN addresses because normal Agent registration is
                # consumed by the router's automatic LAN callback path. A global
                # IPv6 address remains available when the host has no private/ULA
                # address; explicit configuration always wins.
                def priority(
                    item: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
                ) -> int:
                    if item.is_private and item.version == 6:
                        return 0
                    if item.is_private and item.version == 4:
                        return 1
                    if item.is_global and item.version == 6:
                        return 2
                    return 3

                value = str(sorted(candidates, key=priority)[0])
    text = value.strip().strip("[]")
    if "%" in text:
        raise ValueError("scoped IPv6 addresses cannot be advertised")
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        if not _HOSTNAME.fullmatch(text):
            raise ValueError("advertise_address must be an IP address or hostname")
        return text.lower()
    if address.is_unspecified or address.is_multicast or address.is_loopback:
        raise ValueError("advertise_address must identify a reachable Agent host")
    return address.compressed


def _url_host(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    return f"[{address.compressed}]" if address.version == 6 else address.compressed


def _default_agent_id() -> str:
    value = re.sub(r"[^A-Za-z0-9._~-]+", "-", socket.gethostname().lower())
    value = value.strip("-._~")[:95]
    return value or "python-agent"


@dataclass(frozen=True)
class PublishedCapability:
    """One registered capability and its router-managed public address."""

    intent: str
    origin: str
    backend_endpoint: str
    route_id: str
    public_ipv6: Optional[str]
    public_endpoint: Optional[PublicAgentEndpoint]

    @property
    def public_url(self) -> Optional[str]:
        return self.public_endpoint.url if self.public_endpoint is not None else None


@dataclass(frozen=True)
class _CapabilitySpec:
    intent: str
    origin: str
    version: int
    region: str
    lease_seconds: int
    public_ipv6: Optional[bool]
    cost_microunits: int
    latency_ms: int
    trust: int
    tool: Optional[McpToolDescriptor] = None


class NexusAgentHandle:
    """Running high-level Agent; closes leases and listener as one unit."""

    def __init__(
        self,
        owner: "NexusAgent",
        thread: threading.Thread,
        leases: Tuple[AgentLease, ...],
        published: Tuple[PublishedCapability, ...],
    ) -> None:
        self.owner = owner
        self.thread = thread
        self.leases = leases
        self.published = published
        self._closed = False
        self._lock = threading.Lock()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Wait for the listener; return ``True`` if it has stopped."""

        self.thread.join(timeout)
        return not self.thread.is_alive()

    def cloud_status(self) -> CloudRegistrationStatus:
        """Return this Agent's router-managed Nexus Cloud state."""

        return self.owner.client.cloud_status(
            tenant=self.owner.tenant,
            origin=self.owner._cloud_origin(),
        )

    def wait_for_cloud(
        self,
        timeout: float = 60.0,
        *,
        poll_interval: float = 1.0,
    ) -> CloudRegistrationStatus:
        """Wait for managed Cloud publication without affecting the LAN lease."""

        if timeout < 0:
            raise ValueError("timeout must not be negative")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        deadline = time.monotonic() + timeout
        while True:
            status = self.cloud_status()
            if status.ready:
                return status
            if status.state in {"rejected", "suppressed", "disabled", "unsupported"}:
                raise NexusCloudRegistrationError(
                    status.state,
                    status.message or "router cannot publish this Agent to Nexus Cloud",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Cloud Agent registration remained {status.state!r}: "
                    f"{status.message or 'no additional router status'}"
                )
            time.sleep(min(poll_interval, remaining))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for lease in reversed(self.leases):
            try:
                lease.close()
            except Exception:
                pass
        if self.thread.is_alive():
            self.owner.server.shutdown()
        self.thread.join(timeout=3.0)
        self.owner.server.server_close()
        self.owner._handle = None
        self.owner._closed = True

    def __enter__(self) -> "NexusAgentHandle":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


class NexusAgent:
    """Friendly facade that turns decorated Python functions into capabilities."""

    @classmethod
    def public_ipv6(cls, address: str, **options: Any):
        """Create an Agent-owned public IPv6 server without router registration."""

        from .public_ipv6_agent import PublicIPv6Agent

        return PublicIPv6Agent(address, **options)

    def __init__(
        self,
        *,
        router: str = "auto",
        token: Optional[str] = None,
        token_provider: Optional[TokenProvider] = None,
        auth: Optional[Union[str, TokenProvider]] = None,
        transaction_token: Optional[str] = None,
        tenant: str = "default",
        agent_id: Optional[str] = None,
        listen_host: str = "auto",
        port: int = 0,
        advertise_address: str = "auto",
        path: str = "/invoke",
        router_ca_file: Optional[str] = None,
        auth_ca_file: Optional[str] = None,
        cloud_ca_file: Optional[str] = None,
        router_cert_file: Optional[str] = None,
        router_key_file: Optional[str] = None,
        router_tls_server_name: Optional[str] = None,
        cert_file: Optional[str] = None,
        key_file: Optional[str] = None,
        client_ca_file: Optional[str] = None,
        server_tls_name: str = "auto",
        server_ca_bundle_id: str = "system",
        lease_seconds: int = 300,
        timeout: float = 10.0,
        cloud_publish: bool = True,
        cloud_name: Optional[str] = None,
        computer_requirement: str = "disabled",
        workspace_capabilities: Iterable[str] = (),
        mobile_requirement: str = "disabled",
        mobile_capabilities: Iterable[str] = (),
        runtime: str = "auto",
    ) -> None:
        if not _ID.fullmatch(tenant):
            raise ValueError("tenant must be a safe identifier up to 95 characters")
        agent_id = agent_id or _default_agent_id()
        if not _ID.fullmatch(agent_id):
            raise ValueError("agent_id must be a safe identifier up to 95 characters")
        if not 5 <= lease_seconds <= 86400:
            raise ValueError("lease_seconds must be between 5 and 86400")
        # The trusted Docker launcher selects hosted mode before importing user
        # code. Never infer hosting from request arguments or a failed discovery.
        self.runtime_mode = os.environ.get("NEXUS_AGENT_RUNTIME_MODE", "openwrt") if runtime == "auto" else runtime
        if self.runtime_mode not in {"openwrt", "hosted"}:
            raise ValueError("runtime must be auto, openwrt or hosted")
        if self.runtime_mode == "hosted":
            from .hosted import HostedCapabilityRegistry
            self.tenant = tenant
            self.agent_id = agent_id
            self.origin = f"agent://{tenant}/{agent_id}"
            if not isinstance(cloud_publish, bool):
                raise ValueError("cloud_publish must be a boolean")
            if cloud_name is not None and (not isinstance(cloud_name, str) or not cloud_name.strip() or len(cloud_name.encode("utf-8")) > 255):
                raise ValueError("cloud_name must be 1-255 UTF-8 bytes")
            self.cloud_publish = cloud_publish
            self.cloud_name = cloud_name.strip() if cloud_name is not None else agent_id
            self.computer = ComputerRegistrationContract(computer_requirement, tuple(workspace_capabilities))
            self.mobile = MobileRegistrationContract(mobile_requirement, tuple(mobile_capabilities))
            self.default_lease_seconds = lease_seconds
            self.server = HostedCapabilityRegistry()
            self._specs = {}
            self._handle = None
            self._closed = False
            self._hosted_mcp = None
            return
        self.router_url = resolve_router_url(router)
        if auth is not None:
            if token_provider is not None:
                raise ValueError("auth and token_provider are mutually exclusive")
            if auth == "none":
                token_provider = NoTokenProvider()
            elif auth == "auto":
                token_provider = AutoTokenProvider(
                    self.router_url,
                    timeout=timeout,
                    router_ca_file=router_ca_file,
                    issuer_ca_file=auth_ca_file,
                )
            elif isinstance(auth, str):
                raise ValueError("auth must be 'none', 'auto' or a TokenProvider")
            else:
                token_provider = auth
        elif token is None and token_provider is None and transaction_token is None:
            token_provider = AutoTokenProvider(
                self.router_url,
                timeout=timeout,
                router_ca_file=router_ca_file,
                issuer_ca_file=auth_ca_file,
            )
        if sum(value is not None for value in (token, token_provider, transaction_token)) > 1:
            raise ValueError(
                "token, token_provider and transaction_token are mutually exclusive"
            )
        self.tenant = tenant
        self.agent_id = agent_id
        self.origin = f"agent://{tenant}/{agent_id}"
        if not isinstance(cloud_publish, bool):
            raise ValueError("cloud_publish must be a boolean")
        if cloud_name is not None and (
            not isinstance(cloud_name, str)
            or not cloud_name.strip()
            or len(cloud_name.encode("utf-8")) > 255
        ):
            raise ValueError("cloud_name must be 1-255 UTF-8 bytes")
        self.cloud_publish = cloud_publish
        self.cloud_name = cloud_name.strip() if cloud_name is not None else agent_id
        self.computer = ComputerRegistrationContract(
            requirement=computer_requirement,
            workspace_capabilities=tuple(workspace_capabilities),
        )
        self.mobile = MobileRegistrationContract(
            requirement=mobile_requirement,
            mobile_capabilities=tuple(mobile_capabilities),
        )
        self.advertise_address = resolve_advertise_address(
            advertise_address, router_url=self.router_url
        )
        if listen_host == "auto":
            listen_host = "::" if ":" in self.advertise_address else "0.0.0.0"
        self.default_lease_seconds = lease_seconds
        cloud_trust_resolver = (
            StaticCloudTrustResolver(cloud_ca_file)
            if cloud_ca_file is not None
            else token_provider
        )
        run_context_opener = getattr(
            cloud_trust_resolver, "open_cloud_request", None
        )
        self.server = NexusAgentServer(
            listen_host,
            port,
            path=path,
            cert_file=cert_file,
            key_file=key_file,
            client_ca_file=client_ca_file,
            address_family="ipv6" if ":" in listen_host else "ipv4",
            dual_stack=False,
            run_context_opener=(
                run_context_opener if callable(run_context_opener) else None
            ),
        )
        self.backend_tls: Optional[BackendTlsIdentity] = None
        if self.server.tls_enabled:
            assert cert_file is not None
            tls_name, fingerprint = _server_certificate_identity(
                cert_file, server_tls_name
            )
            self.backend_tls = BackendTlsIdentity(
                address=self.advertise_address,
                port=self.server.port,
                tls_server_name=tls_name,
                ca_bundle_id=server_ca_bundle_id,
                certificate_sha256=fingerprint,
            )
        self.client = NexusAgentClient(
            self.router_url,
            token=token,
            token_provider=token_provider,
            transaction_token=transaction_token,
            ca_file=router_ca_file,
            cert_file=router_cert_file,
            key_file=router_key_file,
            tls_server_name=router_tls_server_name,
            timeout=timeout,
        )
        self._specs: Dict[str, _CapabilitySpec] = {}
        self._handle: Optional[NexusAgentHandle] = None
        self._closed = False

    def _cloud_origin(self) -> str:
        origins = {
            spec.origin for spec in self._specs.values() if spec.tool is not None
        }
        if not origins:
            return self.origin
        if len(origins) != 1:
            raise NexusAgentError(
                "one auto-authenticated NexusAgent can publish only one Cloud origin"
            )
        return next(iter(origins))

    @property
    def backend_endpoint(self) -> str:
        if self.runtime_mode == "hosted":
            raise NexusAgentError("Hosted Agents expose MCP through the container runtime, not an edge endpoint")
        if self.backend_tls is not None:
            return (
                f"https://{self.backend_tls.tls_server_name}:"
                f"{self.server.port}{self.server.path}"
            )
        scheme = "http"
        return (
            f"{scheme}://{_url_host(self.advertise_address)}:"
            f"{self.server.port}{self.server.path}"
        )

    def _spec(
        self,
        intent: str,
        *,
        public_ipv6: Optional[bool],
        origin: Optional[str],
        version: int,
        region: str,
        lease_seconds: Optional[int],
        cost_microunits: int,
        latency_ms: int,
        trust: int,
    ) -> _CapabilitySpec:
        if getattr(self, "_hosted_mcp", None) is not None:
            raise NexusAgentError("Declare all capabilities before exporting the MCP server")
        if self._handle is not None or self._closed:
            raise NexusAgentError("capabilities must be declared before start()")
        value = _CapabilitySpec(
            intent=intent,
            origin=origin or self.origin,
            version=version,
            region=region,
            lease_seconds=(
                lease_seconds
                if lease_seconds is not None
                else self.default_lease_seconds
            ),
            public_ipv6=public_ipv6,
            cost_microunits=cost_microunits,
            latency_ms=latency_ms,
            trust=trust,
        )
        current = self._specs.get(intent)
        if current is not None and replace(current, tool=None) != value:
            raise ValueError(f"capability {intent!r} was declared with different options")
        if current is not None:
            return current
        self._specs[intent] = value
        return value

    def capability(
        self,
        intent: str,
        *,
        public_ipv6: Optional[bool] = None,
        origin: Optional[str] = None,
        version: int = 1,
        region: str = "local",
        lease_seconds: Optional[int] = None,
        cost_microunits: int = 0,
        latency_ms: int = 0,
        trust: int = 50,
        pass_envelope: bool = False,
        tool: Optional[Union[McpToolDescriptor, bool]] = None,
        mobile_scopes: Optional[Iterable[str]] = None,
        slash_command: Optional[str] = None,
        slash_description: str = "",
        execution_profiles: Optional[Iterable[NexusExecutionProfile]] = None,
        input_modalities: Optional[Iterable[str]] = None,
        follow_up: Optional[str] = None,
    ) -> Callable[[BusinessHandler], BusinessHandler]:
        """Decorate a sync/async handler; opt in to cooperative Cloud follow-up."""

        if follow_up is not None and follow_up not in {"none", "queue", "steer_and_queue"}:
            raise ValueError("follow_up must be none, queue or steer_and_queue")

        if public_ipv6 is not None and not isinstance(public_ipv6, bool):
            raise ValueError("public_ipv6 must be True, False or None")
        self._spec(
            intent,
            public_ipv6=public_ipv6,
            origin=origin,
            version=version,
            region=region,
            lease_seconds=lease_seconds,
            cost_microunits=cost_microunits,
            latency_ms=latency_ms,
            trust=trust,
        )

        def decorate(function: BusinessHandler) -> BusinessHandler:
            if tool is not None and tool is not False and not isinstance(
                tool, McpToolDescriptor
            ):
                raise ValueError("tool must be None, False or an McpToolDescriptor")
            descriptor = (
                None
                if not self.cloud_publish or tool is False
                else tool
                if isinstance(tool, McpToolDescriptor)
                else _infer_mcp_tool(intent, function, pass_envelope=pass_envelope)
            )
            current = self._specs[intent]
            if tool is None and current.tool is not None:
                descriptor = current.tool
            if descriptor is not None and mobile_scopes is not None:
                descriptor = replace(descriptor, mobile_scopes=tuple(mobile_scopes))
            if descriptor is not None and any(
                value is not None
                for value in (slash_command, execution_profiles, input_modalities)
            ):
                descriptor = replace(
                    descriptor,
                    slash_command=slash_command,
                    slash_description=slash_description or None,
                    execution_profiles=tuple(execution_profiles or ()),
                    input_modalities=tuple(input_modalities or descriptor.input_modalities),
                )
            if descriptor is not None and (
                set(descriptor.mobile_scopes) - set(self.mobile.mobile_capabilities)
            ):
                raise ValueError(
                    "MCP tool mobile_scopes must be declared by mobile_capabilities"
                )
            self._specs[intent] = replace(current, tool=descriptor)
            inject_context = _injects_run_context(function)

            def invoke(envelope: AgentEnvelope) -> Any:
                context = envelope.run_context or NexusRunContext()
                argument = _business_argument(envelope, pass_envelope=pass_envelope)
                if inspect.iscoroutinefunction(function):
                    async def invoke_async() -> Any:
                        token = set_current_run(context)
                        try:
                            if follow_up is not None and context.run_id:
                                await context.aio.inbox.configure(follow_up)
                            if inject_context:
                                return await function(argument, context)
                            return await function(argument)
                        finally:
                            context.inbox.close()
                            reset_current_run(token)

                    return invoke_async()
                token = set_current_run(context)
                try:
                    if follow_up is not None and context.run_id:
                        context.inbox.configure(follow_up)
                    if inject_context:
                        return function(argument, context)
                    return function(argument)
                finally:
                    context.inbox.close()
                    reset_current_run(token)

            self.server.add_handler(intent, invoke)
            return function

        return decorate

    def stream_capability(
        self,
        intent: str,
        *,
        public_ipv6: Optional[bool] = None,
        origin: Optional[str] = None,
        version: int = 1,
        region: str = "local",
        lease_seconds: Optional[int] = None,
        cost_microunits: int = 0,
        latency_ms: int = 0,
        trust: int = 50,
        pass_envelope: bool = False,
        tool: Optional[Union[McpToolDescriptor, bool]] = None,
        mobile_scopes: Optional[Iterable[str]] = None,
        slash_command: Optional[str] = None,
        slash_description: str = "",
        execution_profiles: Optional[Iterable[NexusExecutionProfile]] = None,
        input_modalities: Optional[Iterable[str]] = None,
    ) -> Callable[[BusinessStreamHandler], BusinessStreamHandler]:
        """Decorate a generator as an SSE capability for the same intent."""

        if public_ipv6 is not None and not isinstance(public_ipv6, bool):
            raise ValueError("public_ipv6 must be True, False or None")
        self._spec(
            intent,
            public_ipv6=public_ipv6,
            origin=origin,
            version=version,
            region=region,
            lease_seconds=lease_seconds,
            cost_microunits=cost_microunits,
            latency_ms=latency_ms,
            trust=trust,
        )

        def decorate(function: BusinessStreamHandler) -> BusinessStreamHandler:
            if tool is not None and tool is not False and not isinstance(
                tool, McpToolDescriptor
            ):
                raise ValueError("tool must be None, False or an McpToolDescriptor")
            descriptor = (
                None
                if not self.cloud_publish or tool is False
                else tool
                if isinstance(tool, McpToolDescriptor)
                else _infer_mcp_tool(intent, function, pass_envelope=pass_envelope)
            )
            current = self._specs[intent]
            if tool is None and current.tool is not None:
                descriptor = current.tool
            if descriptor is not None and mobile_scopes is not None:
                descriptor = replace(descriptor, mobile_scopes=tuple(mobile_scopes))
            if descriptor is not None and any(
                value is not None
                for value in (slash_command, execution_profiles, input_modalities)
            ):
                descriptor = replace(
                    descriptor,
                    slash_command=slash_command,
                    slash_description=slash_description or None,
                    execution_profiles=tuple(execution_profiles or ()),
                    input_modalities=tuple(input_modalities or descriptor.input_modalities),
                )
            if descriptor is not None and (
                set(descriptor.mobile_scopes) - set(self.mobile.mobile_capabilities)
            ):
                raise ValueError(
                    "MCP tool mobile_scopes must be declared by mobile_capabilities"
                )
            self._specs[intent] = replace(current, tool=descriptor)
            inject_context = _injects_run_context(function)

            def invoke(envelope: AgentEnvelope) -> Iterable[Any]:
                context = envelope.run_context or NexusRunContext()
                argument = _business_argument(envelope, pass_envelope=pass_envelope)

                if inspect.isasyncgenfunction(function) or inspect.iscoroutinefunction(function):
                    async def generate_async():
                        token = set_current_run(context)
                        try:
                            items = (
                                function(argument, context)
                                if inject_context
                                else function(argument)
                            )
                            if inspect.isawaitable(items):
                                items = await items
                            if hasattr(items, "__aiter__"):
                                async for item in items:
                                    yield item
                            else:
                                for item in items:
                                    yield item
                        finally:
                            reset_current_run(token)

                    return generate_async()

                def generate() -> Iterator[Any]:
                    token = set_current_run(context)
                    try:
                        items = (
                            function(argument, context)
                            if inject_context
                            else function(argument)
                        )
                        yield from items
                    finally:
                        reset_current_run(token)

                return generate()

            self.server.stream_handler(intent)(invoke)
            return function

        return decorate

    def registrations(self) -> Tuple[CapabilityRegistration, ...]:
        if self.runtime_mode == "hosted":
            raise NexusAgentError("Hosted Agents do not register with an OpenWrt Router")
        if not self._specs:
            raise NexusAgentError("declare at least one @agent.capability before start()")
        endpoint = self.backend_endpoint
        return tuple(
            CapabilityRegistration(
                intent=spec.intent,
                origin=spec.origin,
                endpoint=endpoint,
                tenant=self.tenant,
                version=spec.version,
                region=spec.region,
                lease_seconds=spec.lease_seconds,
                cost_microunits=spec.cost_microunits,
                latency_ms=spec.latency_ms,
                trust=spec.trust,
                public_ipv6="auto" if spec.public_ipv6 is not False else None,
                backend_tls=self.backend_tls,
                cloud=(
                    CloudRegistrationManifest(
                        publish=True,
                        agent_name=self.cloud_name,
                        tool=spec.tool,
                        computer=(None if self.computer.is_default else self.computer),
                        mobile=(None if self.mobile.is_default else self.mobile),
                    )
                    if spec.tool is not None
                    else None
                ),
            )
            for spec in self._specs.values()
        )

    def invoke(
        self,
        intent: str,
        payload: Any,
        *,
        target_agent: Optional[str] = None,
        intent_version: int = 1,
        task_id: Optional[str] = None,
        hop_limit: int = 8,
        constraints: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        """Invoke another Agent using this Agent's identity by default."""

        self._require_router_client()
        return self.client.invoke_intent(
            intent,
            payload,
            tenant=self.tenant,
            source_agent=self.origin,
            target_agent=target_agent,
            intent_version=intent_version,
            task_id=task_id,
            hop_limit=hop_limit,
            constraints=constraints,
        )

    def _require_router_client(self) -> None:
        if self.runtime_mode == "hosted":
            raise NexusAgentError("Router-to-Router calls require an OpenWrt runtime; use Nexus Run APIs in hosted tools")

    def _outbound_envelope(
        self,
        intent: str,
        payload: Any,
        *,
        target_agent: Optional[str],
        intent_version: int,
        task_id: Optional[str],
        hop_limit: int,
        constraints: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "version": "1.0",
            "intent": intent,
            "intent_version": intent_version,
            "task_id": task_id or str(uuid.uuid4()),
            "source_agent": self.origin,
            "tenant": self.tenant,
            "hop_limit": hop_limit,
            "constraints": dict(constraints or {}),
            "payload": payload,
        }
        if target_agent is not None:
            value["target_agent"] = target_agent
        return value

    def invoke_async(
        self,
        intent: str,
        payload: Any,
        *,
        target_agent: Optional[str] = None,
        intent_version: int = 1,
        task_id: Optional[str] = None,
        hop_limit: int = 8,
        constraints: Optional[Mapping[str, Any]] = None,
    ):
        """Start an in-network Direct Task and return its private handle."""

        self._require_router_client()
        return self.client.invoke_async(self._outbound_envelope(
            intent,
            payload,
            target_agent=target_agent,
            intent_version=intent_version,
            task_id=task_id,
            hop_limit=hop_limit,
            constraints=constraints,
        ))

    def invoke_interactive(
        self,
        intent: str,
        payload: Any,
        *,
        target_agent: Optional[str] = None,
        intent_version: int = 1,
        task_id: Optional[str] = None,
        hop_limit: int = 8,
        constraints: Optional[Mapping[str, Any]] = None,
    ):
        """Stream a Direct Invoke and allow replies to Chat interactions."""

        self._require_router_client()
        return self.client.invoke_interactive(self._outbound_envelope(
            intent,
            payload,
            target_agent=target_agent,
            intent_version=intent_version,
            task_id=task_id,
            hop_limit=hop_limit,
            constraints=constraints,
        ))

    def invoke_stream(
        self,
        intent: str,
        payload: Any,
        *,
        target_agent: Optional[str] = None,
        intent_version: int = 1,
        task_id: Optional[str] = None,
        hop_limit: int = 8,
        constraints: Optional[Mapping[str, Any]] = None,
        resume: bool = True,
        last_event_id: int = 0,
        max_reconnects: int = 3,
        reconnect_delay: float = 0.25,
    ) -> Iterator[SseEvent]:
        """Stream another Agent and automatically preserve caller identity."""

        self._require_router_client()
        envelope = self._outbound_envelope(
            intent,
            payload,
            target_agent=target_agent,
            intent_version=intent_version,
            task_id=task_id,
            hop_limit=hop_limit,
            constraints=constraints,
        )
        return self.client.invoke_stream(
            envelope,
            resume=resume,
            last_event_id=last_event_id,
            max_reconnects=max_reconnects,
            reconnect_delay=reconnect_delay,
        )

    def start(
        self,
        *,
        auto_renew: bool = True,
        renew_fraction: float = 0.6,
        announce: bool = True,
        print_fn: Callable[[str], Any] = print,
    ) -> NexusAgentHandle:
        """Start listening, register all capabilities, and return a handle."""

        if self.runtime_mode == "hosted":
            raise NexusAgentError("Use agent.run() or agent.as_mcp_server() in hosted mode")
        if self._closed:
            raise NexusAgentError(
                "Agent has been stopped; create a new NexusAgent instance to restart"
            )
        if self._handle is not None:
            raise NexusAgentError("Agent is already running")
        automatic_specs = [
            intent for intent, spec in self._specs.items()
            if spec.public_ipv6 is None
        ]
        metadata = None
        if automatic_specs or (
            self.cloud_publish
            and (not self.computer.is_default or not self.mobile.is_default)
        ):
            metadata = self.client.router_auth_metadata(
                tenant=self.tenant, origin=self._cloud_origin()
            )
            cloud = metadata.cloud if metadata is not None else None
            if (
                self.cloud_publish
                and not self.computer.is_default
                and cloud is not None
                and cloud.manifest_schema_version < 2
            ):
                raise NexusAgentError(
                    "CLOUD_COMPUTER_CONTRACT_UNSUPPORTED: router Cloud manifest schema v2 is required"
                )
            if (
                self.cloud_publish
                and not self.mobile.is_default
                and cloud is not None
                and cloud.manifest_schema_version < 3
            ):
                raise NexusAgentError(
                    "CLOUD_MOBILE_CONTRACT_UNSUPPORTED: router Cloud manifest schema v3 is required"
                )
            if (
                self.cloud_publish
                and "browser.control" in self.computer.workspace_capabilities
                and cloud is not None
                and cloud.manifest_schema_version < 4
            ):
                raise NexusAgentError(
                    "CLOUD_BROWSER_CONTRACT_UNSUPPORTED: router Cloud manifest schema v4 is required"
                )
        if automatic_specs:
            cloud = metadata.cloud if metadata is not None else None
            request_public_ipv6 = (
                True if cloud is None else cloud.transports.direct_ipv6
            )
            for intent in automatic_specs:
                self._specs[intent] = replace(
                    self._specs[intent], public_ipv6=request_public_ipv6
                )
        registrations = self.registrations()
        thread = self.server.serve_in_thread(daemon=True)
        if not self.server.is_healthy():
            self.server.server_close()
            self._closed = True
            raise NexusAgentError("Agent listener did not start")
        leases = []
        try:
            for registration in registrations:
                leases.append(self.client.register(
                    registration,
                    auto_renew=auto_renew,
                    renew_fraction=renew_fraction,
                    health_check=self.server.is_healthy,
                    reregister_on_not_found=True,
                ))
        except BaseException:
            for lease in reversed(leases):
                try:
                    lease.close()
                except Exception:
                    pass
            if thread.is_alive():
                self.server.shutdown()
            thread.join(timeout=3.0)
            self.server.server_close()
            self._closed = True
            raise
        published = tuple(
            PublishedCapability(
                intent=registration.intent,
                origin=registration.origin,
                backend_endpoint=registration.endpoint,
                route_id=lease.route_id,
                public_ipv6=lease.public_ipv6,
                public_endpoint=lease.public_endpoint,
            )
            for registration, lease in zip(registrations, leases)
        )
        handle = NexusAgentHandle(self, thread, tuple(leases), published)
        self._handle = handle
        if announce:
            self._announce(published, print_fn)
        return handle

    def _announce(
        self,
        published: Tuple[PublishedCapability, ...],
        print_fn: Callable[[str], Any],
    ) -> None:
        print_fn("Nexus Agent registered")
        print_fn(f"Agent: {self.origin}")
        print_fn(f"Backend: {self.backend_endpoint}")
        for item in published:
            print_fn(f"Intent: {item.intent}")
            if item.public_url is not None:
                transport = (
                    "Plain HTTP + JWT"
                    if item.public_endpoint is not None
                    and item.public_endpoint.scheme == "http"
                    else "HTTPS"
                )
                print_fn(f"Public URL: {item.public_url}")
                print_fn(f"Transport: {transport}")
            elif item.public_ipv6 is not None:
                print_fn(f"Public IPv6: {item.public_ipv6}")

    def as_mcp_server(self) -> Any:
        """Export the same declared handlers as MCP without Router discovery.

        Requires hosted mode (selected automatically by Python upload) and the
        optional fastmcp dependency. No tool is invoked during export.
        """
        if self.runtime_mode != "hosted":
            raise NexusAgentError("MCP export requires runtime='hosted' or the Nexus hosted launcher")
        if self._hosted_mcp is None:
            from .hosted import create_mcp_server
            self._hosted_mcp = create_mcp_server(self)
        return self._hosted_mcp

    def run(self, **start_options: Any) -> Any:
        """Run the selected transport; edge shutdown also unregisters leases."""

        if self.runtime_mode == "hosted":
            return self.as_mcp_server().run(**start_options)
        handle = self.start(**start_options)
        try:
            while not handle.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            handle.close()
        return handle.published


__all__ = [
    "NexusAgent",
    "NexusAgentHandle",
    "PublishedCapability",
    "resolve_advertise_address",
    "resolve_router_url",
]
