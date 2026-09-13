"""Optional FastMCP 3.x integration for a callable Nexus Agent.

The base SDK remains dependency-free.  Importing this module is safe without
FastMCP installed; a bridge only loads FastMCP when it is started.
"""

import asyncio
import base64
import json
import math
import queue
import re
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from contextlib import AsyncExitStack, contextmanager
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
from uuid import UUID

from .client import AgentLease, NexusAgentClient
from .models import (
    AgentEnvelope,
    CapabilityRegistration,
    CloudRegistrationManifest,
    McpToolDescriptor,
    NexusExecutionProfile,
    MOBILE_SCOPES,
    SseEvent,
)
from .server import AgentRequestError, NexusAgentServer
from .reporting import (
    NexusReportingConfig,
    NexusRunContext,
    reset_current_run,
    set_current_run,
)


class NexusMCPFeedback:
    """Mirror request feedback to the MCP client and the private Nexus Display."""

    def __init__(self, mcp_context: Any, run: NexusRunContext) -> None:
        self._mcp_context = mcp_context
        self._run = run

    @staticmethod
    def _text(value: Any) -> str:
        text = str(value or "")[:1024]
        text = re.sub(
            r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}",
            "Bearer [redacted]",
            text,
        )
        text = re.sub(
            r"\b(?:sk-|github_pat_|xox[bp]-)[A-Za-z0-9_-]{8,}",
            "[redacted]",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"(?i)\b(password|passwd|token|secret|api[_-]?key|private[_-]?key)"
            r"(\s*(?:=|:)\s*|\s+)([^\s]+)",
            r"\1\2[redacted]",
            text,
        )
        return text

    async def progress(
        self,
        progress: float,
        total: Optional[float] = None,
        message: str = "",
    ) -> None:
        safe_message = self._text(message)
        try:
            await self._mcp_context.report_progress(
                progress=progress,
                total=total,
                message=safe_message or None,
            )
        except Exception:
            pass
        self._run.emit({
            "type": "CUSTOM",
            "name": "nexus.mcp.progress",
            "value": {
                "progress": progress,
                "total": total,
                "message": safe_message,
            },
        })

    async def log(self, level: str, message: str) -> None:
        normalized = str(level or "info").lower()
        if normalized not in {"debug", "info", "warning", "error"}:
            normalized = "info"
        safe_message = self._text(message)
        try:
            logger = getattr(self._mcp_context, normalized)
            await logger(safe_message)
        except Exception:
            pass
        self._run.emit({
            "type": "CUSTOM",
            "name": "nexus.mcp.log",
            "value": {"level": normalized, "message": safe_message},
        })


class NexusMCPContext:
    """One FastMCP request's Nexus run, Workspace, Terminal and feedback APIs."""

    def __init__(self, mcp_context: Any, run: NexusRunContext) -> None:
        self.run = run
        self.project = run.project
        self.computer = run.aio.computer
        self.workspace = run.aio.workspace
        self.terminal = run.aio.terminal
        self.plan = run.aio.plan
        self.shell = run.aio.shell
        self.browser = run.aio.browser
        self.chat = run.aio.chat
        self.checkpoint = run.aio.checkpoint
        self.mobile = run.aio.mobile
        self.trace = run.trace
        self.memory = run.memory
        self.output = run.output
        self.feedback = NexusMCPFeedback(mcp_context, run)


def CurrentNexusMCP(config: Optional[NexusReportingConfig] = None) -> Any:
    """Inject a combined FastMCP and Nexus hosted-run context."""

    try:
        from fastmcp.dependencies import Depends
        from fastmcp.server.dependencies import get_context, get_http_headers
    except ImportError as exc:
        raise FastMCPBridgeError(
            "FastMCP support is not installed; install "
            "'nexus-openwrt-agent-sdk[fastmcp]' on Python 3.10+"
        ) from exc

    @contextmanager
    def dependency() -> Iterator[NexusMCPContext]:
        run = NexusRunContext.from_env(headers=get_http_headers(), config=config)
        token = set_current_run(run)
        try:
            yield NexusMCPContext(get_context(), run)
        finally:
            reset_current_run(token)
            run.close()

    return Depends(dependency)


def CurrentNexusRun(config: Optional[NexusReportingConfig] = None) -> Any:
    """Inject a request-scoped Nexus run into a FastMCP tool.

    FastMCP remains optional: its dependency APIs are imported only when this
    helper is called.
    """

    try:
        from fastmcp.dependencies import Depends
        from fastmcp.server.dependencies import get_http_headers
    except ImportError as exc:
        raise FastMCPBridgeError(
            "FastMCP support is not installed; install "
            "'nexus-openwrt-agent-sdk[fastmcp]' on Python 3.10+"
        ) from exc

    @contextmanager
    def nexus_run_dependency() -> Iterator[NexusRunContext]:
        context = NexusRunContext.from_env(
            headers=get_http_headers(),
            config=config,
        )
        token = set_current_run(context)
        try:
            yield context
        finally:
            reset_current_run(token)
            context.close()

    return Depends(nexus_run_dependency)


