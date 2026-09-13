import ipaddress
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
from urllib.parse import urlsplit


_TLS_NAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))+$"
)
_BUNDLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MCP_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,94}$")
_SLASH_COMMAND = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
MOBILE_SCOPES = (
    "mobile.observe",
    "mobile.screen.capture",
    "mobile.tap",
    "mobile.type_text",
    "mobile.swipe",
    "mobile.press_back",
    "mobile.open_app",
    "mobile.wait_for_state",
)
_MOBILE_SCOPES = frozenset(MOBILE_SCOPES)
COMPUTER_REQUIREMENTS = ("disabled", "optional", "required")
MOBILE_REQUIREMENTS = ("disabled", "optional", "required")
WORKSPACE_CAPABILITIES = (
    "connection.list",
    "connection.create",
    "connection.update",
    "connection.delete",
    "connection.test",
    "connection.bind",
    "files.list",
    "files.read",
    "files.write",
    "command.execute",
    "browser.control",
)
_WORKSPACE_CAPABILITIES = frozenset(WORKSPACE_CAPABILITIES)
_CLOUD_STATES = {
    "disabled",
    "unsupported",
    "pending",
    "unavailable",
    "degraded",
    "ready",
    "rejected",
    "suppressed",
}


@dataclass(frozen=True)
class NexusExecutionProfile:
    """Publisher-declared execution choice exposed to a private Run caller."""

    id: str
    label: str
    model: str
    reasoning_efforts: Tuple[str, ...] = ()
    default_reasoning_effort: str = ""
    context_window: Optional[int] = None
    is_default: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _PROFILE_ID.fullmatch(self.id):
            raise ValueError("execution profile id must be a safe identifier up to 64 characters")
        for name, value, maximum in (("label", self.label, 80), ("model", self.model, 128)):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"execution profile {name} must be 1-{maximum} characters")
            object.__setattr__(self, name, value.strip())
        if not isinstance(self.is_default, bool):
            raise ValueError("execution profile is_default must be a boolean")
        efforts = tuple(dict.fromkeys(str(value).strip().lower() for value in self.reasoning_efforts))
        if any(value not in REASONING_EFFORTS for value in efforts):
            raise ValueError("execution profile contains an unsupported reasoning effort")
        default = str(self.default_reasoning_effort or "").strip().lower()
        if default and default not in efforts:
            raise ValueError("default reasoning effort must be one of reasoning_efforts")
        if self.context_window is not None and (isinstance(self.context_window, bool) or int(self.context_window) <= 0):
            raise ValueError("context_window must be a positive integer")
        object.__setattr__(self, "reasoning_efforts", efforts)
        object.__setattr__(self, "default_reasoning_effort", default)
        if self.context_window is not None:
            object.__setattr__(self, "context_window", int(self.context_window))

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "model": self.model,
            "is_default": self.is_default,
            "reasoning_efforts": list(self.reasoning_efforts),
            "default_reasoning_effort": self.default_reasoning_effort,
        }
        if self.context_window is not None:
            value["context_window"] = self.context_window
        return value


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("MCP input_schema must be JSON-compatible") from exc


