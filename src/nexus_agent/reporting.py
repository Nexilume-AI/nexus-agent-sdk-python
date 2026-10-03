"""Dependency-free AG-UI reporting for Nexus-hosted Agent runs."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import queue
import re
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePath
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Sequence, Tuple, TypedDict, Union
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .auth import (
    CloudTrustOriginError,
    CloudTrustUnavailableError,
    CloudTrustVerificationError,
)
from .hosted_trust import HostedCloudTrustError, hosted_cloud_opener
from .browser import (
    NexusAsyncBrowserSession,
    NexusBrowserObservation,
    NexusBrowserSession,
    NexusAttachedBrowserSession,
    NexusBrowserActionFailed,
    NexusBrowserComputerRequired,
    NexusBrowserPermissionRequired,
    NexusBrowserSessionLost,
    NexusBrowserStaleObservation,
    NexusBrowserTunnelUnavailable,
    NexusBrowserUnavailable,
)
from .workspace import (
    CommandResult,
    SSHTestResult,
    SSHWorkspaceConnection,
    SSHWorkspaceConnectionCreate,
    SSHWorkspaceConnectionUpdate,
    WorkspaceEntry,
)
from .workspace_files import BinaryWorkspaceMixin


class NexusComputerError(RuntimeError):
    """A caller-owned Computer operation could not be completed."""

    def __init__(self, message: str, *, code: str = "WORKSPACE_UNAVAILABLE") -> None:
        self.code = code
        super().__init__(message)


_DELEGATE_FAILURE_CODES = {
    "COMPUTER_RUNTIME_OFFLINE", "COMPUTER_RUNTIME_REVOKED", "COMPUTER_CAPABILITY_UNAVAILABLE",
    "COMPUTER_PERMISSION_REQUIRED", "WORKSPACE_PERMISSION_REQUIRED", "RUN_DELEGATE_UNAVAILABLE",
}


class NexusMemoryError(RuntimeError):
    """A caller-scoped Memory operation could not be completed."""


class NexusMemoryConflict(NexusMemoryError):
    """The Memory changed after it was recalled by this Run."""

    def __init__(self, *, current_revision: int) -> None:
        self.current_revision = int(current_revision)
        super().__init__("Memory was modified by another Run")


class NexusMemoryUnavailable(NexusMemoryError):
    """The run-scoped Memory service is unavailable or not authorized."""


class NexusRecoveryError(RuntimeError):
    """A platform-managed operation could not be journaled or replayed safely."""


class NexusRecoveryDiverged(NexusRecoveryError):
    """Replay reached a different operation than the original attempt."""


class NexusChatError(RuntimeError):
    """A run-scoped interactive Chat operation failed."""


class NexusChatTimeout(NexusChatError):
    """The caller did not answer before the interaction expired."""


class NexusChatUnavailable(NexusChatError):
    """Interactive Chat is unavailable for this Run or transport."""


class NexusRunCancelled(NexusChatError):
    """The Nexus invocation was cancelled while waiting for input."""


class NexusCheckpointError(RuntimeError):
    """A durable run checkpoint could not be read or written."""


class NexusRunContextExchangeError(RuntimeError):
    """An OpenWrt IPv6 invocation could not establish its cloud Run context."""

    def __init__(self, message: str, *, code: str = "RUN_CONTEXT_EXCHANGE_FAILED") -> None:
        self.code = code
        super().__init__(message)


class NexusRunContextUnavailable(RuntimeError):
    """The current invocation did not provide its read-only Run context."""


class NexusBillingReportError(RuntimeError):
    """A final Agent-reported cost could not be accepted by Nexus Cloud."""


class NexusBillingUnavailable(NexusBillingReportError):
    """This Run does not include an Agent-reported billing context."""


class NexusUsageError(RuntimeError):
    """Actual model usage could not be recorded for this Run."""


class NexusUsageUnavailable(NexusUsageError):
    """The hosted Run did not provide an authenticated usage endpoint."""


class NexusBillingLineItem(TypedDict):
    code: str
    description: str
    amount: Union[Decimal, int, str]


class NexusMobileError(RuntimeError):
    """A caller-owned Mobile operation could not be completed."""


class NexusMobileUnavailable(NexusMobileError):
    """No authorized, online Mobile is available for this Run."""


class _NexusMobileTransportUnavailable(NexusMobileUnavailable):
    """A transient failure while polling the Nexus Mobile delegate."""


class NexusMobilePermissionRequired(NexusMobileError):
    """The caller did not grant the requested Mobile capability."""


class NexusMobileBusy(NexusMobileError):
    """Another Run currently owns the selected Mobile control lease."""


class NexusMobileTimeout(NexusMobileError):
    """The Mobile command did not finish before its timeout."""


class NexusMobileActionFailed(NexusMobileError):
    """The Mobile device rejected or failed an action."""

    _CODES = frozenset({
        "MOBILE_ACTION_FAILED",
        "MOBILE_ACTION_REJECTED",
        "MOBILE_ACTION_CANCELLED",
    })

    def __init__(self, message: str, *, code: str = "MOBILE_ACTION_FAILED") -> None:
        self.code = code if code in self._CODES else "MOBILE_ACTION_FAILED"
        super().__init__(message)


class NexusMemoryItem(TypedDict, total=False):
    id: str
    kind: str
    text: str
    content: Dict[str, Any]
    scope: str
    confidence: str
    sensitivity: str
    consent: str
    license: str
    revision: int
    created_at: str
    updated_at: str


class MemoryDeleteResult(TypedDict):
    id: str
    deleted: bool
    revision: int


@dataclass(frozen=True)
class NexusChatReply:
    interaction_id: str
    key: str
    value: str
    text: str
    answered_at: str = ""


@dataclass(frozen=True)
class NexusCheckpoint:
    stage: str
    data: Dict[str, Any]
    revision: int
    updated_at: str = ""


@dataclass(frozen=True)
class NexusExecutionSelection:
    profile: str = ""
    model: str = ""
    reasoning_effort: str = ""
    context_window: Optional[int] = None


class _RunInput:
    """Safe metadata for immutable files attached to the current Run."""

    def __init__(self, context: "NexusRunContext") -> None:
        self._context = context

    @property
    def files(self) -> list[Dict[str, Any]]:
        return [dict(item) for item in self._context.input_files]

    @property
    def audio(self) -> list[Dict[str, Any]]:
        return [
            dict(item)
            for item in self._context.input_files
            if str(item.get("source_kind") or "").lower() == "audio"
            or str(item.get("content_type") or "").lower().startswith("audio/")
        ]


@dataclass(frozen=True)
class NexusMobileStatus:
    enabled: bool
    available: bool
    platform: str = ""
    status: str = "unavailable"
    capabilities: Tuple[str, ...] = ()


@dataclass(frozen=True)
class NexusMobileObservation:
    data: Dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class NexusMobileScreen:
    content: bytes = field(repr=False)
    content_type: str = "image/webp"
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass(frozen=True)
class NexusMobileCommandResult:
    command_id: str
    action: str
    status: str
    result: Dict[str, Any] = field(default_factory=dict, repr=False)


_UNSET = object()


@dataclass(frozen=True)
class NexusReportingConfig:
    """Bounded, fail-open delivery settings for AG-UI events."""

    queue_size: int = 256
    request_timeout: float = 3.0
    max_retries: int = 2
    flush_timeout: float = 5.0
    retry_delay: float = 0.25
    control_poll_interval: float = 5.0
    outbox_directory: str = ""
    outbox_max_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.queue_size < 1:
            raise ValueError("queue_size must be positive")
        if self.request_timeout <= 0 or self.flush_timeout < 0:
            raise ValueError("reporting timeouts must be non-negative")
        if self.max_retries < 0 or self.max_retries > 10:
            raise ValueError("max_retries must be between 0 and 10")
        if self.retry_delay < 0:
            raise ValueError("retry_delay must be non-negative")
        if self.control_poll_interval < 0 or self.outbox_max_bytes < 1:
            raise ValueError("Invalid control/outbox limits")


@dataclass(frozen=True)
class DeliveryReport:
    enabled: bool
    sent: int
    failed: int
    dropped: int
    pending: int
    last_error: str = ""
    buffered: int = 0


@dataclass(frozen=True)
class _Delivery:
    context: "NexusRunContext"
    event_id: str
    body: bytes
    outbox_path: Optional[Path] = None


class _Dispatcher:
    def __init__(self, maximum: int) -> None:
        self.queue: "queue.Queue[_Delivery]" = queue.Queue(maxsize=maximum)
        self.thread = threading.Thread(
            target=self._run,
            name="nexus-agui-reporter",
            daemon=True,
        )
        self.thread.start()

    def submit(self, delivery: _Delivery) -> bool:
        try:
            self.queue.put_nowait(delivery)
            return True
        except queue.Full:
            return False

    def _run(self) -> None:
        while True:
            delivery = self.queue.get()
            try:
                delivery.context._deliver(delivery)
            except Exception:
                context = delivery.context
                with context._condition:
                    context._pending = max(context._pending - 1, 0)
                    context._failed += 1
                    context._outbox_inflight.discard(delivery.outbox_path)
                    context._last_error = "AG-UI delivery failed safely"
                    context._condition.notify_all()
            finally:
                self.queue.task_done()


_DISPATCHERS: Dict[int, _Dispatcher] = {}
_DISPATCHERS_LOCK = threading.Lock()
_CURRENT_RUN: ContextVar[Optional["NexusRunContext"]] = ContextVar(
    "nexus_agent_current_run", default=None
)


def _dispatcher(maximum: int) -> _Dispatcher:
    with _DISPATCHERS_LOCK:
        dispatcher = _DISPATCHERS.get(maximum)
        if dispatcher is None:
            dispatcher = _Dispatcher(maximum)
            _DISPATCHERS[maximum] = dispatcher
        return dispatcher


def _header(headers: Optional[Mapping[str, Any]], name: str) -> str:
    if not headers:
        return ""
    expected = name.lower()
    for key, value in headers.items():
        if str(key).lower() == expected:
            return str(value or "").strip()
    return ""


def _nexus_response_data(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    data = value.get("data") if value.get("ok") is True else value
    return dict(data) if isinstance(data, Mapping) else {}


def _text_header(headers: Optional[Mapping[str, Any]], name: str) -> str:
    """Decode an ASCII JSON companion; legacy printable headers still work."""
    encoded = _header(headers, name + "-Json")
    if encoded:
        try:
            value = json.loads(encoded)
        except (TypeError, ValueError):
            raise NexusComputerError("Invalid encoded Nexus text header: " + name) from None
        if not isinstance(value, str):
            raise NexusComputerError("Invalid encoded Nexus text header: " + name)
        return value
    return _header(headers, name)


def _positive_revision(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("revision must be a positive integer")
    try:
        revision = int(value)
    except (TypeError, ValueError):
        raise ValueError("revision must be a positive integer") from None
    if revision < 1:
        raise ValueError("revision must be a positive integer")
    return revision


def _optional_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _json_list(value: Any) -> list[Dict[str, Any]]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed[:8] if isinstance(item, Mapping)]


def _mobile_coordinate_space(values: Iterable[float]) -> str:
    """Use normalized coordinates for 0..1 values and pixels otherwise."""
    coordinates = tuple(float(value) for value in values)
    if any(value < 0 for value in coordinates):
        raise ValueError("Mobile coordinates must be non-negative")
    return "pixels" if any(value > 1 for value in coordinates) else "normalized"


def _safe_http_error(exc: HTTPError) -> Dict[str, Any]:
    try:
        raw = exc.read()
    except OSError:
        raw = b""
    finally:
        exc.close()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    error = payload.get("error")
    return dict(error) if isinstance(error, Mapping) else {}


def _event_mapping(event: Any) -> Dict[str, Any]:
    if isinstance(event, Mapping):
        result = dict(event)
    else:
        dump = getattr(event, "model_dump", None)
        if not callable(dump):
            raise TypeError("event must be a mapping or expose model_dump()")
        try:
            # Pydantic-backed official AG-UI events may contain UUID, datetime,
            # or Enum values. JSON mode gives the transport a fully serializable
            # mapping while preserving field aliases from the wire schema.
            result = dict(dump(mode="json", by_alias=True, exclude_none=True))
        except TypeError:
            try:
                result = dict(dump(by_alias=True, exclude_none=True))
            except TypeError:
                result = dict(dump())
    raw_type = result.get("type")
    event_type = str(getattr(raw_type, "value", raw_type) or "").strip().upper()
    if not event_type:
        raise ValueError("AG-UI event type is required")
    result["type"] = event_type
    return result


class _ToolTrace:
    def __init__(
        self,
        context: "NexusRunContext",
        name: str,
        arguments: Any,
        visibility: str,
    ) -> None:
        self.context = context
        self.name = str(name)
        self.arguments = arguments
        self.visibility = visibility
        self.tool_call_id = context._stable_local_id("tool")
        self._has_result = False

    def __enter__(self) -> "_ToolTrace":
        self.context.emit(
            {
                "type": "TOOL_CALL_START",
                "toolCallId": self.tool_call_id,
                "toolCallName": self.name,
            },
            visibility=self.visibility,
        )
        if self.arguments is not None:
            self.context.emit(
                {
                    "type": "TOOL_CALL_ARGS",
                    "toolCallId": self.tool_call_id,
                    "delta": json.dumps(
                        self.arguments, separators=(",", ":"), ensure_ascii=False
                    ),
                },
                visibility=self.visibility,
            )
        return self

    def result(self, value: Any) -> None:
        self._has_result = True
        self.context.emit(
            {
                "type": "TOOL_CALL_RESULT",
                "toolCallId": self.tool_call_id,
                "result": value,
            },
            visibility=self.visibility,
        )

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.context.emit(
            {"type": "TOOL_CALL_END", "toolCallId": self.tool_call_id},
            visibility=self.visibility,
        )


class _TraceReporter:
    def __init__(self, context: "NexusRunContext") -> None:
        self.context = context

    @contextmanager
    def step(self, name: str, *, visibility: str = "public") -> Iterator[None]:
        step_name = str(name).strip()
        if not step_name:
            raise ValueError("trace step name is required")
        self.context.emit(
            {"type": "STEP_STARTED", "stepName": step_name},
            visibility=visibility,
        )
        try:
            yield
        finally:
            self.context.emit(
                {"type": "STEP_FINISHED", "stepName": step_name},
                visibility=visibility,
            )

    def tool(
        self,
        name: str,
        *,
        arguments: Any = None,
        visibility: str = "public",
    ) -> _ToolTrace:
        if not str(name).strip():
            raise ValueError("tool trace name is required")
        return _ToolTrace(self.context, str(name).strip(), arguments, visibility)


def _plan_state(value: Any) -> str:
    normalized = str(value or "pending").strip().lower()
    return {
        "completed": "done",
        "complete": "done",
        "success": "done",
        "in_progress": "running",
        "in-progress": "running",
        "active": "running",
        "blocked": "blocked",
        "failed": "failed",
    }.get(normalized, normalized if normalized in {"pending", "running", "done", "blocked", "failed"} else "pending")


class _PlanReporter:
    def __init__(self, context: "NexusRunContext") -> None:
        self.context = context
        self._steps: list[Dict[str, Any]] = []

    def set(self, steps: Sequence[Mapping[str, Any]], *, visibility: str = "public") -> bool:
        if isinstance(steps, (str, bytes)):
            raise ValueError("plan steps must be a sequence of mappings")
        normalized: list[Dict[str, Any]] = []
        ids: set[str] = set()
        for index, item in enumerate(steps):
            if not isinstance(item, Mapping):
                raise ValueError("each plan step must be a mapping")
            step_id = str(item.get("id") or f"step-{index + 1}").strip()
            title = str(item.get("title") or item.get("label") or "").strip()
            if not step_id or not title:
                raise ValueError("plan step id and title are required")
            if step_id in ids:
                raise ValueError("plan step ids must be unique")
            ids.add(step_id)
            value: Dict[str, Any] = {
                "id": step_id,
                "label": title,
                "state": _plan_state(item.get("status", item.get("state"))),
            }
            if item.get("detail") is not None:
                value["detail"] = str(item.get("detail"))[:2048]
            normalized.append(value)
        self._steps = normalized
        return self.context.emit(
            {
                "type": "ACTIVITY_SNAPSHOT",
                "messageId": "task-plan",
                "activityType": "PLAN",
                "content": {"steps": normalized},
            },
            visibility=visibility,
        )

    def update(
        self,
        step_id: str,
        *,
        status: Any = _UNSET,
        title: Any = _UNSET,
        detail: Any = _UNSET,
        visibility: str = "public",
    ) -> bool:
        expected = str(step_id or "").strip()
        index = next((i for i, item in enumerate(self._steps) if item["id"] == expected), -1)
        if index < 0:
            raise ValueError("plan step does not exist; call plan.set() first")
        patch: list[Dict[str, Any]] = []
        if status is not _UNSET:
            value = _plan_state(status)
            self._steps[index]["state"] = value
            patch.append({"op": "replace", "path": f"/steps/{index}/state", "value": value})
        if title is not _UNSET:
            value = str(title or "").strip()
            if not value:
                raise ValueError("plan step title cannot be empty")
            self._steps[index]["label"] = value
            patch.append({"op": "replace", "path": f"/steps/{index}/label", "value": value})
        if detail is not _UNSET:
            value = str(detail or "")[:2048]
            operation = "replace" if "detail" in self._steps[index] else "add"
            self._steps[index]["detail"] = value
            patch.append({"op": operation, "path": f"/steps/{index}/detail", "value": value})
        if not patch:
            raise ValueError("plan update requires at least one field")
        return self.context.emit(
            {
                "type": "ACTIVITY_DELTA",
                "messageId": "task-plan",
                "activityType": "PLAN",
                "patch": patch,
            },
            visibility=visibility,
        )


class _DisplayReporter:
    def __init__(self, context: "NexusRunContext") -> None:
        self.context = context

    def title(self, value: Any, *, visibility: str = "public") -> bool:
        normalized = " ".join(str(value or "").split()).strip()
        if not normalized:
            raise ValueError("display title is required")
        if len(normalized) > 80:
            raise ValueError("display title cannot exceed 80 characters")
        return self.context.emit(
            {
                "type": "CUSTOM",
                "name": "nexus.display.title",
                "value": {"title": normalized},
            },
            visibility=visibility,
        )


class _ShellReporter:
    def __init__(self, context: "NexusRunContext") -> None:
        self.context = context

    def write(
        self,
        message: Any,
        *,
        stream: str = "stdout",
        command_id: str = "",
        visibility: str = "public",
    ) -> bool:
        normalized_stream = str(stream or "stdout").lower()
        if normalized_stream not in {"command", "stdout", "stderr", "system"}:
            raise ValueError("shell stream must be command, stdout, stderr or system")
        text = str(message or "")[:4096]
        if not text:
            raise ValueError("shell message is required")
        level = "error" if normalized_stream == "stderr" else "info"
        return self.context.emit(
            {
                "type": "CUSTOM",
                "name": "nexus.computer.log",
                "value": {
                    "message": text,
                    "stream": normalized_stream,
                    "level": level,
                    "command_id": str(command_id or ""),
                },
            },
            visibility=visibility,
        )


class _BrowserReporter:
    MAX_IMAGE_BYTES = 2 * 1024 * 1024
    ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp"}

    def __init__(self, context: "NexusRunContext") -> None:
        self.context = context

    def frame(
        self,
        image: Union[str, os.PathLike[str], bytes, bytearray, memoryview],
        *,
        url: str = "",
        title: str = "",
        text: str = "",
        content_type: str = "",
        width: Optional[int] = None,
        height: Optional[int] = None,
        observation_id: str = "",
        revision: Optional[int] = None,
        action: str = "",
        action_status: str = "",
        dom_node_count: Optional[int] = None,
        visibility: str = "public",
    ) -> bool:
        if isinstance(image, (bytes, bytearray, memoryview)):
            payload = bytes(image)
            filename = "frame.png"
        else:
            path = Path(image)
            payload = path.read_bytes()
            filename = path.name or "frame.png"
        if not payload or len(payload) > self.MAX_IMAGE_BYTES:
            raise ValueError("browser frame must be between 1 byte and 2 MiB")
        mime = str(content_type or mimetypes.guess_type(filename)[0] or "").lower()
        if mime not in self.ALLOWED_TYPES:
            raise ValueError("browser frame must be PNG, JPEG or WebP")
        if isinstance(image, (bytes, bytearray, memoryview)):
            filename = {
                "image/jpeg": "frame.jpg",
                "image/webp": "frame.webp",
            }.get(mime, "frame.png")
        asset = self.context._upload_display_asset(
            content=payload,
            file_name=filename,
            content_type=mime,
            width=width,
            height=height,
        )
        screenshot_url = str(asset.get("url") or "")
        if not screenshot_url:
            return False
        value: Dict[str, Any] = {
            "frame_id": str(asset.get("id") or uuid.uuid4().hex),
            "screenshot_url": screenshot_url,
            "url": _safe_browser_display_url(url)[:2048],
            "title": str(title or "")[:512],
            "text": str(text or "")[:2048],
            "width": width,
            "height": height,
        }
        if observation_id:
            value["observation_id"] = str(observation_id)[:128]
        if revision is not None:
            value["revision"] = max(int(revision), 0)
        if action:
            value["action"] = str(action)[:64]
        if action_status:
            value["action_status"] = str(action_status)[:32]
        if dom_node_count is not None:
            value["dom_node_count"] = max(int(dom_node_count), 0)
        return self.context.emit(
            {
                "type": "CUSTOM",
                "name": "nexus.computer.frame",
                "value": value,
            },
            visibility=visibility,
        )

    def _publish_observation(
        self,
        observation: NexusBrowserObservation,
        action: str,
        action_status: str,
    ) -> None:
        self.frame(
            observation.image,
            content_type=observation.content_type,
            url=observation.url,
            title=observation.title,
            text=f"Browser observation {observation.revision}",
            width=observation.viewport[0],
            height=observation.viewport[1],
            observation_id=observation.observation_id,
            revision=observation.revision,
            action=action,
            action_status=action_status,
            dom_node_count=len(observation.dom.nodes),
        )

    def session(
        self,
        *,
        viewport: Sequence[int] = (1280, 720),
    ) -> NexusBrowserSession:
        return NexusBrowserSession(
            run_id=self.context.run_id,
            publisher=self._publish_observation,
            viewport=viewport,
        )

    def attached_session(
        self,
        *,
        viewport: Sequence[int] = (1280, 720),
    ) -> NexusAttachedBrowserSession:
        if not self.context.browser_enabled:
            raise NexusBrowserUnavailable(
                "Attached Computer browser is unavailable for this Run"
            )
        return NexusAttachedBrowserSession(
            run_id=self.context.run_id,
            requester=self.context._browser_request,
            viewport=viewport,
        )


def _safe_browser_display_url(value: Any) -> str:
    """Remove credentials and sensitive query values before Cloud display."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        hostname = parsed.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            port = ""
        sensitive = ("token", "secret", "password", "passwd", "auth", "key", "session", "cookie")
        query = urlencode([
            (key, "[REDACTED]" if any(marker in key.casefold() for marker in sensitive) else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ])
        return urlunsplit((parsed.scheme.lower(), hostname + port, parsed.path, query, ""))
    except Exception:
        return ""


class _ChatReporter:
    def __init__(self, context: "NexusRunContext") -> None:
        self.context = context

    @property
    def enabled(self) -> bool:
        return bool(
            self.context._local_interaction_request
            or (self.context.interaction_url and self.context._interaction_token)
        )

    def say(self, message: Any, *, visibility: str = "public") -> bool:
        text = str(message or "").strip()
        if not text:
            raise ValueError("chat message is required")
        if len(text) > 4000:
            raise ValueError("chat message cannot exceed 4000 characters")
        message_id = self.context._stable_local_id("assistant")
        results = [
            self.context.emit({"type": "TEXT_MESSAGE_START", "messageId": message_id, "role": "assistant"}, visibility=visibility),
            self.context.emit({"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": text}, visibility=visibility),
            self.context.emit({"type": "TEXT_MESSAGE_END", "messageId": message_id}, visibility=visibility),
        ]
        return all(results)

    def ask(
        self,
        prompt: str,
        *,
        key: str,
        choices: Optional[Sequence[Mapping[str, Any]]] = None,
        kind: str = "",
        timeout: int = 300,
        visibility: str = "public",
    ) -> NexusChatReply:
        if not self.enabled:
            raise NexusChatUnavailable("Interactive Chat is unavailable for this Run")
        if self.context.interaction_mode not in {"stream", "task", "demo"}:
            raise NexusChatUnavailable("Interactive Chat requires Streamable HTTP/SSE or MCP Task mode")
        question = str(prompt or "").strip()
        stable_key = str(key or "").strip()
        if not question or not stable_key:
            raise ValueError("chat prompt and stable key are required")
        if len(question) > 4000 or len(stable_key) > 128:
            raise ValueError("chat prompt or key is too long")
        normalized_choices = []
        for item in choices or ():
            value = str(item.get("value") or "").strip()
            label = str(item.get("label") or value).strip()
            if not value or not label:
                raise ValueError("chat choices require value and label")
            normalized_choices.append({"value": value[:128], "label": label[:120]})
        if len(normalized_choices) > 20:
            raise ValueError("chat choices cannot exceed 20 items")
        interaction_kind = str(kind or "").strip().lower()
        if not interaction_kind:
            confirm_values = {"yes", "no", "ok", "cancel", "continue", "confirm"}
            choice_values = {item["value"].lower() for item in normalized_choices}
            interaction_kind = (
                "confirm"
                if len(normalized_choices) == 2 and choice_values.issubset(confirm_values)
                else ("select" if normalized_choices else "text")
            )
        if interaction_kind not in {"text", "confirm", "select"}:
            raise ValueError("chat kind must be text, confirm, or select")
        if interaction_kind in {"confirm", "select"} and not normalized_choices:
            raise ValueError("confirm and select chat questions require choices")
        if interaction_kind == "text" and normalized_choices:
            raise ValueError("text chat questions cannot include choices")
        wait_seconds = min(max(int(timeout), 1), 900)
        self.say(question, visibility=visibility)
        self.context.flush()
        wire_key = (
            f"turn-{self.context.turn_index}:{stable_key}"
            if self.context.turn_index > 0
            else stable_key
        )
        if len(wire_key) > 128:
            digest = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:24]
            wire_key = f"turn-{self.context.turn_index}:{digest}"
        interaction = self.context._interaction_request(
            "",
            method="POST",
            payload={
                "key": wire_key,
                "prompt": question,
                "kind": interaction_kind,
                "choices": normalized_choices,
                "timeout_seconds": wait_seconds,
                "visibility": visibility,
            },
        )
        interaction_id = str(interaction.get("id") or "")
        if not interaction_id:
            raise NexusChatUnavailable("Nexus did not create an interactive Chat request")
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            current = self.context._interaction_request(
                quote(interaction_id, safe="") + "/",
                method="GET",
            )
            status = str(current.get("status") or "")
            if status == "answered":
                response = current.get("response") if isinstance(current.get("response"), Mapping) else {}
                return NexusChatReply(
                    interaction_id=interaction_id,
                    key=stable_key,
                    value=str(response.get("value") or response.get("text") or ""),
                    text=str(response.get("text") or response.get("value") or ""),
                    answered_at=str(current.get("answered_at") or ""),
                )
            if status == "cancelled":
                raise NexusRunCancelled("The Nexus Run was cancelled")
            if status == "expired":
                raise NexusChatTimeout("The caller did not answer before the interaction expired")
            time.sleep(0.5)
        raise NexusChatTimeout("The caller did not answer before the interaction expired")