class FastMCPBridgeError(Exception):
    """FastMCP dependency, configuration, discovery, or lifecycle failure."""


class FastMCPToolError(FastMCPBridgeError):
    """A mapped FastMCP tool returned an error."""


@dataclass(frozen=True)
class FastMCPToolMapping:
    """Expose one FastMCP tool as one Nexus capability route."""

    tool: str
    capability: CapabilityRegistration


@dataclass(frozen=True)
class FastMCPTool:
    """Bounded, JSON-compatible metadata discovered from FastMCP."""

    name: str
    title: Optional[str]
    description: Optional[str]
    input_schema: Mapping[str, Any]
    output_schema: Optional[Mapping[str, Any]]


def _json_value(value: Any) -> Any:
    """Convert FastMCP/Pydantic results to strict JSON-compatible values."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FastMCPBridgeError("FastMCP result contains a non-finite number")
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (UUID, Path)):
        return str(value)
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, bytes):
        return {
            "encoding": "base64",
            "data": base64.b64encode(value).decode("ascii"),
        }
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_value(model_dump(mode="json", by_alias=True))
        except TypeError:
            return _json_value(model_dump())
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise FastMCPBridgeError("FastMCP result contains a non-string object key")
            result[key] = _json_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise FastMCPBridgeError(
        f"FastMCP result type {type(value).__name__!r} is not JSON serializable"
    )


def _attribute(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _tool_error_text(result: Any) -> str:
    messages: List[str] = []
    for item in _attribute(result, "content") or []:
        text = _attribute(item, "text")
        if isinstance(text, str) and text:
            messages.append(text)
    return "; ".join(messages) or "FastMCP tool returned an error"


def fastmcp_result_to_json(result: Any) -> Any:
    """Convert a FastMCP ``CallToolResult`` into a JSON response body."""

    if bool(_attribute(result, "is_error", "isError")):
        raise FastMCPToolError(_tool_error_text(result))
    data = _attribute(result, "data")
    if data is not None:
        return _json_value(data)
    structured = _attribute(result, "structured_content", "structuredContent")
    if structured is not None:
        return _json_value(structured)
    content = _attribute(result, "content")
    if content is not None:
        return {"content": _json_value(content)}
    return None


class _AsyncLoop:
    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.ready = threading.Event()

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.ready.clear()

        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            self.ready.set()
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

        self.thread = threading.Thread(
            target=run,
            name="nexus-fastmcp-loop",
            daemon=True,
        )
        self.thread.start()
        if not self.ready.wait(5.0) or self.loop is None:
            raise FastMCPBridgeError("FastMCP async event loop did not start")

    def submit(self, awaitable: Any) -> Future:
        if self.loop is None or self.thread is None or not self.thread.is_alive():
            raise FastMCPBridgeError("FastMCP bridge is not running")
        return asyncio.run_coroutine_threadsafe(awaitable, self.loop)

    def run(self, awaitable: Any, timeout: Optional[float] = None) -> Any:
        return self.submit(awaitable).result(timeout=timeout)

    def is_healthy(self) -> bool:
        return (
            self.loop is not None
            and not self.loop.is_closed()
            and self.thread is not None
            and self.thread.is_alive()
        )

    def stop(self) -> None:
        loop = self.loop
        thread = self.thread
        if loop is not None and thread is not None and thread.is_alive():
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5.0)
        self.loop = None
        self.thread = None


class FastMCPBridge:
    """Map enumerated FastMCP tools to Nexus capabilities.

    ``mappings`` may be a ``tool_name -> CapabilityRegistration`` mapping or
    an iterable of :class:`FastMCPToolMapping`.  Only explicitly mapped tools
    are exposed to Nexus; unmapped FastMCP tools remain private.
    """

    def __init__(
        self,
        mcp: Any,
        server: NexusAgentServer,
        mappings: Any,
        *,
        call_timeout: Optional[float] = 30.0,
        pass_nexus_metadata: bool = True,
        max_stream_events: int = 256,
        max_progress_message_chars: int = 1024,
        client_factory: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        if call_timeout is not None and call_timeout <= 0:
            raise ValueError("call_timeout must be positive or None")
        if not 2 <= max_stream_events <= 4096:
            raise ValueError("max_stream_events must be between 2 and 4096")
        if not 32 <= max_progress_message_chars <= 8192:
            raise ValueError(
                "max_progress_message_chars must be between 32 and 8192"
            )
        self.mcp = mcp
        self.server = server
        self.call_timeout = call_timeout
        self.pass_nexus_metadata = pass_nexus_metadata
        self.max_stream_events = max_stream_events
        self.max_progress_message_chars = max_progress_message_chars
        self._client_factory = client_factory
        self._mappings = self._normalize_mappings(mappings)
        self._mapping_by_tool = {mapping.tool: mapping for mapping in self._mappings}
        self._mapping_by_intent = {
            mapping.capability.intent: mapping for mapping in self._mappings
        }
        self._runner = _AsyncLoop()
        self._client_context: Any = None
        self._client: Any = None
        self._tools: Tuple[FastMCPTool, ...] = ()
        self._started = False
        self._lock = threading.RLock()

    @staticmethod
    def _normalize_mappings(mappings: Any) -> Tuple[FastMCPToolMapping, ...]:
        if isinstance(mappings, Mapping):
            values = [
                FastMCPToolMapping(str(tool), capability)
                for tool, capability in mappings.items()
            ]
        else:
            values = list(mappings)
        if not values:
            raise ValueError("at least one FastMCP tool mapping is required")
        normalized: List[FastMCPToolMapping] = []
        tool_names = set()
        intents = set()
        for value in values:
            if not isinstance(value, FastMCPToolMapping):
                raise TypeError("mappings must contain FastMCPToolMapping values")
            if not value.tool or len(value.tool) > 128:
                raise ValueError("FastMCP tool name must be 1..128 characters")
            if not isinstance(value.capability, CapabilityRegistration):
                raise TypeError("each FastMCP mapping requires CapabilityRegistration")
            if value.tool in tool_names:
                raise ValueError(f"duplicate FastMCP tool mapping: {value.tool}")
            if value.capability.intent in intents:
                raise ValueError(
                    f"duplicate Nexus intent mapping: {value.capability.intent}"
                )
            tool_names.add(value.tool)
            intents.add(value.capability.intent)
            normalized.append(value)
        return tuple(normalized)

    @property
    def mappings(self) -> Tuple[FastMCPToolMapping, ...]:
        return self._mappings

    @property
    def capabilities(self) -> Tuple[CapabilityRegistration, ...]:
        return tuple(mapping.capability for mapping in self._mappings)

    @property
    def tools(self) -> Tuple[FastMCPTool, ...]:
        """The full FastMCP tool catalog captured during ``start()``."""

        return self._tools

    @property
    def started(self) -> bool:
        return self._started

    def is_healthy(self) -> bool:
        """Return whether the Tool bridge and Agent listener are both live."""

        with self._lock:
            return (
                self._started
                and self._client is not None
                and self._runner.is_healthy()
                and self.server.is_healthy()
            )

    @staticmethod
    def _default_client_factory(mcp: Any) -> Any:
        try:
            from fastmcp import Client
        except ImportError as exc:
            raise FastMCPBridgeError(
                "FastMCP support is not installed; install "
                "'nexus-openwrt-agent-sdk[fastmcp]' on Python 3.10+"
            ) from exc
        return Client(mcp)

    @staticmethod
    def _describe_tool(tool: Any) -> FastMCPTool:
        name = _attribute(tool, "name")
        if not isinstance(name, str) or not name:
            raise FastMCPBridgeError("FastMCP returned a tool without a valid name")
        title = _attribute(tool, "title")
        description = _attribute(tool, "description")
        input_schema = _attribute(tool, "inputSchema", "input_schema") or {}
        output_schema = _attribute(tool, "outputSchema", "output_schema")
        converted_input = _json_value(input_schema)
        converted_output = None if output_schema is None else _json_value(output_schema)
        if not isinstance(converted_input, Mapping):
            raise FastMCPBridgeError(f"FastMCP tool {name!r} has an invalid input schema")
        if converted_output is not None and not isinstance(converted_output, Mapping):
            raise FastMCPBridgeError(f"FastMCP tool {name!r} has an invalid output schema")
        return FastMCPTool(
            name=name,
            title=title if isinstance(title, str) else None,
            description=description if isinstance(description, str) else None,
            input_schema=converted_input,
            output_schema=converted_output,
        )

    async def _open_client(self) -> Tuple[FastMCPTool, ...]:
        factory = self._client_factory or self._default_client_factory
        context = factory(self.mcp)
        if not hasattr(context, "__aenter__") or not hasattr(context, "__aexit__"):
            raise FastMCPBridgeError("FastMCP Client must be an async context manager")
        self._client_context = context
        self._client = await context.__aenter__()
        try:
            raw_tools = await self._client.list_tools()
            tools = tuple(self._describe_tool(tool) for tool in raw_tools)
            names = {tool.name for tool in tools}
            missing = sorted(set(self._mapping_by_tool) - names)
            if missing:
                raise FastMCPBridgeError(
                    "mapped FastMCP tools were not discovered: " + ", ".join(missing)
                )
            return tools
        except BaseException:
            await context.__aexit__(None, None, None)
            self._client_context = None
            self._client = None
            raise

    async def _close_client(self) -> None:
        context = self._client_context
        self._client_context = None
        self._client = None
        if context is not None:
            await context.__aexit__(None, None, None)

    @staticmethod
    def _inferred_text(value: Optional[str], maximum: int) -> Optional[str]:
        if not isinstance(value, str) or not value.strip():
            return None
        raw = " ".join(value.split()).encode("utf-8")
        if len(raw) <= maximum:
            return raw.decode("utf-8")
        return raw[:maximum].decode("utf-8", "ignore").rstrip() or None

    def _reuse_cloud_tool_metadata(self) -> None:
        """Fill implicit Cloud descriptors from the discovered FastMCP catalog."""

        catalog = {tool.name: tool for tool in self._tools}
        updated: List[FastMCPToolMapping] = []
        for mapping in self._mappings:
            capability = mapping.capability
            cloud = capability.cloud
            if cloud is not None and cloud.publish and cloud.tool is None:
                tool = catalog[mapping.tool]
                try:
                    descriptor = McpToolDescriptor(
                        name=tool.name,
                        title=self._inferred_text(tool.title, 127),
                        description=self._inferred_text(tool.description, 511),
                        input_schema=dict(tool.input_schema),
                    )
                except ValueError as exc:
                    raise FastMCPBridgeError(
                        f"FastMCP tool {tool.name!r} cannot be published: {exc}"
                    ) from exc
                capability = replace(
                    capability,
                    cloud=CloudRegistrationManifest(
                        publish=cloud.publish,
                        agent_name=cloud.agent_name,
                        tool=descriptor,
                    ),
                )
            updated.append(FastMCPToolMapping(mapping.tool, capability))
        self._mappings = tuple(updated)
        self._mapping_by_tool = {mapping.tool: mapping for mapping in self._mappings}
        self._mapping_by_intent = {
            mapping.capability.intent: mapping for mapping in self._mappings
        }

    def start(self) -> "FastMCPBridge":
        """Open the in-memory FastMCP client, enumerate, validate, and attach."""

        with self._lock:
            if self._started:
                return self
            occupied = [
                intent for intent in self._mapping_by_intent
                if self.server.has_handler(intent)
            ]
            if occupied:
                raise FastMCPBridgeError(
                    "Nexus intent already has a handler: " + ", ".join(sorted(occupied))
                )
            self._runner.start()
            try:
                self._tools = self._runner.run(self._open_client(), timeout=10.0)
                self._reuse_cloud_tool_metadata()
                for intent in self._mapping_by_intent:
                    self.server.add_handler(
                        intent,
                        self.handle,
                        stream_handler=self.handle_stream,
                    )
                self._started = True
                return self
            except BaseException:
                for intent in self._mapping_by_intent:
                    self.server.remove_handler(intent)
                self._tools = ()
                if self._client_context is not None:
                    try:
                        self._runner.run(self._close_client(), timeout=5.0)
                    except BaseException:
                        pass
                self._runner.stop()
                raise

    def stop(self) -> None:
        """Detach Nexus handlers and close the FastMCP client."""

        with self._lock:
            if not self._started:
                self._runner.stop()
                return
            for intent in self._mapping_by_intent:
                self.server.remove_handler(intent)
            self._started = False
            self._tools = ()
            try:
                self._runner.run(self._close_client(), timeout=5.0)
            finally:
                self._runner.stop()

    async def _call_tool(self, tool: str, arguments: Mapping[str, Any], meta: Any) -> Any:
        client = self._client
        if client is None:
            raise FastMCPBridgeError("FastMCP bridge is not running")
        keywords: Dict[str, Any] = {"raise_on_error": False}
        if self.call_timeout is not None:
            keywords["timeout"] = self.call_timeout
        if meta:
            keywords["meta"] = meta
        result = await client.call_tool(tool, dict(arguments), **keywords)
        return fastmcp_result_to_json(result)

    async def _call_tool_with_progress(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        meta: Any,
        events: "queue.Queue[Mapping[str, Any]]",
    ) -> Any:
        client = self._client
        if client is None:
            raise FastMCPBridgeError("FastMCP bridge is not running")
        progress_count = 0

        async def progress_handler(
            progress: float,
            total: Optional[float],
            message: Optional[str],
        ) -> None:
            nonlocal progress_count
            if progress_count >= self.max_stream_events - 1:
                raise FastMCPBridgeError(
                    "FastMCP progress event limit exceeded"
                )
            progress_value = float(progress)
            total_value = None if total is None else float(total)
            if not math.isfinite(progress_value) or (
                total_value is not None and not math.isfinite(total_value)
            ):
                raise FastMCPBridgeError(
                    "FastMCP progress contains a non-finite number"
                )
            message_value = None
            if message is not None:
                message_value = str(message)
                if len(message_value) > self.max_progress_message_chars:
                    raise FastMCPBridgeError(
                        "FastMCP progress message exceeds the configured limit"
                    )
            progress_count += 1
            try:
                events.put_nowait({
                    "progress": progress_value,
                    "total": total_value,
                    "message": message_value,
                })
            except queue.Full as exc:
                raise FastMCPBridgeError(
                    "FastMCP progress consumer is too slow"
                ) from exc

        keywords: Dict[str, Any] = {
            "raise_on_error": False,
            "progress_handler": progress_handler,
        }
        if self.call_timeout is not None:
            keywords["timeout"] = self.call_timeout
        if meta:
            keywords["meta"] = meta
        result = await client.call_tool(tool, dict(arguments), **keywords)
        return fastmcp_result_to_json(result)

    def _submit_call(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        meta: Optional[Mapping[str, Any]],
    ) -> Future:
        if tool not in self._mapping_by_tool:
            raise FastMCPBridgeError(f"FastMCP tool is not mapped to Nexus: {tool}")
        with self._lock:
            self.start()
            return self._runner.submit(self._call_tool(tool, arguments, meta))

    def call_tool_sync(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Call a mapped FastMCP tool from synchronous application code."""

        future = self._submit_call(tool, arguments, meta)
        wait_timeout = None if self.call_timeout is None else self.call_timeout + 1.0
        try:
            return future.result(timeout=wait_timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise FastMCPToolError("FastMCP tool invocation timed out") from exc

    async def call_tool(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Asynchronously call a mapped tool through the shared FastMCP client."""

        future = self._submit_call(tool, arguments, meta)
        return await asyncio.wrap_future(future)

    def call_tool_stream(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> Iterator[SseEvent]:
        """Yield bounded MCP progress notifications and one terminal event."""

        if tool not in self._mapping_by_tool:
            raise FastMCPBridgeError(f"FastMCP tool is not mapped to Nexus: {tool}")
        with self._lock:
            self.start()
            events: "queue.Queue[Mapping[str, Any]]" = queue.Queue(
                maxsize=self.max_stream_events
            )
            future = self._runner.submit(
                self._call_tool_with_progress(tool, arguments, meta, events)
            )
        sequence = 0
        try:
            while True:
                try:
                    progress = events.get(timeout=0.1)
                except queue.Empty:
                    if not future.done():
                        continue
                    try:
                        result = future.result()
                    except FastMCPToolError as exc:
                        payload = {
                            "code": "FASTMCP_TOOL_FAILED",
                            "message": (str(exc)[:512] or "FastMCP tool failed"),
                        }
                        sequence += 1
                        yield SseEvent(
                            data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                            event="error",
                            event_id=str(sequence),
                        )
                    except BaseException as exc:
                        payload = {
                            "code": "FASTMCP_STREAM_FAILED",
                            "message": (str(exc)[:512] or "FastMCP stream failed"),
                        }
                        sequence += 1
                        yield SseEvent(
                            data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                            event="error",
                            event_id=str(sequence),
                        )
                    else:
                        sequence += 1
                        yield SseEvent(
                            data=json.dumps(
                                {"result": result},
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ),
                            event="result",
                            event_id=str(sequence),
                        )
                    break
                else:
                    sequence += 1
                    yield SseEvent(
                        data=json.dumps(
                            progress,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                        event="progress",
                        event_id=str(sequence),
                    )
        finally:
            if not future.done():
                future.cancel()

    @staticmethod
    def _arguments(envelope: AgentEnvelope, tool: str) -> Mapping[str, Any]:
        if envelope.protocol is None:
            if not isinstance(envelope.payload, Mapping):
                raise AgentRequestError(
                    400, "INVALID_TOOL_ARGUMENTS", "Nexus payload must be an object"
                )
            return envelope.payload
        if envelope.protocol.lower() != "mcp":
            raise AgentRequestError(
                400, "INVALID_MCP_REQUEST", "FastMCP capability requires MCP or direct Invoke"
            )
        request = envelope.protocol_request
        if not isinstance(request, Mapping) or request.get("method") != "tools/call":
            raise AgentRequestError(
                400, "INVALID_MCP_REQUEST", "expected an MCP tools/call request"
            )
        params = request.get("params")
        if not isinstance(params, Mapping):
            raise AgentRequestError(
                400, "INVALID_MCP_REQUEST", "MCP tools/call params must be an object"
            )
        requested_tool = params.get("name")
        if requested_tool != tool or (
            envelope.selector is not None and envelope.selector != tool
        ):
            raise AgentRequestError(
                400, "MCP_TOOL_MISMATCH", "MCP selector does not match the routed capability"
            )
        arguments = params.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise AgentRequestError(
                400, "INVALID_TOOL_ARGUMENTS", "MCP tool arguments must be an object"
            )
        return arguments

    def handle(self, envelope: AgentEnvelope) -> Any:
        """Nexus server handler that dispatches an Envelope to its mapped tool."""

        mapping = self._mapping_by_intent.get(envelope.intent)
        if mapping is None:
            raise AgentRequestError(404, "INTENT_NOT_SERVED", "intent is not mapped")
        arguments = self._arguments(envelope, mapping.tool)
        meta = self._metadata(envelope)
        try:
            return self.call_tool_sync(mapping.tool, arguments, meta=meta)
        except FastMCPToolError as exc:
            message = str(exc)[:512] or "FastMCP tool failed"
            status = 504 if "timed out" in message.lower() else 502
            code = "FASTMCP_TOOL_TIMEOUT" if status == 504 else "FASTMCP_TOOL_FAILED"
            raise AgentRequestError(status, code, message) from exc
        except FastMCPBridgeError as exc:
            raise AgentRequestError(
                503, "FASTMCP_UNAVAILABLE", str(exc)[:512]
            ) from exc

    def _metadata(self, envelope: AgentEnvelope) -> Optional[Mapping[str, Any]]:
        if not self.pass_nexus_metadata:
            return None
        meta: Dict[str, Any] = {
                "nexus.task_id": envelope.task_id,
                "nexus.source_agent": envelope.source_agent,
                "nexus.tenant": envelope.tenant,
                "nexus.intent": envelope.intent,
        }
        if envelope.route_id:
            meta["nexus.route_id"] = envelope.route_id
        if envelope.target_agent:
            meta["nexus.target_agent"] = envelope.target_agent
        return meta

    def handle_stream(self, envelope: AgentEnvelope) -> Iterator[SseEvent]:
        """Nexus SSE handler for one mapped FastMCP Tool call."""

        mapping = self._mapping_by_intent.get(envelope.intent)
        if mapping is None:
            raise AgentRequestError(404, "INTENT_NOT_SERVED", "intent is not mapped")
        arguments = self._arguments(envelope, mapping.tool)
        return self.call_tool_stream(
            mapping.tool,
            arguments,
            meta=self._metadata(envelope),
        )

    @contextmanager
    def registered(
        self,
        client: NexusAgentClient,
        *,
        auto_renew: bool = True,
        renew_fraction: float = 0.6,
        health_check: Optional[Callable[[], bool]] = None,
        reregister_on_not_found: bool = True,
    ) -> Iterator[Tuple[AgentLease, ...]]:
        """Start the bridge and keep all mapped Nexus routes registered."""

        self.start()
        try:
            with self.server.registered(
                client,
                self.capabilities,
                auto_renew=auto_renew,
                renew_fraction=renew_fraction,
                health_check=health_check or self.is_healthy,
                reregister_on_not_found=reregister_on_not_found,
            ) as leases:
                yield leases
        finally:
            self.stop()

    def serve_registered(
        self,
        client: NexusAgentClient,
        *,
        auto_renew: bool = True,
        renew_fraction: float = 0.6,
        health_check: Optional[Callable[[], bool]] = None,
        reregister_on_not_found: bool = True,
    ) -> None:
        """Register mapped capabilities and serve Nexus invokes forever."""

        with self.registered(
            client,
            auto_renew=auto_renew,
            renew_fraction=renew_fraction,
            health_check=health_check,
            reregister_on_not_found=reregister_on_not_found,
        ):
            self.server.serve_forever()

    def __enter__(self) -> "FastMCPBridge":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop()


class _CombinedMCPASGI:
    def __init__(self, modern: Any, legacy: Any) -> None:
        self.modern = modern
        self.legacy = legacy

    async def __call__(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan":
            await self._lifespan(receive, send)
            return
        path = str(scope.get("path") or "")
        target = self.legacy if path == "/sse" or path == "/messages" or path.startswith("/messages/") else self.modern
        await target(scope, receive, send)

    async def _lifespan(self, receive: Any, send: Any) -> None:
        message = await receive()
        if message.get("type") != "lifespan.startup":
            return
        started = False
        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(
                    self.modern.router.lifespan_context(self.modern)
                )
                await stack.enter_async_context(
                    self.legacy.router.lifespan_context(self.legacy)
                )
                started = True
                await send({"type": "lifespan.startup.complete"})
                message = await receive()
        except Exception as exc:
            await send({
                "type": (
                    "lifespan.shutdown.failed"
                    if started
                    else "lifespan.startup.failed"
                ),
                "message": type(exc).__name__,
            })
            return
        if message.get("type") == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})


class NexusMCPServer:
    """Production-oriented FastMCP facade for Nexus-hosted Docker Agents."""

    WORKSPACE_SCOPES = frozenset({
        "connection.list", "connection.create", "connection.update",
        "connection.delete", "connection.test", "connection.bind",
        "files.list", "files.read", "files.write", "command.execute",
    })

    def __init__(self, name: str, *, legacy_sse: bool = False, **options: Any) -> None:
        try:
            from fastmcp import FastMCP
        except ImportError as exc:
            raise FastMCPBridgeError(
                "FastMCP support is not installed; install "
                "'nexus-openwrt-agent-sdk[fastmcp]' on Python 3.10+"
            ) from exc
        self._mcp = FastMCP(name, **options)
        self.legacy_sse = bool(legacy_sse)
        self._workspace_scopes: set[str] = set()
        self._nexus_tool_policy: dict[str, dict[str, Any]] = {}

    @property
    def fastmcp(self) -> Any:
        return self._mcp

    def __getattr__(self, name: str) -> Any:
        return getattr(self._mcp, name)

    def tool(
        self,
        function: Any = None,
        *,
        name: Optional[str] = None,
        task: bool = False,
        continuable: bool = False,
        demo: bool = False,
        chat: bool = False,
        interactive: bool = False,
        mobile_scopes: Optional[Sequence[str]] = None,
        slash_command: Optional[str] = None,
        slash_description: Optional[str] = None,
        execution_profiles: Optional[Sequence[NexusExecutionProfile]] = None,
        input_modalities: Optional[Sequence[str]] = None,
        **options: Any,
    ) -> Any:
        """Register a FastMCP tool and its Nexus execution policy."""

        task = bool(task or chat)
        interactive = bool(interactive or chat)
        if continuable and not task:
            raise ValueError("continuable Nexus tools must enable task=True")
        normalized_mobile_scopes = tuple(dict.fromkeys(str(value).strip() for value in (mobile_scopes or ()) if str(value).strip()))
        unsupported_mobile_scopes = [
            value for value in normalized_mobile_scopes if value not in MOBILE_SCOPES
        ]
        if unsupported_mobile_scopes:
            raise ValueError(
                f"unsupported Mobile scope: {unsupported_mobile_scopes[0]}"
            )
        normalized_profiles = tuple(execution_profiles or ())
        # Reuse the SDK descriptor as the single validation implementation.
        declared_modalities = tuple(input_modalities or ("text",))
        validation_properties: Dict[str, Any] = {"content": {"type": "string"}}
        if "audio" in declared_modalities:
            validation_properties["audio"] = {"type": "array", "items": {"type": "object"}}
        policy_descriptor = McpToolDescriptor(
            name=str(name or "placeholder"),
            input_schema={"type": "object", "properties": validation_properties},
            task=task,
            continuable=continuable,
            demo=demo,
            chat=chat,
            interactive=interactive,
            mobile_scopes=normalized_mobile_scopes,
            slash_command=slash_command,
            slash_description=slash_description,
            execution_profiles=normalized_profiles,
            input_modalities=declared_modalities,
        )

        def register(candidate: Any) -> Any:
            tool_name = str(name or getattr(candidate, "__name__", "")).strip()
            if not tool_name:
                raise ValueError("Nexus MCP tool name is required")
            if chat and any(
                item.get("chat") for item in self._nexus_tool_policy.values()
            ):
                raise ValueError("a Nexus MCP server can declare only one chat tool")
            policy = {
                "task": bool(task),
                "continuable": bool(continuable),
                "demo": bool(demo),
                "recovery_protocol": 1,
            }
            if chat:
                policy["chat"] = True
            if interactive:
                policy["interactive"] = True
            if normalized_mobile_scopes:
                policy["mobile_scopes"] = list(normalized_mobile_scopes)
            if policy_descriptor.slash_command:
                policy["slash_command"] = policy_descriptor.slash_command
                policy["slash_description"] = policy_descriptor.slash_description or policy_descriptor.slash_command
            if policy_descriptor.execution_profiles:
                policy["execution_profiles"] = [item.to_dict() for item in policy_descriptor.execution_profiles]
            if input_modalities is not None:
                policy["input_modalities"] = list(policy_descriptor.input_modalities)
            setattr(candidate, "__nexus_tool_policy__", policy)
            self._nexus_tool_policy[tool_name] = policy
            metadata = dict(options.pop("meta", {}) or {})
            metadata["nexus"] = policy
            try:
                decorator = self._mcp.tool(name=tool_name, meta=metadata, **options)
            except TypeError:
                # Older FastMCP releases do not expose tool metadata. The
                # policy remains available through ``nexus_tool_policy`` and
                # ordinary tools continue to work unchanged.
                decorator = self._mcp.tool(name=tool_name, **options)
            return decorator(candidate)

        return register if function is None else register(function)

    @property
    def nexus_tool_policy(self) -> Mapping[str, Mapping[str, Any]]:
        return {key: dict(value) for key, value in self._nexus_tool_policy.items()}

    def enable_workspace_tools(self, scopes: Any) -> "NexusMCPServer":
        requested = {str(value) for value in scopes}
        unsupported = requested - self.WORKSPACE_SCOPES
        if unsupported:
            raise ValueError("unsupported Nexus Workspace scopes: " + ", ".join(sorted(unsupported)))
        new_scopes = requested - self._workspace_scopes
        self._register_workspace_tools(new_scopes)
        self._workspace_scopes.update(new_scopes)
        return self

    def _tool(self, name: str, function: Any) -> None:
        self._mcp.tool(name=name)(function)

    def _register_workspace_tools(self, scopes: set[str]) -> None:
        if "connection.list" in scopes:
            async def connections_list(nexus: NexusMCPContext = CurrentNexusMCP()):
                return [dict(item) for item in await nexus.computer.connections.list()]
            self._tool("nexus_workspace_connections_list", connections_list)

        if "connection.create" in scopes:
            async def connection_create(
                name: str,
                ssh_host: str,
                ssh_user: str,
                auth_mode: str = "private_key",
                ssh_port: int = 22,
                workspace_root: str = "~/.nexus",
                private_key: str = "",
                password: str = "",
                metadata: Optional[Mapping[str, Any]] = None,
                nexus: NexusMCPContext = CurrentNexusMCP(),
            ):
                value = await nexus.computer.connections.create(
                    name,
                    ssh_host,
                    ssh_user,
                    auth_mode=auth_mode,
                    ssh_port=ssh_port,
                    workspace_root=workspace_root,
                    private_key=private_key,
                    password=password,
                    metadata=metadata,
                )
                return dict(value)
            self._tool("nexus_workspace_connection_create", connection_create)

        if "connection.update" in scopes:
            async def connection_update(
                connection_id: str,
                name: Optional[str] = None,
                ssh_host: Optional[str] = None,
                ssh_port: Optional[int] = None,
                ssh_user: Optional[str] = None,
                auth_mode: Optional[str] = None,
                workspace_root: Optional[str] = None,
                private_key: Optional[str] = None,
                password: Optional[str] = None,
                metadata: Optional[Mapping[str, Any]] = None,
                nexus: NexusMCPContext = CurrentNexusMCP(),
            ):
                changes = {
                    key: value for key, value in {
                        "name": name, "ssh_host": ssh_host, "ssh_port": ssh_port,
                        "ssh_user": ssh_user, "auth_mode": auth_mode,
                        "workspace_root": workspace_root, "private_key": private_key,
                        "password": password, "metadata": metadata,
                    }.items() if value is not None
                }
                return dict(await nexus.computer.connections.update(connection_id, **changes))
            self._tool("nexus_workspace_connection_update", connection_update)

        if "connection.delete" in scopes:
            async def connection_delete(
                connection_id: str,
                nexus: NexusMCPContext = CurrentNexusMCP(),
            ):
                await nexus.computer.connections.delete(connection_id)
                return {"deleted": True, "connection_id": connection_id}
            self._tool("nexus_workspace_connection_delete", connection_delete)

        if "connection.test" in scopes:
            async def connection_test(
                connection_id: str,
                nexus: NexusMCPContext = CurrentNexusMCP(),
            ):
                return dict(await nexus.computer.connections.test(connection_id))
            self._tool("nexus_workspace_connection_test", connection_test)

        if "connection.bind" in scopes:
            async def computer_bind(
                connection_id: str,
                make_default: bool = True,
                nexus: NexusMCPContext = CurrentNexusMCP(),
            ):
                return dict(await nexus.computer.bind(connection_id, make_default=make_default))
            self._tool("nexus_workspace_computer_bind", computer_bind)

        if "files.list" in scopes:
            async def files_list(path: str = ".", nexus: NexusMCPContext = CurrentNexusMCP()):
                return [dict(item) for item in await nexus.workspace.list(path)]
            self._tool("nexus_workspace_files_list", files_list)

        if "files.read" in scopes:
            async def file_read(path: str, nexus: NexusMCPContext = CurrentNexusMCP()):
                return {"path": path, "content": await nexus.workspace.read_text(path)}
            self._tool("nexus_workspace_file_read", file_read)

        if "files.write" in scopes:
            async def file_write(
                path: str,
                content: str,
                nexus: NexusMCPContext = CurrentNexusMCP(),
            ):
                return await nexus.workspace.write_text(path, content)
            self._tool("nexus_workspace_file_write", file_write)

        if "command.execute" in scopes:
            async def command_execute(
                command: str,
                cwd: str = ".",
                timeout: Optional[int] = None,
                display: bool = True,
                nexus: NexusMCPContext = CurrentNexusMCP(),
            ):
                return dict(
                    await nexus.terminal.run(
                        command,
                        cwd=cwd,
                        timeout=timeout,
                        display=display,
                    )
                )
            self._tool("nexus_workspace_command_execute", command_execute)

    def http_app(self, *, legacy_sse: Optional[bool] = None) -> Any:
        enabled = self.legacy_sse if legacy_sse is None else bool(legacy_sse)
        try:
            modern = self._mcp.http_app(path="/mcp")
        except TypeError:
            modern = self._mcp.http_app()
        if not enabled:
            return modern
        try:
            legacy = self._mcp.http_app(path="/sse", transport="sse")
        except TypeError:
            raise FastMCPBridgeError(
                "Installed FastMCP does not provide legacy SSE transport"
            ) from None
        return _CombinedMCPASGI(modern, legacy)

    def run(
        self,
        *,
        transport: str = "streamable-http",
        host: str = "127.0.0.1",
        port: int = 8000,
        legacy_sse: Optional[bool] = None,
        **options: Any,
    ) -> Any:
        normalized = "http" if transport in {"http", "streamable-http"} else transport
        enabled = self.legacy_sse if legacy_sse is None else bool(legacy_sse)
        if not enabled:
            return self._mcp.run(transport=normalized, host=host, port=port, **options)
        try:
            import uvicorn
        except ImportError as exc:
            raise FastMCPBridgeError("FastMCP HTTP deployment requires uvicorn") from exc
        return uvicorn.run(self.http_app(legacy_sse=True), host=host, port=port, **options)


__all__ = [
    "CurrentNexusMCP",
    "CurrentNexusRun",
    "FastMCPBridge",
    "FastMCPBridgeError",
    "FastMCPTool",
    "FastMCPToolError",
    "FastMCPToolMapping",
    "NexusMCPContext",
    "NexusMCPFeedback",
    "NexusMCPServer",
    "fastmcp_result_to_json",
]