@dataclass(frozen=True)
class McpToolDescriptor:
    """Bounded MCP metadata published with one capability lease."""

    name: str
    title: Optional[str] = None
    description: Optional[str] = None
    input_schema: Mapping[str, Any] = field(
        default_factory=lambda: {"type": "object", "additionalProperties": True}
    )
    task: bool = False
    continuable: bool = False
    demo: bool = False
    chat: bool = False
    interactive: bool = False
    mobile_scopes: Tuple[str, ...] = ()
    slash_command: Optional[str] = None
    slash_description: Optional[str] = None
    execution_profiles: Tuple[NexusExecutionProfile, ...] = ()
    input_modalities: Tuple[str, ...] = ("text",)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _MCP_TOOL_NAME.fullmatch(self.name):
            raise ValueError(
                "MCP tool name must be a safe identifier up to 95 characters"
            )
        for name, value, maximum in (
            ("title", self.title, 127),
            ("description", self.description, 511),
        ):
            if value is not None and (
                not isinstance(value, str)
                or not value
                or len(value.encode("utf-8")) > maximum
            ):
                raise ValueError(
                    f"MCP tool {name} must be non-empty and at most {maximum} UTF-8 bytes"
                )
        if not isinstance(self.input_schema, Mapping):
            raise ValueError("MCP input_schema must be an object")
        schema = dict(self.input_schema)
        if len(_canonical_json(schema)) >= 2048:
            raise ValueError("MCP input_schema must be smaller than 2048 UTF-8 bytes")
        object.__setattr__(self, "input_schema", schema)
        if self.continuable and not (self.task or self.chat):
            raise ValueError("continuable MCP tools must also be tasks")
        if self.chat:
            object.__setattr__(self, "task", True)
            object.__setattr__(self, "interactive", True)
        scopes = tuple(dict.fromkeys(str(value).strip() for value in self.mobile_scopes if str(value).strip()))
        unsupported = [value for value in scopes if value not in _MOBILE_SCOPES]
        if unsupported:
            raise ValueError(f"unsupported Mobile scope: {unsupported[0]}")
        object.__setattr__(self, "mobile_scopes", scopes)
        command = self.slash_command
        if command is not None:
            command = str(command).strip().removeprefix("/")
            if not _SLASH_COMMAND.fullmatch(command):
                raise ValueError("slash_command must contain lowercase letters, numbers or hyphens")
            if not self.task:
                raise ValueError("slash command tools must enable task=True")
            properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
            if "content" not in properties and "message" not in properties:
                raise ValueError("slash command tools must accept content or message input")
            object.__setattr__(self, "slash_command", command)
        if self.slash_description is not None:
            description = str(self.slash_description).strip()
            if not description or len(description) > 160:
                raise ValueError("slash_description must be 1-160 characters")
            object.__setattr__(self, "slash_description", description)
        profiles = tuple(self.execution_profiles)
        if len(profiles) > 8 or any(not isinstance(item, NexusExecutionProfile) for item in profiles):
            raise ValueError("execution_profiles must contain at most eight NexusExecutionProfile values")
        if len({item.id for item in profiles}) != len(profiles):
            raise ValueError("execution profile ids must be unique within a tool")
        if sum(1 for item in profiles if item.is_default) > 1:
            raise ValueError("execution_profiles may declare only one default")
        object.__setattr__(self, "execution_profiles", profiles)
        modalities = tuple(dict.fromkeys(str(value).strip().lower() for value in self.input_modalities if str(value).strip()))
        if not modalities or any(value not in {"text", "image", "audio"} for value in modalities):
            raise ValueError("input_modalities may contain text, image and audio")
        properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        if "audio" in modalities and not (
            isinstance(properties.get("audio"), Mapping)
            and properties["audio"].get("type") == "array"
        ):
            raise ValueError("audio input requires an audio array in input_schema")
        if "image" in modalities and not (
            isinstance(properties.get("attachments"), Mapping)
            and properties["attachments"].get("type") == "array"
        ):
            raise ValueError("image input requires an attachments array in input_schema")
        object.__setattr__(self, "input_modalities", modalities)

    def to_dict(self, *, intent: str, intent_version: int) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "name": self.name,
            "input_schema": dict(self.input_schema),
            "intent": intent,
            "intent_version": int(intent_version),
            "task": self.task,
            "continuable": self.continuable,
            "demo": self.demo,
            "chat": self.chat,
            "interactive": self.interactive,
            "recovery_protocol": 1,
        }
        if self.mobile_scopes:
            value["mobile_scopes"] = list(self.mobile_scopes)
        if self.slash_command:
            value["slash_command"] = self.slash_command
            value["slash_description"] = self.slash_description or self.description or self.slash_command
        if self.execution_profiles:
            value["execution_profiles"] = [item.to_dict() for item in self.execution_profiles]
        value["input_modalities"] = list(self.input_modalities)
        if self.title is not None:
            value["title"] = self.title
        if self.description is not None:
            value["description"] = self.description
        return value