class NexusExternalOperation:
    """One optional custom side effect protected by the Run operation journal."""

    def __init__(self, reporter: "_RecoveryReporter", prepared: Mapping[str, Any]) -> None:
        self._reporter = reporter
        self.id = str(prepared.get("id") or "")
        self.idempotency_key = str(prepared.get("idempotency_key") or "")
        self.replayed = bool(prepared.get("replayed"))
        self.execute = bool(prepared.get("execute", True))
        self.result = prepared.get("result")
        self._completion: Dict[str, Any] = {}

    def complete(self, result: Optional[Mapping[str, Any]] = None) -> None:
        self._completion = dict(result or {})


class _RecoveryReporter:
    def __init__(
        self,
        context: "NexusRunContext",
        *,
        managed: bool,
        attempt: int,
        is_replay: bool,
        last_committed_operation: int,
    ) -> None:
        self._context = context
        self.managed = bool(managed and context.recovery_url and context._interaction_token)
        self.attempt = max(int(attempt or 0), 0)
        self.is_replay = bool(is_replay)
        self.last_committed_operation = max(int(last_committed_operation or 0), 0)
        self._sequence = 0
        self._lock = threading.Lock()

    def _prepare(
        self,
        operation_type: str,
        request: Mapping[str, Any],
        *,
        can_reconcile: bool,
    ) -> Dict[str, Any]:
        if not self.managed:
            raise NexusRecoveryError("Managed recovery is unavailable for this Agent version")
        try:
            encoded = json.dumps(
                dict(request), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("operation request must be JSON-compatible") from exc
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        payload = {
            "sequence": sequence,
            "operation_type": str(operation_type or "").strip(),
            "request_digest": hashlib.sha256(encoded).hexdigest(),
            "can_reconcile": bool(can_reconcile),
        }
        try:
            return self._context._interaction_request_url(
                self._context.recovery_url, method="POST", payload=payload
            )
        except NexusRecoveryDiverged:
            raise
        except (NexusChatUnavailable, NexusRecoveryError) as exc:
            raise NexusRecoveryError("Nexus could not prepare the managed operation") from exc

    def _finish(
        self,
        operation_id: str,
        *,
        status: str,
        result: Optional[Mapping[str, Any]] = None,
        error_code: str = "",
    ) -> Dict[str, Any]:
        try:
            return self._context._interaction_request_url(
                urljoin(self._context.recovery_url, quote(str(operation_id)) + "/"),
                method="POST",
                payload={
                    "status": status,
                    "result": dict(result or {}),
                    "error_code": str(error_code or "")[:96],
                },
            )
        except NexusChatUnavailable as exc:
            raise NexusRecoveryError("Nexus could not commit the managed operation") from exc

    @contextmanager
    def external_operation(
        self,
        operation_type: str,
        request: Mapping[str, Any],
        *,
        can_reconcile: bool = False,
    ) -> Iterator[NexusExternalOperation]:
        prepared = self._prepare(operation_type, request, can_reconcile=can_reconcile)
        operation = NexusExternalOperation(self, prepared)
        if str(prepared.get("status") or "") == "outcome_unknown":
            raise NexusRecoveryError("The previous external operation has an unknown outcome")
        try:
            yield operation
        except BaseException:
            self._finish(operation.id, status="outcome_unknown", error_code="EXTERNAL_OPERATION_INTERRUPTED")
            raise
        else:
            if operation.execute:
                self._finish(operation.id, status="succeeded", result=operation._completion)

    def call(
        self,
        operation_type: str,
        request: Mapping[str, Any],
        callback: Callable[[str], Any],
        *,
        can_reconcile: bool = True,
    ) -> Any:
        prepared = self._prepare(operation_type, request, can_reconcile=can_reconcile)
        if not prepared.get("execute", True):
            result = prepared.get("result") or {}
            return result.get("value")
        try:
            value = callback(str(prepared.get("idempotency_key") or ""))
        except BaseException:
            # A local exception does not prove that a non-queryable external
            # side effect did not happen.  Preserve the ambiguity so Nexus
            # pauses the Turn instead of replaying it automatically.
            self._finish(
                str(prepared["id"]),
                status="failed" if can_reconcile else "outcome_unknown",
                error_code=(
                    "MANAGED_OPERATION_FAILED"
                    if can_reconcile
                    else "OPERATION_OUTCOME_UNKNOWN"
                ),
            )
            raise
        result = {"value": value}
        self._finish(str(prepared["id"]), status="succeeded", result=result)
        return value


class _AsyncRecovery:
    def __init__(self, sync: _RecoveryReporter) -> None:
        self._sync = sync

    @property
    def managed(self) -> bool:
        return self._sync.managed

    @property
    def attempt(self) -> int:
        return self._sync.attempt

    @property
    def is_replay(self) -> bool:
        return self._sync.is_replay

    @property
    def last_committed_operation(self) -> int:
        return self._sync.last_committed_operation

    async def call(self, operation_type: str, request: Mapping[str, Any], callback: Callable[[str], Any], *, can_reconcile: bool = True) -> Any:
        return await asyncio.to_thread(self._sync.call, operation_type, request, callback, can_reconcile=can_reconcile)


class _CheckpointReporter:
    def __init__(self, context: "NexusRunContext") -> None:
        self.context = context

    def load(self) -> Optional[NexusCheckpoint]:
        if not self.context.checkpoint_url or not self.context._interaction_token:
            raise NexusCheckpointError("Run checkpoint service is unavailable")
        value = self.context._interaction_request_url(self.context.checkpoint_url, method="GET")
        if not value or not value.get("stage"):
            return None
        return NexusCheckpoint(
            stage=str(value.get("stage") or ""),
            data=dict(value.get("data") or {}),
            revision=int(value.get("revision") or 1),
            updated_at=str(value.get("updated_at") or ""),
        )

    def save(
        self,
        *,
        stage: str,
        data: Optional[Mapping[str, Any]] = None,
        revision: Optional[int] = None,
    ) -> NexusCheckpoint:
        normalized_stage = str(stage or "").strip()
        normalized_data = dict(data or {})
        if not normalized_stage:
            raise ValueError("checkpoint stage is required")
        if len(json.dumps(normalized_data, ensure_ascii=False).encode("utf-8")) > 65536:
            raise ValueError("checkpoint data cannot exceed 64 KiB")
        payload: Dict[str, Any] = {"stage": normalized_stage, "data": normalized_data}
        if revision is not None:
            payload["expected_revision"] = _positive_revision(revision)
        value = self.context._interaction_request_url(self.context.checkpoint_url, method="PUT", payload=payload)
        return NexusCheckpoint(
            stage=str(value.get("stage") or normalized_stage),
            data=dict(value.get("data") or normalized_data),
            revision=int(value.get("revision") or 1),
            updated_at=str(value.get("updated_at") or ""),
        )


class _MemoryReporter:
    def __init__(self, context: "NexusRunContext") -> None:
        self.context = context

    def add(
        self,
        text: str = "",
        *,
        data: Optional[Mapping[str, Any]] = None,
        kind: str = "fact",
        confidence: float = 1.0,
        sensitivity: str = "internal",
        consent: str = "pending",
        license: str = "unknown",
        scope: str = "caller",
        visibility: str = "public",
    ) -> bool:
        content_text = str(text or "").strip()
        content_json = dict(data or {})
        if not content_text and not content_json:
            raise ValueError("memory requires text or data")
        return self.context.emit(
            {
                "type": "CUSTOM",
                "name": "nexus.memory.item",
                "value": {
                    "memory_type": str(kind),
                    "content_text": content_text,
                    "content_json": content_json,
                    "confidence": str(confidence),
                    "sensitivity_level": str(sensitivity),
                    "consent_status": str(consent),
                    "license_status": str(license),
                    "scope": str(scope),
                },
            },
            visibility=visibility,
        )

    def recall(self, *, limit: int = 50) -> list[NexusMemoryItem]:
        if not self.context.memory_url:
            return []
        result = self.context._internal_request(
            self.context.memory_url + "?" + urlencode({"limit": int(limit)}),
            method="GET",
        )
        data = result.get("data") if result.get("ok") is True else result
        return list(data.get("items") or []) if isinstance(data, Mapping) else []

    def update(
        self,
        memory_id: str,
        *,
        revision: int,
        text: Any = _UNSET,
        data: Any = _UNSET,
        kind: Any = _UNSET,
        confidence: Any = _UNSET,
        sensitivity: Any = _UNSET,
        consent: Any = _UNSET,
        license: Any = _UNSET,
    ) -> NexusMemoryItem:
        payload: Dict[str, Any] = {"expected_revision": _positive_revision(revision)}
        for argument, field, value in (
            ("text", "content_text", text),
            ("data", "content_json", data),
            ("kind", "memory_type", kind),
            ("confidence", "confidence", confidence),
            ("sensitivity", "sensitivity_level", sensitivity),
            ("consent", "consent_status", consent),
            ("license", "license_status", license),
        ):
            if value is _UNSET:
                continue
            if argument == "data":
                if not isinstance(value, Mapping):
                    raise ValueError("memory data must be a mapping")
                payload[field] = dict(value)
            else:
                payload[field] = value
        if len(payload) == 1:
            raise ValueError("memory update requires at least one field")
        if self.context.recovery.managed:
            result = self.context.recovery.call(
                "memory.update",
                {"memory_id": str(memory_id), "payload": payload},
                lambda _key: self.context._memory_request(self._item_url(memory_id), method="PATCH", payload=payload),
                can_reconcile=False,
            )
        else:
            result = self.context._memory_request(self._item_url(memory_id), method="PATCH", payload=payload)
        return dict(result)  # type: ignore[return-value]

    def delete(self, memory_id: str, *, revision: int) -> MemoryDeleteResult:
        payload = {"expected_revision": _positive_revision(revision)}
        if self.context.recovery.managed:
            result = self.context.recovery.call(
                "memory.delete",
                {"memory_id": str(memory_id), "payload": payload},
                lambda _key: self.context._memory_request(self._item_url(memory_id), method="DELETE", payload=payload),
                can_reconcile=False,
            )
        else:
            result = self.context._memory_request(self._item_url(memory_id), method="DELETE", payload=payload)
        return dict(result)  # type: ignore[return-value]

    def _item_url(self, memory_id: str) -> str:
        normalized = str(memory_id or "").strip()
        if not normalized:
            raise ValueError("memory_id is required")
        return self.context.memory_url.rstrip("/") + "/" + quote(normalized, safe="") + "/"


class NexusImageReference(TypedDict):
    """A protected image belonging to the current Run, not a public URL."""
    asset_id: str
    content_type: str


class _MediaReporter:
    def __init__(self, context: "NexusRunContext") -> None:
        self.context = context

    def read_image(self, reference: Mapping[str, Any]) -> bytes:
        """Read an input image using the current Run delegate and Cloud TLS."""
        try:
            asset_id = str(uuid.UUID(str(reference["asset_id"])))
        except (KeyError, TypeError, ValueError):
            raise ValueError("A Run image asset_id is required") from None
        ctx = self.context
        if not ctx.display_asset_url or not ctx._interaction_token:
            raise NexusChatUnavailable("Run image access is unavailable on this Cloud")
        request = Request(
            ctx.display_asset_url.rstrip("/") + "/" + asset_id + "/",
            headers={"Accept": "image/png,image/jpeg,image/webp", "X-Nexus-Interaction-Token": ctx._interaction_token},
            method="GET",
        )
        try:
            with ctx._open_cloud(request) as response:
                mime = response.headers.get("Content-Type", "").split(";")[0]
                data = response.read(2 * 1024 * 1024 + 1)
            if mime not in {"image/png", "image/jpeg", "image/webp"} or not 0 < len(data) <= 2 * 1024 * 1024:
                raise ValueError()
            return data
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise NexusChatUnavailable("Run image is unavailable or access was rejected") from None


class _AsyncMedia:
    def __init__(self, media: _MediaReporter) -> None:
        self.media = media

    async def read_image(self, reference: Mapping[str, Any]) -> bytes:
        return await asyncio.to_thread(self.media.read_image, reference)


class _OutputReporter:
    def __init__(self, context: "NexusRunContext") -> None:
        self.context = context

    def created(self, path: str, **metadata: Any) -> bool:
        return self._file("nexus.file.created", path, metadata)

    def upload_file(self, path, **options):
        """Stream a large immutable output to Cloud; never embed its bytes in AG-UI."""
        return self.context.files.upload(path, **options)

    def updated(self, path: str, **metadata: Any) -> bool:
        return self._file("nexus.file.updated", path, metadata)

    def image(
        self,
        image: Union[str, os.PathLike[str], bytes, bytearray, memoryview],
        *,
        content_type: str = "image/png",
        title: str = "Image",
        alt: str = "",
    ) -> Dict[str, Any]:
        """Upload a protected Run image directly to Cloud, never through OpenWrt.

        Returns a small Nexus JSON asset reference. No public URL or inline
        base64 is placed in the event stream or tool result. Cloud validates the
        actual raster bytes and binds the asset to this Run and its caller.
        """
        if content_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("output image must be PNG, JPEG or WebP")
        if isinstance(image, (bytes, bytearray, memoryview)):
            data = bytes(image)
        else:
            with Path(image).open("rb") as stream:
                data = stream.read(2 * 1024 * 1024 + 1)
        if not 0 < len(data) <= 2 * 1024 * 1024:
            raise ValueError("output image must be between 1 byte and 2 MiB")
        asset = self.context._upload_display_asset(content=data, content_type=content_type, file_name="image." + content_type.split("/")[1])
        if not asset.get("id"):
            raise NexusChatUnavailable("Cloud did not return a protected image asset")
        block = {"type": "image", "asset_id": str(asset["id"]), "url": str(asset.get("url") or ""), "title": str(title)[:256], "alt": str(alt)[:1024]}
        self.context.emit({"type": "CUSTOM", "name": "nexus.image.created", "value": block}, visibility="public")
        return block

    def ready(
        self,
        result: Any = None,
        *,
        visibility: str = "public",
    ) -> bool:
        return self.context.emit(
            {
                "type": "CUSTOM",
                "name": "nexus.deliverable.ready",
                "value": {"result": result},
            },
            visibility=visibility,
        )

    def path(self, path: str) -> str:
        normalized = str(path or "").strip().replace("\\", "/").lstrip("/")
        if not normalized:
            raise ValueError("output path is required")
        return normalized

    def write_text(
        self,
        path: str,
        content: str,
        *,
        content_type: str = "text/plain",
        **metadata: Any,
    ) -> Dict[str, Any]:
        normalized = self.path(path)
        result = self.context.workspace._write(normalized, str(content), root="output")
        self.created(normalized, content_type=content_type, **metadata)
        return result

    def _file(self, name: str, path: str, metadata: Dict[str, Any]) -> bool:
        workspace_path = str(path or "").strip().replace("\\", "/").lstrip("/")
        if not workspace_path:
            raise ValueError("output path is required")
        visibility = str(metadata.pop("visibility", "public"))
        content_type = metadata.pop("content_type", None)
        mime_type = metadata.pop("mime_type", None)
        value: Dict[str, Any] = {
            "workspace_path": workspace_path,
            "original_file_name": str(
                metadata.pop("original_file_name", "")
                or PurePath(workspace_path).name
            ),
            "content_type": str(
                content_type
                or mime_type
                or mimetypes.guess_type(workspace_path)[0]
                or ""
            ),
            "producer_step": str(metadata.pop("producer_step", "")),
            "license_status": str(metadata.pop("license", "internal")),
        }
        value.update(metadata)
        return self.context.emit(
            {"type": "CUSTOM", "name": name, "value": value},
            visibility=visibility,
        )


class _ComputerReporter:
    def __init__(self, context: "NexusRunContext") -> None:
        self.context = context
        self.connections = _ConnectionReporter(context)

    @property
    def enabled(self) -> bool:
        return self.context.computer_enabled

    def bindings(self) -> Tuple[Mapping[str, Any], ...]:
        self.context._require_workspace_capability("connection.bind")
        result = self.context._delegate_request("bindings/", method="GET")
        return tuple(dict(item) for item in result.get("items") or ())

    def bind(self, connection_id: str, *, make_default: bool = True) -> Mapping[str, Any]:
        self.context._require_workspace_capability("connection.bind")
        result = self.context._delegate_request(
            "bindings/",
            method="POST",
            payload={"connection_id": str(connection_id), "make_default": bool(make_default)},
        )
        self.context._apply_computer_binding(result)
        return result


class _ConnectionReporter:
    def __init__(self, context: "NexusRunContext") -> None:
        self.context = context

    def list(self) -> Tuple[SSHWorkspaceConnection, ...]:
        self.context._require_workspace_capability("connection.list")
        result = self.context._delegate_request("connections/", method="GET")
        return tuple(SSHWorkspaceConnection.from_dict(item) for item in result.get("items") or ())

    def get(self, connection_id: str) -> SSHWorkspaceConnection:
        self.context._require_workspace_capability("connection.list")
        result = self.context._delegate_request(f"connections/{connection_id}/", method="GET")
        return SSHWorkspaceConnection.from_dict(result)

    def create(
        self,
        name: str,
        ssh_host: str,
        ssh_user: str,
        *,
        auth_mode: str = "private_key",
        ssh_port: int = 22,
        workspace_root: str = "~/.nexus",
        private_key: str = "",
        password: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> SSHWorkspaceConnection:
        self.context._require_workspace_capability("connection.create")
        values = SSHWorkspaceConnectionCreate(
            name=name,
            ssh_host=ssh_host,
            ssh_user=ssh_user,
            auth_mode=auth_mode,
            ssh_port=ssh_port,
            workspace_root=workspace_root,
            private_key=private_key,
            password=password,
            metadata=dict(metadata or {}),
        )
        result = self.context._delegate_request(
            "connections/",
            method="POST",
            payload={
                "name": values.name,
                "ssh_host": values.ssh_host,
                "ssh_port": values.ssh_port,
                "ssh_user": values.ssh_user,
                "auth_mode": values.auth_mode,
                "workspace_root": values.workspace_root,
                "private_key": values.private_key,
                "password": values.password,
                "metadata": dict(values.metadata),
            },
        )
        return SSHWorkspaceConnection.from_dict(result)

    def update(
        self,
        connection_id: str,
        values: Optional[SSHWorkspaceConnectionUpdate] = None,
        **changes: Any,
    ) -> SSHWorkspaceConnection:
        self.context._require_workspace_capability("connection.update")
        update = values or SSHWorkspaceConnectionUpdate(**changes)
        payload = {
            name: getattr(update, name)
            for name in (
                "name", "ssh_host", "ssh_port", "ssh_user", "auth_mode",
                "workspace_root", "private_key", "password", "metadata",
            )
            if getattr(update, name) is not None
        }
        result = self.context._delegate_request(
            f"connections/{connection_id}/",
            method="PATCH",
            payload=payload,
        )
        return SSHWorkspaceConnection.from_dict(result)

    def delete(self, connection_id: str) -> None:
        self.context._require_workspace_capability("connection.delete")
        self.context._delegate_request(f"connections/{connection_id}/", method="DELETE")

    def test(self, connection_id: str) -> SSHTestResult:
        self.context._require_workspace_capability("connection.test")
        result = self.context._delegate_request(
            f"connections/{connection_id}/test/", method="POST", payload={}
        )
        return SSHTestResult.from_dict(result)

    def validate(self, **values: Any) -> SSHTestResult:
        self.context._require_workspace_capability("connection.test")
        result = self.context._delegate_request(
            "connections/validate/", method="POST", payload=values
        )
        return SSHTestResult.from_dict(result)


class _TerminalReporter:
    def __init__(self, context: "NexusRunContext") -> None:
        self.context = context

    def run(
        self,
        command: str,
        cwd: str = ".",
        timeout: Optional[int] = None,
        *,
        display: bool = True,
    ) -> CommandResult:
        self.context._require_workspace_capability("command.execute")
        if not self.context.computer_enabled or not self.context.terminal_url:
            raise NexusComputerError("Computer is not enabled for this run")
        if not isinstance(display, bool):
            raise TypeError("display must be a bool")
        command_timeout = int(timeout) if timeout is not None else 300
        payload = {
                "command": str(command),
                "cwd": str(cwd),
                "timeout_seconds": timeout,
                "display": display,
            }
        def invoke(idempotency_key: str) -> Dict[str, Any]:
            return self.context._internal_request(
                self.context.terminal_url,
                method="POST",
                payload={**payload, "idempotency_key": idempotency_key},
                request_timeout=max(
                float(self.context.config.request_timeout),
                float(command_timeout) + 5.0,
                ),
            )
        result = (
            self.context.recovery.call("terminal.run", payload, invoke, can_reconcile=True)
            if self.context.recovery.managed
            else invoke("")
        )
        return CommandResult.from_dict(result)

    def status(self) -> Dict[str, Any]:
        self.context._require_workspace_capability("command.execute")
        if not self.context.computer_enabled or not self.context.terminal_url:
            return {"enabled": False, "status": "disabled", "viewer_mode": "read_only"}
        return self.context._internal_request(self.context.terminal_url, method="GET")


class _WorkspaceReporter(BinaryWorkspaceMixin):
    # Cloud waits up to 30 seconds for a Computer command, plus 2 seconds for
    # result observation. Event delivery's 3-second timeout must not truncate
    # that bounded operation. Do not retry writes after an ambiguous timeout.
    _REQUEST_TIMEOUT = 35.0
    def __init__(self, context: "NexusRunContext") -> None:
        self.context = context

    def list(self, path: str = ".") -> Tuple[WorkspaceEntry, ...]:
        self.context._require_workspace_capability("files.list")
        result = self._get(path, operation="list")
        return tuple(WorkspaceEntry.from_dict(item) for item in result.get("items") or ())

    def read_text(self, path: str) -> str:
        self.context._require_workspace_capability("files.read")
        result = self._get(path, operation="read")
        return str(result.get("content") or "")

    def write_text(self, path: str, content: str) -> Dict[str, Any]:
        self.context._require_workspace_capability("files.write")
        return self._write(path, content, root="workspace")

    def _get(self, path: str, *, operation: str, root: str = "workspace") -> Dict[str, Any]:
        if not self.context.computer_enabled or not self.context.workspace_url:
            raise NexusComputerError("Computer is not enabled for this run")
        query = urlencode({"path": str(path), "operation": operation, "root": root})
        return self.context._internal_request(
            self.context.workspace_url + "?" + query,
            method="GET", request_timeout=self._REQUEST_TIMEOUT,
        )

    def _write(self, path: str, content: str, *, root: str) -> Dict[str, Any]:
        if not self.context.computer_enabled or not self.context.workspace_url:
            raise NexusComputerError("Computer is not enabled for this run")
        payload = {"path": str(path), "content": str(content), "root": root}
        def invoke(idempotency_key: str) -> Dict[str, Any]:
            return self.context._internal_request(
                self.context.workspace_url,
                method="POST",
                payload={**payload, "idempotency_key": idempotency_key},
                request_timeout=self._REQUEST_TIMEOUT,
            )
        return (
            self.context.recovery.call("workspace.write", payload, invoke, can_reconcile=True)
            if self.context.recovery.managed
            else invoke("")
        )


class _MobileReporter:
    _TERMINAL = frozenset({"succeeded", "failed", "rejected", "canceled", "deleted"})

    def __init__(self, context: "NexusRunContext") -> None:
        self.context = context

    @property
    def enabled(self) -> bool:
        return bool(
            self.context.mobile_enabled
            and self.context.mobile_delegate_url
            and self.context._mobile_delegate_token
        )

    def status(self) -> NexusMobileStatus:
        if not self.enabled:
            return NexusMobileStatus(enabled=False, available=False)
        value = self.context._mobile_request("", method="GET")
        return NexusMobileStatus(
            enabled=bool(value.get("enabled", True)),
            available=bool(value.get("available")),
            platform=str(value.get("platform") or ""),
            status=str(value.get("status") or "unavailable"),
            capabilities=tuple(str(item) for item in value.get("capabilities") or ()),
        )

    def observe(self, *, timeout: float = 120.0) -> NexusMobileObservation:
        command = self._command("observe", {}, "mobile.observe", timeout=timeout)
        return NexusMobileObservation(data=dict(command.result or {}))

    def capture_screen(self, *, timeout: float = 120.0) -> NexusMobileScreen:
        command = self._command("capture_screen", {}, "mobile.screen.capture", timeout=timeout)
        encoded = str(command.result.get("screenshot_base64") or "")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise NexusMobileActionFailed("Mobile screen data was unavailable") from None
        return NexusMobileScreen(
            content=content,
            content_type=str(command.result.get("content_type") or "image/webp"),
            width=_optional_int(command.result.get("width")),
            height=_optional_int(command.result.get("height")),
        )

    def tap_text(self, text: str, *, timeout: float = 120.0) -> NexusMobileCommandResult:
        return self._command("tap_text", {"text": str(text)}, "mobile.tap", timeout=timeout)

    def tap(self, *, x: float, y: float, timeout: float = 120.0) -> NexusMobileCommandResult:
        arguments = {"x": float(x), "y": float(y)}
        arguments["coordinate_space"] = _mobile_coordinate_space(arguments.values())
        return self._command("tap_coordinates", arguments, "mobile.tap", timeout=timeout)

    def type_text(self, text: str, *, timeout: float = 120.0) -> NexusMobileCommandResult:
        return self._command("type_text", {"text": str(text)}, "mobile.type_text", timeout=timeout)

    def swipe(self, start_x: float, start_y: float, end_x: float, end_y: float, *, duration_ms: int = 300, timeout: float = 120.0) -> NexusMobileCommandResult:
        coordinates = {
            "start_x": float(start_x),
            "start_y": float(start_y),
            "end_x": float(end_x),
            "end_y": float(end_y),
        }
        return self._command(
            "swipe",
            {
                **coordinates,
                "coordinate_space": _mobile_coordinate_space(coordinates.values()),
                "duration_ms": int(duration_ms),
            },
            "mobile.swipe",
            timeout=timeout,
        )

    def press_back(self, *, timeout: float = 120.0) -> NexusMobileCommandResult:
        return self._command("press_back", {}, "mobile.press_back", timeout=timeout)

    def open_app(self, package: str, *, timeout: float = 120.0) -> NexusMobileCommandResult:
        return self._command("open_app", {"package": str(package)}, "mobile.open_app", timeout=timeout)

    def wait_for_state(self, *, text: str, timeout: float = 30.0) -> NexusMobileCommandResult:
        return self._command("wait_for_state", {"text": str(text)}, "mobile.wait_for_state", timeout=timeout)

    def _command(self, action: str, arguments: Mapping[str, Any], capability: str, *, timeout: float) -> NexusMobileCommandResult:
        if not self.enabled:
            raise NexusMobileUnavailable("Mobile is not enabled for this Run")
        if capability not in self.context.mobile_capabilities:
            raise NexusMobilePermissionRequired("Mobile capability is not authorized for this Run")
        deadline = time.monotonic() + max(float(timeout), 0.1)
        request_payload = {
            "action": action,
            "arguments": dict(arguments),
            "ttl_seconds": min(max(int(timeout), 5), 900),
        }
        def invoke(idempotency_key: str) -> Dict[str, Any]:
            payload = {
                **request_payload,
                # Operation journal keys are opaque nxo_* strings; Mobile's
                # wire contract is UUID. Keep the mapping stable across retry
                # and process recovery without widening the server API type.
                "client_request_id": (
                    str(uuid.uuid5(uuid.NAMESPACE_URL, "nexus.mobile.command:" + idempotency_key))
                    if idempotency_key else str(uuid.uuid4())
                ),
            }
            create_deadline = min(deadline, time.monotonic() + 30.0)
            retry_delay = 0.1
            while True:
                try:
                    current = self.context._mobile_request("", method="POST", payload=payload)
                    break
                except _NexusMobileTransportUnavailable:
                    if time.monotonic() >= create_deadline:
                        raise NexusMobileUnavailable("Nexus Mobile service is temporarily unavailable") from None
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 1.0)
            command_id = str(current.get("id") or "")
            while str(current.get("status") or "") not in self._TERMINAL:
                if time.monotonic() >= deadline:
                    raise NexusMobileTimeout("Mobile command timed out")
                time.sleep(0.25)
                try:
                    current = self.context._mobile_request(f"commands/{quote(command_id)}/", method="GET")
                except _NexusMobileTransportUnavailable:
                    continue
            return dict(current)
        value = (
            self.context.recovery.call(f"mobile.{action}", request_payload, invoke, can_reconcile=True)
            if self.context.recovery.managed
            else invoke("")
        )
        command_id = str(value.get("id") or "")
        status = str(value.get("status") or "failed")
        if status != "succeeded":
            failures = {
                "rejected": (
                    "MOBILE_ACTION_REJECTED",
                    "Mobile action was rejected by the caller",
                ),
                "canceled": (
                    "MOBILE_ACTION_CANCELLED",
                    "Mobile action was canceled before completion",
                ),
                "deleted": (
                    "MOBILE_ACTION_CANCELLED",
                    "Mobile action was canceled before completion",
                ),
            }
            code, message = failures.get(
                status,
                (
                    "MOBILE_ACTION_FAILED",
                    "Mobile action failed on the attached device",
                ),
            )
            raise NexusMobileActionFailed(message, code=code)
        return NexusMobileCommandResult(
            command_id=command_id,
            action=str(value.get("action") or action),
            status=status,
            result=dict(value.get("result") or {}),
        )


class _AsyncConnections:
    def __init__(self, sync: _ConnectionReporter) -> None:
        self._sync = sync

    async def list(self):
        return await asyncio.to_thread(self._sync.list)

    async def get(self, connection_id: str):
        return await asyncio.to_thread(self._sync.get, connection_id)

    async def create(self, *args: Any, **kwargs: Any):
        return await asyncio.to_thread(self._sync.create, *args, **kwargs)

    async def update(self, connection_id: str, values=None, **changes: Any):
        return await asyncio.to_thread(self._sync.update, connection_id, values, **changes)

    async def delete(self, connection_id: str):
        return await asyncio.to_thread(self._sync.delete, connection_id)

    async def test(self, connection_id: str):
        return await asyncio.to_thread(self._sync.test, connection_id)

    async def validate(self, **values: Any):
        return await asyncio.to_thread(self._sync.validate, **values)


class _AsyncComputer:
    def __init__(self, sync: _ComputerReporter) -> None:
        self._sync = sync
        self.connections = _AsyncConnections(sync.connections)

    @property
    def enabled(self) -> bool:
        return self._sync.enabled

    async def bindings(self):
        return await asyncio.to_thread(self._sync.bindings)

    async def bind(self, connection_id: str, *, make_default: bool = True):
        return await asyncio.to_thread(
            self._sync.bind, connection_id, make_default=make_default
        )


class _AsyncWorkspace:
    def __init__(self, sync: _WorkspaceReporter) -> None:
        self._sync = sync

    async def list(self, path: str = "."):
        return await asyncio.to_thread(self._sync.list, path)

    async def read_text(self, path: str) -> str:
        return await asyncio.to_thread(self._sync.read_text, path)

    async def write_text(self, path: str, content: str):
        return await asyncio.to_thread(self._sync.write_text, path, content)

    async def read_bytes(self, path: str, *, max_bytes: int = 16 * 1024 * 1024) -> bytes:
        return await asyncio.to_thread(self._sync.read_bytes, path, max_bytes=max_bytes)

    async def write_bytes(self, path: str, content: bytes):
        return await asyncio.to_thread(self._sync.write_bytes, path, content)

    async def download(self, path: str, destination: Union[str, Path], *, max_bytes: int = 1024 * 1024 * 1024):
        return await asyncio.to_thread(self._sync.download, path, destination, max_bytes=max_bytes)

    async def upload(self, source: Union[str, Path], path: str):
        return await asyncio.to_thread(self._sync.upload, source, path)


class _AsyncTerminal:
    def __init__(self, sync: _TerminalReporter) -> None:
        self._sync = sync

    async def run(
        self,
        command: str,
        cwd: str = ".",
        timeout: Optional[int] = None,
        *,
        display: bool = True,
    ):
        return await asyncio.to_thread(
            self._sync.run,
            command,
            cwd,
            timeout,
            display=display,
        )

    async def status(self):
        return await asyncio.to_thread(self._sync.status)


class _AsyncPlan:
    def __init__(self, sync: _PlanReporter) -> None:
        self._sync = sync

    async def set(self, steps: Sequence[Mapping[str, Any]], *, visibility: str = "public"):
        return await asyncio.to_thread(self._sync.set, steps, visibility=visibility)

    async def update(self, step_id: str, *, visibility: str = "public", **changes: Any):
        return await asyncio.to_thread(
            self._sync.update, step_id, visibility=visibility, **changes
        )


class _AsyncDisplay:
    def __init__(self, sync: _DisplayReporter) -> None:
        self._sync = sync

    async def title(self, value: Any, *, visibility: str = "public"):
        return await asyncio.to_thread(self._sync.title, value, visibility=visibility)


class _AsyncShell:
    def __init__(self, sync: _ShellReporter) -> None:
        self._sync = sync

    async def write(self, message: str, *, stream: str = "stdout", visibility: str = "public"):
        return await asyncio.to_thread(
            self._sync.write, message, stream=stream, visibility=visibility
        )


class _AsyncBrowser:
    def __init__(self, sync: _BrowserReporter) -> None:
        self._sync = sync

    async def frame(self, image: Union[str, os.PathLike[str], bytes, bytearray], **metadata: Any):
        return await asyncio.to_thread(self._sync.frame, image, **metadata)

    def session(
        self,
        *,
        viewport: Sequence[int] = (1280, 720),
    ) -> NexusAsyncBrowserSession:
        return NexusAsyncBrowserSession(self._sync.session(viewport=viewport))

    def attached_session(
        self,
        *,
        viewport: Sequence[int] = (1280, 720),
    ) -> NexusAsyncBrowserSession:
        return NexusAsyncBrowserSession(
            self._sync.attached_session(viewport=viewport)
        )


class _AsyncChat:
    def __init__(self, sync: _ChatReporter) -> None:
        self._sync = sync

    async def say(self, text: str, *, visibility: str = "public"):
        return await asyncio.to_thread(self._sync.say, text, visibility=visibility)

    async def ask(self, prompt: str, **options: Any) -> NexusChatReply:
        return await asyncio.to_thread(self._sync.ask, prompt, **options)


class _ProjectContext:
    def __init__(self, context: "NexusRunContext") -> None:
        self._context = context

    def _value(self) -> Dict[str, Any]:
        value = self._context._load_run_context().get("project")
        return dict(value) if isinstance(value, Mapping) else {}

    @property
    def id(self) -> str:
        return str(self._value().get("id") or "")

    @property
    def name(self) -> str:
        return str(self._value().get("name") or "")

    @property
    def instructions(self) -> str:
        return str(self._value().get("instructions") or "")

    @property
    def revision(self) -> int:
        return max(int(self._value().get("revision") or 0), 0)


class _RunContextReader:
    def __init__(self, context: "NexusRunContext") -> None:
        self._context = context

    @property
    def id(self) -> str:
        return self._context.run_id

    @property
    def turn_index(self) -> int:
        return self._context.turn_index

    def messages(self, *, limit: int = 200, refresh: bool = False) -> list[Dict[str, Any]]:
        requested = min(max(int(limit), 1), 200)
        value = self._context._load_run_context(limit=requested, refresh=refresh)
        items = value.get("messages") if isinstance(value.get("messages"), list) else []
        return [dict(item) for item in items[-requested:] if isinstance(item, Mapping)]


class _AsyncRunReader:
    def __init__(self, sync: _RunContextReader) -> None:
        self._sync = sync

    @property
    def id(self) -> str:
        return self._sync.id

    @property
    def turn_index(self) -> int:
        return self._sync.turn_index

    async def messages(self, *, limit: int = 200, refresh: bool = False) -> list[Dict[str, Any]]:
        return await asyncio.to_thread(self._sync.messages, limit=limit, refresh=refresh)


class _AsyncCheckpoint:
    def __init__(self, sync: _CheckpointReporter) -> None:
        self._sync = sync

    async def load(self) -> Optional[NexusCheckpoint]:
        return await asyncio.to_thread(self._sync.load)

    async def save(self, *, stage: str, data: Mapping[str, Any], revision: Optional[int] = None):
        return await asyncio.to_thread(
            self._sync.save, stage=stage, data=data, revision=revision
        )


class _AsyncMobile:
    def __init__(self, sync: "_MobileReporter") -> None:
        self._sync = sync

    @property
    def enabled(self) -> bool:
        return self._sync.enabled

    async def status(self) -> NexusMobileStatus:
        return await asyncio.to_thread(self._sync.status)

    async def observe(self, *, timeout: float = 120.0) -> NexusMobileObservation:
        return await asyncio.to_thread(self._sync.observe, timeout=timeout)

    async def capture_screen(self, *, timeout: float = 120.0) -> NexusMobileScreen:
        return await asyncio.to_thread(self._sync.capture_screen, timeout=timeout)

    async def tap_text(self, text: str, *, timeout: float = 120.0) -> NexusMobileCommandResult:
        return await asyncio.to_thread(self._sync.tap_text, text, timeout=timeout)

    async def tap(self, *, x: float, y: float, timeout: float = 120.0) -> NexusMobileCommandResult:
        return await asyncio.to_thread(self._sync.tap, x=x, y=y, timeout=timeout)

    async def type_text(self, text: str, *, timeout: float = 120.0) -> NexusMobileCommandResult:
        return await asyncio.to_thread(self._sync.type_text, text, timeout=timeout)

    async def swipe(self, start_x: float, start_y: float, end_x: float, end_y: float, *, duration_ms: int = 300, timeout: float = 120.0) -> NexusMobileCommandResult:
        return await asyncio.to_thread(
            self._sync.swipe,
            start_x,
            start_y,
            end_x,
            end_y,
            duration_ms=duration_ms,
            timeout=timeout,
        )

    async def press_back(self, *, timeout: float = 120.0) -> NexusMobileCommandResult:
        return await asyncio.to_thread(self._sync.press_back, timeout=timeout)

    async def open_app(self, package: str, *, timeout: float = 120.0) -> NexusMobileCommandResult:
        return await asyncio.to_thread(self._sync.open_app, package, timeout=timeout)

    async def wait_for_state(self, *, text: str, timeout: float = 30.0) -> NexusMobileCommandResult:
        return await asyncio.to_thread(self._sync.wait_for_state, text=text, timeout=timeout)


class _BillingReporter:
    def __init__(self, context: "NexusRunContext") -> None:
        self._context = context

    @property
    def enabled(self) -> bool:
        return bool(
            self._context.billing_url
            and self._context._billing_token
            and self._context.billing_currency
        )

    @staticmethod
    def _decimal(value: Union[Decimal, int, str], *, field_name: str) -> Decimal:
        if isinstance(value, (float, bool)) or not isinstance(value, (Decimal, int, str)):
            raise TypeError(f"{field_name} must be Decimal, int, or a decimal string")
        try:
            parsed = Decimal(str(value)).quantize(Decimal("0.000001"))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{field_name} must be a valid decimal") from exc
        if not parsed.is_finite() or parsed < 0:
            raise ValueError(f"{field_name} must be a non-negative finite decimal")
        return parsed

    def report(
        self,
        *,
        amount: Union[Decimal, int, str],
        line_items: Sequence[Mapping[str, Any]] = (),
        idempotency_key: str,
    ) -> Dict[str, Any]:
        if not self.enabled:
            raise NexusBillingUnavailable(
                "Agent-reported billing is unavailable for this Run; upgrade Nexus Cloud or use a reported pricing model"
            )
        normalized_amount = self._decimal(amount, field_name="amount")
        if normalized_amount > self._context.billing_max_cost:
            raise ValueError(
                f"amount exceeds the authorized maximum {self._context.billing_max_cost} {self._context.billing_currency}"
            )
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 128:
            raise ValueError("idempotency_key is required and must be at most 128 characters")
        if len(line_items) > 32:
            raise ValueError("line_items must contain at most 32 entries")
        normalized_items: list[dict[str, str]] = []
        item_total = Decimal("0")
        for item in line_items:
            if not isinstance(item, Mapping):
                raise TypeError("each line item must be a mapping")
            item_amount = self._decimal(item.get("amount"), field_name="line_items.amount")
            code = str(item.get("code") or "").strip()
            description = str(item.get("description") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", code):
                raise ValueError(
                    "line_items.code must use letters, digits, dot, underscore, colon, or dash and be at most 64 characters"
                )
            if len(description) > 256:
                raise ValueError("line_items.description must be at most 256 characters")
            item_total += item_amount
            normalized_items.append(
                {
                    "code": code,
                    "description": description,
                    "amount": f"{item_amount:.6f}",
                }
            )
        if normalized_items and item_total != normalized_amount:
            raise ValueError("line item amounts must sum to amount")
        payload = {
            "turn_index": self._context.turn_index,
            "amount": f"{normalized_amount:.6f}",
            "currency": self._context.billing_currency,
            "line_items": normalized_items,
            "idempotency_key": key,
        }
        request = Request(
            self._context.billing_url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Nexus-Billing-Token": self._context._billing_token,
            },
            method="PUT",
        )
        try:
            response = self._context._open_cloud(request)
            with response:
                raw = response.read()
            value = json.loads(raw.decode("utf-8")) if raw else {}
            return dict(value) if isinstance(value, Mapping) else {"accepted": True}
        except HTTPError as exc:
            try:
                raw = exc.read()
                value = json.loads(raw.decode("utf-8")) if raw else {}
                detail = (
                    value.get("detail") or value.get("message")
                ) if isinstance(value, Mapping) else ""
            except Exception:
                detail = ""
            finally:
                exc.close()
            raise NexusBillingReportError(
                str(detail or "Nexus Cloud rejected the final billing report")
            ) from None
        except (URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError) as exc:
            raise NexusBillingReportError(
                f"Nexus Cloud billing report failed: {exc}"
            ) from None


class _UsageReporter:
    def __init__(self, context: "NexusRunContext") -> None:
        self._context = context

    @property
    def enabled(self) -> bool:
        return bool(self._context.usage_url and self._context._interaction_token)

    @staticmethod
    def _tokens(value: int, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        return value

    def report(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        context_window: int,
        cached_input_tokens: int = 0,
        reasoning_tokens: int = 0,
        primary: bool = True,
        event_id: str = "",
    ) -> Dict[str, Any]:
        if not self.enabled:
            raise NexusUsageUnavailable("Model usage reporting is unavailable for this Run")
        model_name = str(model or "").strip()
        if not model_name or len(model_name) > 128:
            raise ValueError("model must be 1-128 characters")
        if isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0:
            raise ValueError("context_window must be a positive integer")
        identifier = str(event_id or uuid.uuid4()).strip()
        if len(identifier) > 128:
            raise ValueError("event_id must be at most 128 characters")
        payload = {
            "event_id": identifier,
            "turn_index": self._context.turn_index,
            "profile_id": self._context.execution.profile,
            "model": model_name,
            "input_tokens": self._tokens(input_tokens, "input_tokens"),
            "output_tokens": self._tokens(output_tokens, "output_tokens"),
            "cached_input_tokens": self._tokens(cached_input_tokens, "cached_input_tokens"),
            "reasoning_tokens": self._tokens(reasoning_tokens, "reasoning_tokens"),
            "context_window": int(context_window),
            "primary": bool(primary),
        }
        try:
            return self._context._interaction_request_url(
                self._context.usage_url, method="POST", payload=payload
            )
        except NexusChatUnavailable as exc:
            raise NexusUsageError("Nexus rejected or could not record model usage") from exc

    def gateway_headers(self, *, event_id: str = "") -> Dict[str, str]:
        """Return short-lived attribution headers for a Nexus model gateway call."""

        if not self.enabled or not self._context.run_id:
            raise NexusUsageUnavailable("Model gateway attribution is unavailable for this Run")
        if not self._context.execution.context_window:
            raise NexusUsageUnavailable("The selected execution profile has no context window")
        identifier = str(event_id or uuid.uuid4()).strip()
        if not identifier or len(identifier) > 128:
            raise ValueError("event_id must be 1-128 characters")
        return {
            "X-Nexus-Agent-Run-Id": self._context.run_id,
            "X-Nexus-Agent-Usage-Token": self._context._interaction_token,
            "X-Nexus-Agent-Usage-Event-Id": identifier,
            "X-Nexus-Agent-Context-Window": str(self._context.execution.context_window),
        }


class _AsyncUsage:
    def __init__(self, sync: _UsageReporter) -> None:
        self._sync = sync

    @property
    def enabled(self) -> bool:
        return self._sync.enabled

    async def report(self, **values: Any) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.report, **values)

    def gateway_headers(self, *, event_id: str = "") -> Dict[str, str]:
        return self._sync.gateway_headers(event_id=event_id)


class _AsyncBilling:
    def __init__(self, sync: _BillingReporter) -> None:
        self._sync = sync

    @property
    def enabled(self) -> bool:
        return self._sync.enabled

    async def report(
        self,
        *,
        amount: Union[Decimal, int, str],
        line_items: Sequence[Mapping[str, Any]] = (),
        idempotency_key: str,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self._sync.report,
            amount=amount,
            line_items=line_items,
            idempotency_key=idempotency_key,
        )


class _AsyncRunContext:
    def __init__(self, context: "NexusRunContext") -> None:
        self._context = context
        self.computer = _AsyncComputer(context.computer)
        self.workspace = _AsyncWorkspace(context.workspace)
        self.terminal = _AsyncTerminal(context.terminal)
        self.display = _AsyncDisplay(context.display)
        self.plan = _AsyncPlan(context.plan)
        self.shell = _AsyncShell(context.shell)
        self.browser = _AsyncBrowser(context.browser)
        self.chat = _AsyncChat(context.chat)
        self.run = _AsyncRunReader(context.run)
        self.project = context.project
        from .inbox import AsyncRunInbox
        self.inbox = AsyncRunInbox(context.inbox)
        self.checkpoint = _AsyncCheckpoint(context.checkpoint)
        self.recovery = _AsyncRecovery(context.recovery)
        self.mobile = _AsyncMobile(context.mobile)
        self.billing = _AsyncBilling(context.billing)
        self.usage = _AsyncUsage(context.usage)
        self.output = _AsyncOutput(context.output)
        self.media = _AsyncMedia(context.media)
        from .files import AsyncRunFiles
        self.files = AsyncRunFiles(context.files)

    async def raise_if_cancelled(self) -> None:
        await asyncio.to_thread(self._context.raise_if_cancelled)

    async def control(self, *, refresh: bool = False) -> Dict[str, Any]:
        return await asyncio.to_thread(self._context.control, refresh=refresh)


class _AsyncOutput:
    def __init__(self, sync: _OutputReporter) -> None:
        self._sync = sync

    async def upload_file(self, path, **options):
        return await asyncio.to_thread(self._sync.upload_file, path, **options)

    async def image(self, image, **metadata):
        return await asyncio.to_thread(self._sync.image, image, **metadata)

    async def write_text(self, path, content, **metadata):
        return await asyncio.to_thread(self._sync.write_text, path, content, **metadata)

    async def ready(self, result=None, **metadata):
        return await asyncio.to_thread(self._sync.ready, result, **metadata)


class NexusRunContext:
    """One Nexus-hosted invocation's fail-open AG-UI reporter."""

    def __init__(
        self,
        *,
        run_id: str = "",
        events_url: str = "",
        token: str = "",
        computer_enabled: bool = False,
        terminal_url: str = "",
        workspace_url: str = "",
        memory_url: str = "",
        workspace_token: str = "",
        workspace_root: str = "",
        output_root: str = "",
        workspace_delegate_url: str = "",
        workspace_delegate_token: str = "",
        workspace_capabilities: Tuple[str, ...] = (),
        browser_enabled: bool = False,
        browser_delegate_url: str = "",
        browser_delegate_token: str = "",
        browser_computer_name: str = "",
        mobile_enabled: bool = False,
        mobile_delegate_url: str = "",
        mobile_delegate_token: str = "",
        mobile_capabilities: Tuple[str, ...] = (),
        interaction_url: str = "",
        context_url: str = "",
        context_token: str = "",
        checkpoint_url: str = "",
        recovery_url: str = "",
        recovery_managed: bool = False,
        recovery_attempt: int = 0,
        recovery_is_replay: bool = False,
        recovery_last_committed_operation: int = 0,
        display_asset_url: str = "",
        interaction_token: str = "",
        interaction_mode: str = "",
        billing_url: str = "",
        billing_token: str = "",
        billing_currency: str = "",
        billing_max_cost: Union[Decimal, int, str] = "0",
        usage_url: str = "",
        execution_profile: str = "",
        execution_model: str = "",
        reasoning_effort: str = "",
        execution_context_window: Optional[int] = None,
        input_files: Sequence[Mapping[str, Any]] = (),
        turn_index: int = 1,
        config: Optional[NexusReportingConfig] = None,
        _event_sink: Optional[Callable[[Mapping[str, Any]], bool]] = None,
        _interaction_request: Optional[
            Callable[[str, str, Optional[Mapping[str, Any]]], Dict[str, Any]]
        ] = None,
        _asset_uploader: Optional[
            Callable[[bytes, str, str, Optional[int], Optional[int]], Dict[str, Any]]
        ] = None,
        _cloud_opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.run_id = str(run_id or "").strip()
        self.events_url = str(events_url or "").strip()
        self._token = str(token or "").strip()
        self.computer_enabled = bool(computer_enabled)
        self.terminal_url = str(terminal_url or "").strip()
        self.workspace_url = str(workspace_url or "").strip()
        self.memory_url = str(memory_url or "").strip()
        self.workspace_root = str(workspace_root or "").strip()
        self.output_root = str(output_root or "").strip()
        self.workspace_delegate_url = str(workspace_delegate_url or "").strip()
        self._workspace_delegate_token = str(workspace_delegate_token or "").strip()
        self.workspace_capabilities = frozenset(
            str(value).strip() for value in workspace_capabilities if str(value).strip()
        )
        self.browser_enabled = bool(browser_enabled)
        self.browser_delegate_url = str(browser_delegate_url or "").strip()
        self._browser_delegate_token = str(browser_delegate_token or "").strip()
        self.browser_computer_name = str(browser_computer_name or "").strip()
        self.mobile_enabled = bool(mobile_enabled)
        self.mobile_delegate_url = str(mobile_delegate_url or "").strip()
        self._mobile_delegate_token = str(mobile_delegate_token or "").strip()
        self.mobile_capabilities = frozenset(
            str(value).strip() for value in mobile_capabilities if str(value).strip()
        )
        self.interaction_url = str(interaction_url or "").strip()
        self.context_url = str(context_url or "").strip()
        self._context_token = str(context_token or "").strip()
        self._run_context_cache: Optional[Dict[str, Any]] = None
        self.checkpoint_url = str(checkpoint_url or "").strip()
        self.recovery_url = str(recovery_url or "").strip()
        self._control_checked_at = 0.0
        self._run_control: Dict[str, Any] = {}
        self.display_asset_url = str(display_asset_url or "").strip()
        self._interaction_token = str(interaction_token or "").strip()
        self.interaction_mode = str(interaction_mode or "").strip().lower()
        self.turn_index = max(int(turn_index or 1), 1)
        self.is_resumed = self.turn_index > 1
        self.billing_url = str(billing_url or "").strip()
        self._billing_token = str(billing_token or "").strip()
        self.billing_currency = str(billing_currency or "").strip().upper()
        self.usage_url = str(usage_url or "").strip()
        self.execution = NexusExecutionSelection(
            profile=str(execution_profile or "").strip(),
            model=str(execution_model or "").strip(),
            reasoning_effort=str(reasoning_effort or "").strip().lower(),
            context_window=(int(execution_context_window) if execution_context_window else None),
        )
        self.input_files = tuple(dict(item) for item in input_files if isinstance(item, Mapping))
        try:
            self.billing_max_cost = Decimal(str(billing_max_cost or "0")).quantize(
                Decimal("0.000001")
            )
        except (InvalidOperation, ValueError):
            self.billing_max_cost = Decimal("0")
        self._workspace_token = str(workspace_token or token or "").strip()
        self._event_sink = _event_sink
        self._local_interaction_request = _interaction_request
        self._asset_uploader = _asset_uploader
        self._cloud_opener = _cloud_opener
        self.config = config or NexusReportingConfig()
        self._stable_id_lock = threading.Lock()
        self._stable_id_sequence = 0
        self.enabled = bool(
            self.run_id
            and (
                (self.events_url and self._token)
                or self._event_sink is not None
            )
        )
        self.trace = _TraceReporter(self)
        self.memory = _MemoryReporter(self)
        self.output = _OutputReporter(self)
        self.media = _MediaReporter(self)
        from .files import RunFiles
        self.files = RunFiles(self)
        self.input = _RunInput(self)
        self.computer = _ComputerReporter(self)
        self.terminal = _TerminalReporter(self)
        self.workspace = _WorkspaceReporter(self)
        self.display = _DisplayReporter(self)
        self.plan = _PlanReporter(self)
        self.shell = _ShellReporter(self)
        self.browser = _BrowserReporter(self)
        self.chat = _ChatReporter(self)
        self.run = _RunContextReader(self)
        self.project = _ProjectContext(self)
        from .inbox import RunInbox
        self.inbox = RunInbox(self)
        self.checkpoint = _CheckpointReporter(self)
        self.recovery = _RecoveryReporter(
            self,
            managed=recovery_managed,
            attempt=recovery_attempt,
            is_replay=recovery_is_replay,
            last_committed_operation=recovery_last_committed_operation,
        )
        self.mobile = _MobileReporter(self)
        self.billing = _BillingReporter(self)
        self.usage = _UsageReporter(self)
        self.aio = _AsyncRunContext(self)
        self._condition = threading.Condition()
        self._pending = 0
        self._sent = 0
        self._failed = 0
        self._dropped = 0
        self._last_error = ""
        self._cancelled = False
        self._outbox = None
        self._outbox_inflight = set()
        directory = self.config.outbox_directory or os.environ.get("NEXUS_AGENT_OUTBOX_DIR", "")
        if directory and self.enabled and self._event_sink is None:
            try:
                from .outbox import EventOutbox
                self._outbox = EventOutbox(directory, self.run_id, self.config.outbox_max_bytes)
                self.replay_pending()
            except (OSError, ValueError):
                self._last_error = "AG-UI outbox is unavailable"

    def __repr__(self) -> str:
        return (
            f"NexusRunContext(run_id={self.run_id!r}, enabled={self.enabled!r}, "
            f"events_url={self.events_url!r})"
        )

    def _stable_local_id(self, namespace: str) -> str:
        """Return a replay-stable opaque ID without exposing Run contents."""

        with self._stable_id_lock:
            self._stable_id_sequence += 1
            sequence = self._stable_id_sequence
        material = f"{self.run_id}:{self.turn_index}:{sequence}:{namespace}".encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()[:32]
        prefix = re.sub(r"[^a-z0-9-]", "-", str(namespace or "item").lower()).strip("-") or "item"
        return f"{prefix}-{digest}"

    @classmethod
    def from_env(
        cls,
        *,
        headers: Optional[Mapping[str, Any]] = None,
        environ: Optional[Mapping[str, str]] = None,
        config: Optional[NexusReportingConfig] = None,
        _cloud_opener: Optional[Callable[..., Any]] = None,
    ) -> "NexusRunContext":
        values = environ if environ is not None else os.environ
        if _cloud_opener is None:
            _cloud_opener = hosted_cloud_opener(values)
        return cls(
            run_id=(
                _header(headers, "X-Nexus-AGUI-Run-Id")
                or str(values.get("NEXUS_AGUI_RUN_ID", ""))
            ),
            events_url=(
                _header(headers, "X-Nexus-AGUI-Events-Url")
                or str(values.get("NEXUS_AGUI_EVENTS_URL", ""))
            ),
            token=(
                _header(headers, "X-Nexus-AGUI-Token")
                or str(values.get("NEXUS_AGUI_TOKEN", ""))
            ),
            computer_enabled=(
                _header(headers, "X-Nexus-Computer-Enabled")
                or str(values.get("NEXUS_COMPUTER_ENABLED", ""))
            ).lower() in {"1", "true", "yes"},
            terminal_url=_header(headers, "X-Nexus-Terminal-Url") or str(values.get("NEXUS_TERMINAL_URL", "")),
            workspace_url=_header(headers, "X-Nexus-Workspace-Url") or str(values.get("NEXUS_WORKSPACE_URL", "")),
            memory_url=_header(headers, "X-Nexus-Memory-Url") or str(values.get("NEXUS_MEMORY_URL", "")),
            workspace_token=_header(headers, "X-Nexus-Workspace-Token") or str(values.get("NEXUS_WORKSPACE_TOKEN", "")),
            workspace_root=_text_header(headers, "X-Nexus-Workspace-Root") or str(values.get("NEXUS_WORKSPACE_ROOT", "")),
            output_root=_text_header(headers, "X-Nexus-Output-Root") or str(values.get("NEXUS_OUTPUT_ROOT", "")),
            workspace_delegate_url=(
                _header(headers, "X-Nexus-Workspace-Delegate-Url")
                or str(values.get("NEXUS_WORKSPACE_DELEGATE_URL", ""))
            ),
            workspace_delegate_token=(
                _header(headers, "X-Nexus-Workspace-Delegate-Token")
                or str(values.get("NEXUS_WORKSPACE_DELEGATE_TOKEN", ""))
            ),
            workspace_capabilities=tuple(
                item.strip()
                for item in (
                    _header(headers, "X-Nexus-Workspace-Capabilities")
                    or str(values.get("NEXUS_WORKSPACE_CAPABILITIES", ""))
                ).split(",")
                if item.strip()
            ),
            browser_enabled=(
                _header(headers, "X-Nexus-Browser-Enabled")
                or str(values.get("NEXUS_BROWSER_ENABLED", ""))
            ).lower() in {"1", "true", "yes"},
            browser_delegate_url=(
                _header(headers, "X-Nexus-Browser-Delegate-Url")
                or str(values.get("NEXUS_BROWSER_DELEGATE_URL", ""))
            ),
            browser_delegate_token=(
                _header(headers, "X-Nexus-Browser-Delegate-Token")
                or str(values.get("NEXUS_BROWSER_DELEGATE_TOKEN", ""))
            ),
            browser_computer_name=(
                _text_header(headers, "X-Nexus-Browser-Computer-Name")
                or str(values.get("NEXUS_BROWSER_COMPUTER_NAME", ""))
            ),
            mobile_enabled=(
                _header(headers, "X-Nexus-Mobile-Enabled")
                or str(values.get("NEXUS_MOBILE_ENABLED", ""))
            ).lower() in {"1", "true", "yes"},
            mobile_delegate_url=(
                _header(headers, "X-Nexus-Mobile-Delegate-Url")
                or str(values.get("NEXUS_MOBILE_DELEGATE_URL", ""))
            ),
            mobile_delegate_token=(
                _header(headers, "X-Nexus-Mobile-Delegate-Token")
                or str(values.get("NEXUS_MOBILE_DELEGATE_TOKEN", ""))
            ),
            mobile_capabilities=tuple(
                item.strip()
                for item in (
                    _header(headers, "X-Nexus-Mobile-Capabilities")
                    or str(values.get("NEXUS_MOBILE_CAPABILITIES", ""))
                ).split(",")
                if item.strip()
            ),
            interaction_url=(
                _header(headers, "X-Nexus-Interaction-Url")
                or str(values.get("NEXUS_INTERACTION_URL", ""))
            ),
            context_url=(
                _header(headers, "X-Nexus-Context-Url")
                or str(values.get("NEXUS_CONTEXT_URL", ""))
            ),
            context_token=(
                _header(headers, "X-Nexus-Context-Token")
                or str(values.get("NEXUS_CONTEXT_TOKEN", ""))
            ),
            checkpoint_url=(
                _header(headers, "X-Nexus-Checkpoint-Url")
                or str(values.get("NEXUS_CHECKPOINT_URL", ""))
            ),
            recovery_url=(
                _header(headers, "X-Nexus-Recovery-Url")
                or str(values.get("NEXUS_RECOVERY_URL", ""))
            ),
            recovery_managed=(
                _header(headers, "X-Nexus-Recovery-Managed")
                or str(values.get("NEXUS_RECOVERY_MANAGED", ""))
            ).lower() in {"1", "true", "yes"},
            recovery_attempt=int(
                _header(headers, "X-Nexus-Recovery-Attempt")
                or str(values.get("NEXUS_RECOVERY_ATTEMPT", "0"))
                or 0
            ),
            recovery_is_replay=(
                _header(headers, "X-Nexus-Recovery-Replay")
                or str(values.get("NEXUS_RECOVERY_REPLAY", ""))
            ).lower() in {"1", "true", "yes"},
            recovery_last_committed_operation=int(
                _header(headers, "X-Nexus-Recovery-Last-Committed")
                or str(values.get("NEXUS_RECOVERY_LAST_COMMITTED", "0"))
                or 0
            ),
            display_asset_url=(
                _header(headers, "X-Nexus-Display-Asset-Url")
                or str(values.get("NEXUS_DISPLAY_ASSET_URL", ""))
            ),
            interaction_token=(
                _header(headers, "X-Nexus-Interaction-Token")
                or str(values.get("NEXUS_INTERACTION_TOKEN", ""))
            ),
            interaction_mode=(
                _header(headers, "X-Nexus-Interaction-Mode")
                or str(values.get("NEXUS_INTERACTION_MODE", ""))
            ),
            billing_url=(
                _header(headers, "X-Nexus-Billing-Url")
                or str(values.get("NEXUS_BILLING_URL", ""))
            ),
            billing_token=(
                _header(headers, "X-Nexus-Billing-Token")
                or str(values.get("NEXUS_BILLING_TOKEN", ""))
            ),
            billing_currency=(
                _header(headers, "X-Nexus-Billing-Currency")
                or str(values.get("NEXUS_BILLING_CURRENCY", ""))
            ),
            billing_max_cost=(
                _header(headers, "X-Nexus-Billing-Max-Cost")
                or str(values.get("NEXUS_BILLING_MAX_COST", "0"))
            ),
            usage_url=(
                _header(headers, "X-Nexus-Usage-Url")
                or str(values.get("NEXUS_USAGE_URL", ""))
            ),
            execution_profile=(
                _header(headers, "X-Nexus-Execution-Profile")
                or str(values.get("NEXUS_EXECUTION_PROFILE", ""))
            ),
            execution_model=(
                _header(headers, "X-Nexus-Execution-Model")
                or str(values.get("NEXUS_EXECUTION_MODEL", ""))
            ),
            reasoning_effort=(
                _header(headers, "X-Nexus-Reasoning-Effort")
                or str(values.get("NEXUS_REASONING_EFFORT", ""))
            ),
            execution_context_window=_optional_int(
                _header(headers, "X-Nexus-Execution-Context-Window")
                or str(values.get("NEXUS_EXECUTION_CONTEXT_WINDOW", ""))
            ),
            input_files=_json_list(
                _header(headers, "X-Nexus-Input-Files")
                or str(values.get("NEXUS_INPUT_FILES", ""))
            ),
            turn_index=(
                _optional_int(
                    _header(headers, "X-Nexus-Run-Turn")
                    or str(values.get("NEXUS_RUN_TURN", ""))
                )
                or 1
            ),
            config=config,
            _cloud_opener=_cloud_opener,
        )

    @classmethod
    def from_exchange(
        cls,
        exchange_url: str,
        exchange_token: str,
        *,
        config: Optional[NexusReportingConfig] = None,
        cloud_opener: Optional[Callable[..., Any]] = None,
    ) -> "NexusRunContext":
        url = str(exchange_url or "").strip()
        token = str(exchange_token or "").strip()
        if not url or not token:
            raise NexusRunContextExchangeError("Nexus Run context exchange is unavailable")
        try:
            parsed_url = urlsplit(url)
            parsed_url.port  # Validate an explicitly supplied port.
            valid_https = bool(
                parsed_url.scheme.lower() == "https"
                and parsed_url.hostname
                and parsed_url.username is None
                and parsed_url.password is None
            )
        except ValueError:
            valid_https = False
        if not valid_https:
            raise NexusRunContextExchangeError(
                "Nexus Run context exchange must use HTTPS",
                code="RUN_CONTEXT_TLS_FAILED",
            )
        request = Request(
            url,
            data=b"{}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Connection": "close",
                "X-Nexus-Run-Context-Token": token,
            },
            method="POST",
        )
        timeout = (config or NexusReportingConfig()).request_timeout
        try:
            response = cloud_opener(request, timeout=timeout, exchange=True) \
                if cloud_opener is not None else urlopen(request, timeout=timeout)
            with response:
                raw = response.read()
            value = json.loads(raw.decode("utf-8")) if raw else {}
            headers = value.get("context") if isinstance(value, Mapping) else None
            if headers is None and isinstance(value, Mapping):
                data = value.get("data")
                headers = data.get("context") if isinstance(data, Mapping) else None
            if not isinstance(headers, Mapping):
                raise ValueError("context is missing")
        except CloudTrustUnavailableError as exc:
            raise NexusRunContextExchangeError(
                str(exc), code="RUN_CONTEXT_TRUST_UNAVAILABLE"
            ) from None
        except (CloudTrustVerificationError, CloudTrustOriginError) as exc:
            raise NexusRunContextExchangeError(
                str(exc), code="RUN_CONTEXT_TLS_FAILED"
            ) from None
        except HTTPError as exc:
            exc.close()
            raise NexusRunContextExchangeError(
                "Nexus Run context exchange was rejected or unavailable"
            ) from None
        except (URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError):
            raise NexusRunContextExchangeError(
                "Nexus Run context exchange was rejected or unavailable"
            ) from None
        context = cls.from_env(
            headers=headers,
            environ={},
            config=config,
            _cloud_opener=cloud_opener,
        )
        if not context.enabled:
            raise NexusRunContextExchangeError("Nexus Run context exchange returned no active Run")
        return context

    def _open_cloud(self, request: Request, *, timeout: Optional[float] = None):
        request_timeout = self.config.request_timeout if timeout is None else float(timeout)
        if self._cloud_opener is not None:
            return self._cloud_opener(
                request, timeout=request_timeout, exchange=False
            )
        return urlopen(request, timeout=request_timeout)

    def _load_run_context(self, *, limit: int = 200, refresh: bool = False) -> Dict[str, Any]:
        if self._run_context_cache is not None and not refresh:
            return dict(self._run_context_cache)
        if not self.context_url or not self._context_token:
            raise NexusRunContextUnavailable("Run context is unavailable")
        url = self.context_url + "?" + urlencode({"limit": min(max(int(limit), 1), 200)})
        request = Request(
            url,
            headers={"Accept": "application/json", "X-Nexus-Context-Token": self._context_token},
            method="GET",
        )
        try:
            with self._open_cloud(request) as response:
                raw = response.read()
            value = _nexus_response_data(json.loads(raw.decode("utf-8")) if raw else {})
            if not isinstance(value, Mapping):
                raise ValueError()
            self._run_context_cache = dict(value)
            return dict(self._run_context_cache)
        except HTTPError as exc:
            exc.close()
            raise NexusRunContextUnavailable("Run context was rejected by Nexus") from None
        except (URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError):
            raise NexusRunContextUnavailable("Run context is unavailable") from None

    def _interaction_request(
        self,
        path: str,
        *,
        method: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if self._local_interaction_request is not None:
            return self._local_interaction_request(path, method, payload)
        if not self.interaction_url:
            raise NexusChatUnavailable("Interactive Chat is unavailable for this Run")
        return self._interaction_request_url(
            urljoin(self.interaction_url.rstrip("/") + "/", str(path).lstrip("/")),
            method=method,
            payload=payload,
        )

    def _interaction_request_url(
        self,
        url: str,
        *,
        method: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not url or not self._interaction_token:
            raise NexusChatUnavailable("Interactive control is unavailable for this Run")
        body = None if payload is None else json.dumps(
            dict(payload), separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Nexus-Interaction-Token": self._interaction_token,
            },
            method=method,
        )
        try:
            with self._open_cloud(request) as response:
                raw = response.read()
                value = json.loads(raw.decode("utf-8")) if raw else {}
                return _nexus_response_data(value)
        except HTTPError as exc:
            error = _safe_http_error(exc)
            code = str(error.get("code") or "")
            if code == "INTERACTIVE_TRANSPORT_REQUIRED":
                raise NexusChatUnavailable(
                    "Interactive Chat requires Streamable HTTP/SSE or an MCP Task"
                ) from None
            if code == "RUN_CANCELLED":
                raise NexusRunCancelled("The Nexus Run was cancelled") from None
            if code == "CHECKPOINT_REVISION_CONFLICT":
                raise NexusCheckpointError("Checkpoint was modified by another execution") from None
            if code == "RECOVERY_DIVERGED":
                raise NexusRecoveryDiverged("Recovered execution diverged from its operation journal") from None
            if code == "MANAGED_RECOVERY_UNAVAILABLE":
                raise NexusRecoveryError("Managed recovery is unavailable for this Agent version") from None
            if "checkpoint" in url.lower():
                raise NexusCheckpointError("Checkpoint operation was rejected by Nexus") from None
            raise NexusChatUnavailable("Interactive Chat was rejected by Nexus") from None
        except (URLError, TimeoutError, OSError, ValueError):
            if "checkpoint" in url.lower():
                raise NexusCheckpointError("Nexus Checkpoint service is unavailable") from None
            raise NexusChatUnavailable("Nexus interactive service is unavailable") from None

    def _upload_display_asset(
        self,
        *,
        content: bytes,
        content_type: str,
        file_name: str,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> Dict[str, Any]:
        if self._asset_uploader is not None:
            return self._asset_uploader(
                bytes(content),
                str(content_type),
                str(file_name or "frame"),
                width,
                height,
            )
        if not self.display_asset_url or not self._interaction_token:
            raise NexusChatUnavailable("Display Asset upload is unavailable for this Run")
        return self._interaction_request_url(
            self.display_asset_url,
            method="POST",
            payload={
                "file_name": str(file_name or "frame"),
                "content_type": str(content_type),
                "content_base64": base64.b64encode(content).decode("ascii"),
                "sha256": hashlib.sha256(content).hexdigest(),
                "width": width,
                "height": height,
            },
        )

    def _require_workspace_capability(self, capability: str) -> None:
        legacy_run_capability = capability in {
            "files.list", "files.read", "files.write", "command.execute"
        }
        if (
            legacy_run_capability
            and not self.workspace_delegate_url
            and self.computer_enabled
            and self._workspace_token
        ):
            return
        if (
            not self.workspace_delegate_url
            or not self._workspace_delegate_token
            or capability not in self.workspace_capabilities
        ):
            raise NexusComputerError("Workspace capability is not authorized for this run")

    def _delegate_request(
        self,
        path: str,
        *,
        method: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.workspace_delegate_url or not self._workspace_delegate_token:
            raise NexusComputerError("Workspace delegation is unavailable for this run")
        body = None if payload is None else json.dumps(
            dict(payload), separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        request = Request(
            urljoin(self.workspace_delegate_url, path),
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Nexus-Workspace-Delegate-Token": self._workspace_delegate_token,
            },
            method=method,
        )
        try:
            with self._open_cloud(request) as response:
                raw = response.read()
                value = json.loads(raw.decode("utf-8")) if raw else {}
                return _nexus_response_data(value)
        except HTTPError as exc:
            code = str(_safe_http_error(exc).get("code") or "")
            raise NexusComputerError("Workspace operation was not authorized or available",
                code=code if code in _DELEGATE_FAILURE_CODES else "WORKSPACE_UNAVAILABLE") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise NexusComputerError("Nexus Workspace service is unavailable", code="RUN_DELEGATE_UNAVAILABLE") from None

    def _mobile_request(
        self,
        path: str,
        *,
        method: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.mobile_delegate_url or not self._mobile_delegate_token:
            raise NexusMobileUnavailable("Mobile delegation is unavailable for this Run")
        body = None if payload is None else json.dumps(
            dict(payload), separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        request = Request(
            urljoin(self.mobile_delegate_url.rstrip("/") + "/", str(path).lstrip("/")),
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Nexus-Mobile-Delegate-Token": self._mobile_delegate_token,
            },
            method=method,
        )
        try:
            with self._open_cloud(request) as response:
                raw = response.read()
                value = json.loads(raw.decode("utf-8")) if raw else {}
                return _nexus_response_data(value)
        except HTTPError as exc:
            code = str(_safe_http_error(exc).get("code") or "")
            if code == "MOBILE_PERMISSION_REQUIRED":
                raise NexusMobilePermissionRequired("Mobile capability is not authorized") from None
            if code == "MOBILE_BUSY":
                raise NexusMobileBusy("Mobile is being controlled by another Run") from None
            if code == "INTERACTIVE_TRANSPORT_REQUIRED":
                raise NexusMobileUnavailable("This Mobile action requires SSE or an MCP Task") from None
            raise NexusMobileUnavailable("Mobile operation was rejected or unavailable") from None
        except (URLError, TimeoutError, OSError):
            raise _NexusMobileTransportUnavailable(
                "Nexus Mobile service is temporarily unavailable"
            ) from None
        except ValueError:
            raise NexusMobileUnavailable("Nexus Mobile service is unavailable") from None

    def _browser_request_raw(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str = "",
    ) -> Dict[str, Any]:
        if (
            not self.browser_enabled
            or not self.browser_delegate_url
            or not self._browser_delegate_token
        ):
            raise NexusBrowserUnavailable(
                "Attached Computer browser is unavailable for this Run"
            )
        body = json.dumps(
            {
                "operation": str(operation),
                **dict(payload),
                **({"idempotency_key": idempotency_key} if idempotency_key else {}),
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            self.browser_delegate_url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Nexus-Browser-Delegate-Token": self._browser_delegate_token,
            },
            method="POST",
        )
        try:
            with self._open_cloud(request, timeout=60) as response:
                raw = response.read()
                value = json.loads(raw.decode("utf-8")) if raw else {}
                return _nexus_response_data(value)
        except HTTPError as exc:
            failure = _safe_http_error(exc)
            code = str(failure.get("code") or "")
            message = re.sub(r"[\r\n\t]+", " ", str(failure.get("message") or "")).strip()[:300]
            if code == "BROWSER_ACTION_FAILED":
                raise NexusBrowserActionFailed(message or "Attached Computer browser action failed") from None
            if code == "BROWSER_STALE_OBSERVATION":
                raise NexusBrowserStaleObservation(
                    message or "Browser observation is stale; observe the page again"
                ) from None
            if code == "BROWSER_UNAVAILABLE":
                raise NexusBrowserUnavailable(
                    message or "Browser automation is unavailable on this Computer"
                ) from None
            if code == "BROWSER_SESSION_LOST":
                raise NexusBrowserSessionLost("Attached Computer browser session was lost") from None
            if code == "BROWSER_COMPUTER_REQUIRED":
                raise NexusBrowserComputerRequired("Attach a Computer before using this browser Agent") from None
            if code == "BROWSER_PERMISSION_REQUIRED":
                raise NexusBrowserPermissionRequired("browser.control is not authorized for this Run") from None
            if code == "BROWSER_TUNNEL_UNAVAILABLE":
                raise NexusBrowserTunnelUnavailable("The protected Attached Computer browser tunnel is unavailable") from None
            if code in _DELEGATE_FAILURE_CODES:
                error = NexusBrowserUnavailable("The attached Computer could not provide the browser operation")
                error.code = code
                raise error from None
            raise NexusBrowserUnavailable(
                "Attached Computer browser was rejected or unavailable"
            ) from None
        except (URLError, TimeoutError, OSError, ValueError):
            error = NexusBrowserUnavailable(
                "Attached Computer browser service is unavailable"
            )
            error.code = "RUN_DELEGATE_UNAVAILABLE"
            raise error from None

    def _browser_request(
        self,
        operation: str,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Execute one attached-browser operation with a stable Runtime key.

        Browser observations contain screenshots and can be much larger than
        the operation journal's deliberately small result envelope.  The
        journal therefore stores only completion metadata; replay asks the
        Computer Runtime for the result under the same idempotency key.  This
        prevents a click/fill/type action from being executed twice while
        still returning the full observation to the handler.
        """

        normalized_operation = str(operation or "").strip().lower()
        if not self.recovery.managed:
            return self._browser_request_raw(normalized_operation, payload)
        prepared = self.recovery._prepare(
            f"browser.{normalized_operation}",
            {"operation": normalized_operation, "payload": dict(payload)},
            can_reconcile=True,
        )
        operation_id = str(prepared.get("id") or "")
        idempotency_key = str(prepared.get("idempotency_key") or "")
        try:
            result = self._browser_request_raw(
                normalized_operation,
                payload,
                idempotency_key=idempotency_key,
            )
        except BaseException:
            if prepared.get("execute", True):
                self.recovery._finish(
                    operation_id,
                    status="failed",
                    error_code="BROWSER_OPERATION_INTERRUPTED",
                )
            raise
        if prepared.get("execute", True):
            self.recovery._finish(
                operation_id,
                status="succeeded",
                result={
                    "operation": normalized_operation,
                    "observation_id": str(result.get("observation_id") or "")[:128],
                    "revision": max(int(result.get("revision") or 0), 0),
                },
            )
        return result

    def _apply_computer_binding(self, value: Mapping[str, Any]) -> None:
        self.computer_enabled = bool(value.get("computer_enabled"))
        self.workspace_root = str(value.get("workspace_root") or self.workspace_root)
        self.output_root = str(value.get("output_root") or self.output_root)
        if value.get("workspace_path"):
            self.workspace_url = urljoin(self.workspace_delegate_url, str(value["workspace_path"]))
        if value.get("terminal_path"):
            self.terminal_url = urljoin(self.workspace_delegate_url, str(value["terminal_path"]))

    def _internal_request(
        self,
        url: str,
        *,
        method: str,
        payload: Optional[Mapping[str, Any]] = None,
        request_timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        token = self._workspace_delegate_token or self._workspace_token
        if not url or not token:
            raise NexusComputerError("Run-scoped Nexus endpoint is unavailable")
        body = None if payload is None else json.dumps(dict(payload), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                # The delegate token is the authority for caller-owned
                # Computer operations.  Send both Nexus header spellings for
                # mixed Cloud/Edge versions and an Authorization fallback for
                # HTTPS front doors that do not preserve custom headers.
                "X-Nexus-Workspace-Token": token,
                "X-Nexus-Workspace-Delegate-Token": token,
                "Authorization": "Bearer " + token,
            },
            method=method,
        )
        try:
            with self._open_cloud(request, timeout=request_timeout) as response:
                raw = response.read()
                value = json.loads(raw.decode("utf-8")) if raw else {}
                return _nexus_response_data(value)
        except HTTPError as exc:
            try:
                value = json.loads(exc.read(65536).decode("utf-8"))
                error = value.get("error") if isinstance(value, dict) else {}
                code = str((error or {}).get("code") or "") if isinstance(error, dict) else ""
            except (ValueError, TypeError, OSError):
                code = ""
            finally:
                exc.close()
            # Return only recognized diagnostic codes, never arbitrary remote
            # config, file content, delegate tokens or authorization headers.
            if not re.fullmatch(r"(?:WORKSPACE|COMPUTER)_[A-Z_]{1,80}", code):
                code = "WORKSPACE_UNAVAILABLE"
            raise NexusComputerError("Nexus run endpoint returned HTTP " + str(exc.code) + " (" + code + ")", code=code) from None
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise NexusComputerError("Nexus run endpoint is unavailable: " + type(exc).__name__) from None

    def _memory_request(
        self,
        url: str,
        *,
        method: str,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        token = self._workspace_delegate_token or self._workspace_token
        if not self.memory_url or not token:
            raise NexusMemoryUnavailable("Run-scoped Memory service is unavailable")
        body = json.dumps(dict(payload), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Nexus-Workspace-Token": token,
                "X-Nexus-Workspace-Delegate-Token": token,
                "Authorization": "Bearer " + token,
            },
            method=method,
        )
        try:
            with self._open_cloud(request) as response:
                raw = response.read()
                value = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(value, Mapping):
                    return {}
                data = value.get("data") if value.get("ok") is True else value
                return dict(data) if isinstance(data, Mapping) else {}
        except HTTPError as exc:
            error = _safe_http_error(exc)
            if error.get("code") == "MEMORY_REVISION_CONFLICT":
                try:
                    current_revision = int(error.get("current_revision"))
                except (TypeError, ValueError):
                    current_revision = 0
                raise NexusMemoryConflict(current_revision=current_revision) from None
            if exc.code in {404, 409}:
                raise NexusMemoryUnavailable("Memory operation is unavailable for this Run") from None
            raise NexusMemoryError("Memory operation was rejected by Nexus") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise NexusMemoryUnavailable("Nexus Memory service is unavailable") from None

    def control(self, *, refresh: bool = False) -> Dict[str, Any]:
        """Read server deadline/lease/cancellation; no-op outside hosted runs.

        Network failure does not invent a cancellation acknowledgement. Platform
        delegates independently enforce the server lease and permission expiry.
        """
        if not self.checkpoint_url or not self._interaction_token:
            return {"managed": False}
        now = time.monotonic()
        if refresh or now - self._control_checked_at >= self.config.control_poll_interval:
            self._control_checked_at = now
            try:
                self._run_control = self._interaction_request_url(
                    urljoin(self.checkpoint_url, "../control/"), method="GET")
            except NexusChatUnavailable:
                return {**self._run_control, "available": False}
        return dict(self._run_control)

    def raise_if_cancelled(self) -> None:
        """Cooperative cancellation point for long loops and between side effects."""
        state = self.control()
        if state.get("managed") and (
            state.get("cancel_requested") or state.get("remaining_seconds") == 0
            or not state.get("lease_active", True)
        ):
            raise NexusRunCancelled("The Nexus task was cancelled or its execution lease ended")

    def emit(
        self,
        event: Any,
        *,
        visibility: str = "public",
        event_id: Optional[str] = None,
    ) -> bool:
        if not self.enabled:
            return False
        self.raise_if_cancelled()
        normalized = _event_mapping(event)
        normalized["visibility"] = str(visibility or "public")
        prepared_operation: Optional[Dict[str, Any]] = None
        if self.recovery.managed and self._event_sink is None:
            try:
                prepared_operation = self.recovery._prepare(
                    "display.event",
                    {"event": normalized, "requested_event_id": str(event_id or "")},
                    can_reconcile=True,
                )
                if not prepared_operation.get("execute", True):
                    return bool((prepared_operation.get("result") or {}).get("accepted", True))
            except NexusRecoveryError:
                # Display reporting remains fail-open. Other managed operations
                # and the worker lease still prevent unsafe business commits.
                prepared_operation = None
        if self._event_sink is not None:
            try:
                accepted = bool(self._event_sink(normalized))
            except Exception:
                with self._condition:
                    self._failed += 1
                    self._last_error = "Direct Invoke event delivery failed"
                return False
            with self._condition:
                if accepted:
                    self._sent += 1
                else:
                    self._dropped += 1
            return accepted
        try:
            body = json.dumps(
                normalized,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("AG-UI event must contain finite JSON values") from exc
        identifier = str(
            event_id
            or (prepared_operation or {}).get("idempotency_key")
            or uuid.uuid4().hex
        )
        outbox_path = None
        if self._outbox is not None:
            try:
                outbox_path = self._outbox.append(identifier, body)
            except (OSError, ValueError):
                with self._condition:
                    self._last_error = "AG-UI outbox is full or unavailable"
        delivery = _Delivery(
            context=self,
            event_id=identifier,
            body=body,
            outbox_path=outbox_path,
        )
        with self._condition:
            if self._cancelled:
                self._dropped += 1
                return False
            self._pending += 1
            if outbox_path:
                self._outbox_inflight.add(outbox_path)
        if not _dispatcher(self.config.queue_size).submit(delivery):
            with self._condition:
                self._pending -= 1
                self._dropped += 1
                self._last_error = "AG-UI delivery queue is full"
                self._outbox_inflight.discard(outbox_path)
                self._condition.notify_all()
            return False
        if prepared_operation is not None:
            try:
                self.recovery._finish(
                    str(prepared_operation["id"]),
                    status="succeeded",
                    result={"accepted": True, "event_id": identifier},
                )
            except NexusRecoveryError:
                pass
        return True

    def replay_pending(self) -> int:
        """Requeue unacknowledged IDs for this Run while its lease is active.

        Does not resurrect completed Runs or acknowledge delivery without a 2xx.
        File retention/volume backup remain the operator's responsibility.
        """
        if self._outbox is None or self._cancelled:
            return 0
        count = 0
        for path, identifier, body in self._outbox.records():
            with self._condition:
                if path in self._outbox_inflight:
                    continue
                self._pending += 1
                self._outbox_inflight.add(path)
            if not _dispatcher(self.config.queue_size).submit(_Delivery(self, identifier, body, path)):
                with self._condition:
                    self._pending -= 1
                    self._outbox_inflight.discard(path)
                break
            count += 1
        return count

    def flush(self, timeout: Optional[float] = None) -> DeliveryReport:
        if not self.enabled:
            return self.report()
        maximum = self.config.flush_timeout if timeout is None else max(timeout, 0.0)
        deadline = time.monotonic() + maximum
        with self._condition:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._cancelled = True
                    self._dropped += self._pending
                    self._pending = 0
                    self._last_error = "AG-UI flush timed out"
                    break
                self._condition.wait(remaining)
        return self.report()

    def close(self) -> DeliveryReport:
        self.inbox.close()
        report = self.flush()
        with self._condition:
            self._cancelled = True
        return report

    def report(self) -> DeliveryReport:
        with self._condition:
            try:
                buffered = len(self._outbox.records()) if self._outbox else 0
            except (OSError, ValueError):
                buffered = 0
                self._last_error = "AG-UI outbox diagnostics unavailable"
            return DeliveryReport(
                enabled=self.enabled,
                sent=self._sent,
                failed=self._failed,
                dropped=self._dropped,
                pending=self._pending,
                last_error=self._last_error,
                buffered=buffered,
            )

    def _deliver(self, delivery: _Delivery) -> None:
        with self._condition:
            if self._cancelled:
                return
        error = ""
        success = False
        for attempt in range(self.config.max_retries + 1):
            if self._outbox and delivery.outbox_path and self._outbox.oldest() != delivery.outbox_path:
                error = "AG-UI waiting for an earlier outbox event"
                break
            request = Request(
                self.events_url,
                data=delivery.body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + self._token,
                    "X-Nexus-AGUI-Event-Id": delivery.event_id,
                },
                method="POST",
            )
            try:
                with self._open_cloud(request) as response:
                    response.read()
                    success = 200 <= response.status < 300
                    if success:
                        break
                    error = "AG-UI endpoint returned HTTP " + str(response.status)
            except HTTPError as exc:
                error = "AG-UI endpoint returned HTTP " + str(exc.code)
                if 400 <= exc.code < 500 and exc.code != 429:
                    break
            except (URLError, TimeoutError, OSError) as exc:
                error = "AG-UI endpoint is unavailable: " + type(exc).__name__
            except HostedCloudTrustError as exc:
                error = exc.code
                break
            if attempt < self.config.max_retries:
                time.sleep(self.config.retry_delay * (2 ** attempt))
        with self._condition:
            if self._cancelled:
                return
            self._pending = max(self._pending - 1, 0)
            self._outbox_inflight.discard(delivery.outbox_path)
            if success:
                self._sent += 1
                if self._outbox and delivery.outbox_path:
                    try:
                        self._outbox.ack(delivery.outbox_path)
                    except OSError:
                        self._last_error = "AG-UI delivered; outbox acknowledgement unavailable"
            else:
                self._failed += 1
                self._last_error = error or "AG-UI delivery failed"
            self._condition.notify_all()


def current_run() -> NexusRunContext:
    context = _CURRENT_RUN.get()
    return context if context is not None else NexusRunContext()


def set_current_run(context: NexusRunContext) -> Token:
    return _CURRENT_RUN.set(context)


def reset_current_run(token: Token) -> None:
    _CURRENT_RUN.reset(token)


@contextmanager
def managed_run_context(
    *,
    headers: Optional[Mapping[str, Any]] = None,
    config: Optional[NexusReportingConfig] = None,
) -> Iterator[NexusRunContext]:
    context = NexusRunContext.from_env(headers=headers, config=config)
    token = set_current_run(context)
    try:
        yield context
    finally:
        reset_current_run(token)
        context.close()


__all__ = [
    "DeliveryReport",
    "MemoryDeleteResult",
    "NexusBillingLineItem",
    "NexusBillingReportError",
    "NexusBillingUnavailable",
    "NexusReportingConfig",
    "NexusComputerError",
    "NexusChatError",
    "NexusChatReply",
    "NexusChatTimeout",
    "NexusChatUnavailable",
    "NexusCheckpoint",
    "NexusCheckpointError",
    "NexusMemoryConflict",
    "NexusMemoryError",
    "NexusMemoryItem",
    "NexusMemoryUnavailable",
    "NexusRunContext",
    "NexusRunCancelled",
    "current_run",
    "managed_run_context",
]