def normalize_workspace_capabilities(values: Iterable[str]) -> Tuple[str, ...]:
    """Validate and order Workspace scopes exactly like Nexus Cloud."""

    selected = set()
    for raw in values:
        value = str(raw or "").strip()
        if value not in _WORKSPACE_CAPABILITIES:
            raise ValueError(f"unsupported Workspace capability: {value}")
        selected.add(value)
    return tuple(value for value in WORKSPACE_CAPABILITIES if value in selected)


def normalize_mobile_capabilities(values: Iterable[str]) -> Tuple[str, ...]:
    """Validate and order Caller Mobile scopes exactly like Nexus Cloud."""

    selected = set()
    for raw in values:
        value = str(raw or "").strip()
        if value not in _MOBILE_SCOPES:
            raise ValueError(f"unsupported Mobile capability: {value}")
        selected.add(value)
    return tuple(value for value in MOBILE_SCOPES if value in selected)


@dataclass(frozen=True)
class ComputerRegistrationContract:
    """Computer requirement declared by the Agent and enforced by Cloud."""

    requirement: str = "disabled"
    workspace_capabilities: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.requirement not in COMPUTER_REQUIREMENTS:
            raise ValueError(
                "computer_requirement must be 'disabled', 'optional' or 'required'"
            )
        scopes = normalize_workspace_capabilities(self.workspace_capabilities)
        if self.requirement == "disabled" and scopes:
            raise ValueError(
                "disabled Computer requirement cannot declare Workspace capabilities"
            )
        object.__setattr__(self, "workspace_capabilities", scopes)

    @property
    def is_default(self) -> bool:
        return self.requirement == "disabled" and not self.workspace_capabilities

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requirement": self.requirement,
            "workspace_capabilities": list(self.workspace_capabilities),
        }


@dataclass(frozen=True)
class MobileRegistrationContract:
    """Caller Mobile requirement declared by the Agent and enforced by Cloud."""

    requirement: str = "disabled"
    mobile_capabilities: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.requirement not in MOBILE_REQUIREMENTS:
            raise ValueError(
                "mobile_requirement must be 'disabled', 'optional' or 'required'"
            )
        scopes = normalize_mobile_capabilities(self.mobile_capabilities)
        if self.requirement == "disabled" and scopes:
            raise ValueError(
                "disabled Mobile requirement cannot declare Mobile capabilities"
            )
        if self.requirement == "required" and not scopes:
            raise ValueError(
                "required Mobile requirement must declare at least one capability"
            )
        object.__setattr__(self, "mobile_capabilities", scopes)

    @property
    def is_default(self) -> bool:
        return self.requirement == "disabled" and not self.mobile_capabilities

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requirement": self.requirement,
            "mobile_capabilities": list(self.mobile_capabilities),
        }


@dataclass(frozen=True)
class CloudRegistrationManifest:
    """Agent-level Cloud intent attached to one local capability registration."""

    publish: bool
    agent_name: str
    tool: Optional[McpToolDescriptor] = None
    computer: Optional[ComputerRegistrationContract] = None
    mobile: Optional[MobileRegistrationContract] = None
    manifest_digest: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.publish, bool):
            raise ValueError("cloud publish must be a boolean")
        if (
            not isinstance(self.agent_name, str)
            or not self.agent_name.strip()
            or len(self.agent_name.encode("utf-8")) > 255
        ):
            raise ValueError("cloud agent_name must be 1-255 UTF-8 bytes")
        object.__setattr__(self, "agent_name", self.agent_name.strip())
        if self.manifest_digest is not None and not _SHA256.fullmatch(
            self.manifest_digest
        ):
            raise ValueError("manifest_digest must be a lowercase SHA-256 digest")

    def to_dict(self, *, intent: str, intent_version: int) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "publish": self.publish,
            "agent_name": self.agent_name,
        }
        if self.tool is not None:
            value["tool"] = self.tool.to_dict(
                intent=intent, intent_version=intent_version
            )
        if self.computer is not None:
            value["computer"] = self.computer.to_dict()
        if self.mobile is not None:
            value["mobile"] = self.mobile.to_dict()
        digest = self.manifest_digest or hashlib.sha256(
            _canonical_json(value)
        ).hexdigest()
        value["manifest_digest"] = digest
        return value


@dataclass(frozen=True)
class CloudRegistrationStatus:
    """Non-secret Cloud publication state returned by the trusted LAN router."""

    state: str
    origin: str = ""
    registration_id: Optional[str] = None
    agent_id: Optional[str] = None
    runtime_id: Optional[str] = None
    transport: Optional[str] = None
    mcp_url: Optional[str] = None
    manifest_digest: Optional[str] = None
    message: str = ""

    def __post_init__(self) -> None:
        if self.state not in _CLOUD_STATES:
            raise ValueError("router returned an unsupported Cloud registration state")

    @property
    def ready(self) -> bool:
        return self.state == "ready" and bool(self.agent_id)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CloudRegistrationStatus":
        def optional(name: str) -> Optional[str]:
            item = value.get(name)
            return str(item) if item not in (None, "") else None

        return cls(
            state=str(value.get("state") or "pending"),
            origin=str(value.get("origin") or ""),
            registration_id=optional("registration_id"),
            agent_id=optional("agent_id"),
            runtime_id=optional("runtime_id"),
            transport=optional("transport"),
            mcp_url=optional("mcp_url"),
            manifest_digest=optional("manifest_digest"),
            message=str(value.get("message") or ""),
        )


@dataclass(frozen=True)
class BackendTlsIdentity:
    """Lease-bound connection metadata for an SDK-managed HTTPS Agent."""

    address: str
    port: int
    tls_server_name: str
    ca_bundle_id: str = "system"
    certificate_sha256: Optional[str] = None

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.address)
        except ValueError as exc:
            raise ValueError("backend TLS address must be a numeric IP address") from exc
        if address.is_unspecified or address.is_loopback or address.is_multicast \
                or address.is_link_local:
            raise ValueError("backend TLS address must be a routable unicast address")
        if isinstance(self.port, bool) or not isinstance(self.port, int) \
                or not 1 <= self.port <= 65535:
            raise ValueError("backend TLS port must be between 1 and 65535")
        if not _TLS_NAME.fullmatch(self.tls_server_name):
            raise ValueError(
                "tls_server_name must be a lowercase dotted DNS identity"
            )
        if not _BUNDLE_ID.fullmatch(self.ca_bundle_id):
            raise ValueError("ca_bundle_id must be a safe identifier up to 63 characters")
        fingerprint = self.certificate_sha256
        if fingerprint is not None:
            fingerprint = fingerprint.lower()
            if not _SHA256.fullmatch(fingerprint):
                raise ValueError("certificate_sha256 must contain 64 hexadecimal characters")
            object.__setattr__(self, "certificate_sha256", fingerprint)
        object.__setattr__(self, "address", address.compressed)

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        if value["certificate_sha256"] is None:
            del value["certificate_sha256"]
        return value


@dataclass(frozen=True)
class CapabilityRegistration:
    intent: str
    origin: str
    endpoint: str
    tenant: str
    version: int = 1
    region: str = "local"
    route_id: Optional[str] = None
    cost_microunits: int = 0
    latency_ms: int = 0
    trust: int = 50
    load_permille: int = 0
    hop_count: int = 0
    lease_seconds: int = 30
    public_ipv6: Optional[str] = None
    backend_tls: Optional[BackendTlsIdentity] = None
    cloud: Optional[CloudRegistrationManifest] = None

    def __post_init__(self) -> None:
        if self.public_ipv6 not in (None, "auto"):
            raise ValueError("public_ipv6 must be None or 'auto'")
        if self.backend_tls is not None:
            parsed = urlsplit(self.endpoint)
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError("HTTPS endpoint port is invalid") from exc
            if (
                parsed.scheme != "https"
                or parsed.hostname != self.backend_tls.tls_server_name
                or port != self.backend_tls.port
                or parsed.username is not None
                or parsed.fragment
            ):
                raise ValueError(
                    "backend_tls name and port must match the HTTPS endpoint"
                )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if data["route_id"] is None:
            del data["route_id"]
        if data["public_ipv6"] is None:
            del data["public_ipv6"]
        if self.backend_tls is None:
            del data["backend_tls"]
        else:
            data["backend_tls"] = self.backend_tls.to_dict()
        if self.cloud is None:
            del data["cloud"]
        else:
            data["cloud"] = self.cloud.to_dict(
                intent=self.intent, intent_version=self.version
            )
        return data


@dataclass(frozen=True)
class PublicAgentEndpoint:
    """Portable descriptor for a router-managed or Agent-owned IPv6 endpoint."""

    address: str
    port: int
    tls_server_name: Optional[str] = None
    ca_bundle_id: Optional[str] = None
    scheme: str = "https"

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.address)
        except ValueError as exc:
            raise ValueError("address must be an IPv6 address") from exc
        if not isinstance(address, ipaddress.IPv6Address) or "%" in self.address:
            raise ValueError("address must be a non-scoped IPv6 address")
        if self.scheme not in ("https", "http"):
            raise ValueError("scheme must be https or http")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.scheme == "https":
            for name, value, maximum in (
                ("tls_server_name", self.tls_server_name, 253),
                ("ca_bundle_id", self.ca_bundle_id, 63),
            ):
                if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
                    raise ValueError(f"{name} must be a non-empty value up to {maximum} characters")
        elif self.tls_server_name is not None or self.ca_bundle_id is not None:
            raise ValueError("plain HTTP descriptors must not contain TLS identity fields")
        object.__setattr__(self, "address", address.compressed)

    @property
    def url(self) -> str:
        return f"{self.scheme}://[{self.address}]:{self.port}"

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        if value["tls_server_name"] is None:
            del value["tls_server_name"]
        if value["ca_bundle_id"] is None:
            del value["ca_bundle_id"]
        return value

    def to_json(self, *, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PublicAgentEndpoint":
        try:
            return cls(
                scheme=str(value.get("scheme", "https")),
                address=str(value["address"]),
                port=int(value["port"]),
                tls_server_name=(
                    str(value["tls_server_name"])
                    if value.get("tls_server_name") is not None else None
                ),
                ca_bundle_id=(
                    str(value["ca_bundle_id"])
                    if value.get("ca_bundle_id") is not None else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid public Agent endpoint descriptor") from exc

    def connect(self, **credentials: Any):
        """Create a DirectIPv6Agent using caller-supplied credentials/CA file."""
        from .direct_ipv6 import DirectIPv6Agent

        return DirectIPv6Agent.from_endpoint(self, **credentials)


@dataclass(frozen=True)
class LeaseInfo:
    route_id: str
    generation: int
    lease_seconds: int
    removed: bool = False
    public_ipv6: Optional[str] = None
    public_endpoint: Optional[PublicAgentEndpoint] = None


@dataclass(frozen=True)
class SseEvent:
    data: str
    event: Optional[str] = None
    event_id: Optional[str] = None
    retry_ms: Optional[int] = None


@dataclass(frozen=True)
class AgentEnvelope:
    """A validated inbound Nexus Agent Envelope."""

    version: str
    intent: str
    intent_version: int
    task_id: str
    source_agent: str
    tenant: str
    hop_limit: int
    payload: Any
    constraints: Mapping[str, Any] = field(default_factory=dict)
    target_agent: Optional[str] = None
    route_id: Optional[str] = None
    resume_from_event_id: int = 0
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)
    authenticated_subject: Optional[str] = None
    authenticated_scopes: Tuple[str, ...] = ()
    auth_claims: Mapping[str, Any] = field(default_factory=dict, repr=False)
    run_context: Any = field(default=None, repr=False, compare=False)

    @property
    def protocol(self) -> Optional[str]:
        if isinstance(self.payload, Mapping):
            value = self.payload.get("protocol")
            if isinstance(value, str):
                return value
        return None

    @property
    def selector(self) -> Optional[str]:
        if isinstance(self.payload, Mapping):
            value = self.payload.get("selector")
            if isinstance(value, str):
                return value
        return None

    @property
    def protocol_request(self) -> Optional[Mapping[str, Any]]:
        if isinstance(self.payload, Mapping):
            value = self.payload.get("request")
            if isinstance(value, Mapping):
                return value
        return None


@dataclass(frozen=True)
class AgentResponse:
    """Optional explicit status and headers for a synchronous Agent reply."""

    body: Any
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
