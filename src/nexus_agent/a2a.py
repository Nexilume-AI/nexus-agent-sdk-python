"""Optional A2A 1.x SDK bridge for callable Nexus Python Agents.

The Nexus Adapter keeps the A2A wire protocol at the router edge and places
the original ``SendMessageRequest`` in ``AgentEnvelope.protocol_request``.
This bridge validates that request with the official A2A Python SDK and runs
an official ``AgentExecutor`` through ``DefaultRequestHandler``.
"""

import asyncio
import threading
import urllib.parse
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from dataclasses import dataclass
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from .client import AgentLease, NexusAgentClient
from .models import AgentEnvelope, CapabilityRegistration
from .server import AgentRequestError, NexusAgentServer


class A2ABridgeError(Exception):
    """A2A dependency, configuration, or lifecycle failure."""


@dataclass(frozen=True)
class A2ASkillMapping:
    """Expose one Agent Card skill as one Nexus capability route."""

    skill: str
    capability: CapabilityRegistration


@dataclass(frozen=True)
class A2AResult:
    """Friendly result returned by :class:`NexusA2AClient`."""

    message_id: str
    context_id: str
    text: str
    data: Any
    task: Optional[Mapping[str, Any]]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class A2AStreamEvent:
    """One normalized event from an official A2A streaming task."""

    kind: str
    task_id: str
    context_id: str
    state: str
    text: str
    last_chunk: bool
    final: bool
    raw: Mapping[str, Any]
    event_id: str = ""


@dataclass(frozen=True)
class _SkillDefinition:
    skill: str
    name: str
    description: str
    tags: Tuple[str, ...]
    input_modes: Tuple[str, ...]
    output_modes: Tuple[str, ...]
    capability: CapabilityRegistration


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
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

        self.thread = threading.Thread(
            target=run,
            name="nexus-a2a-loop",
            daemon=True,
        )
        self.thread.start()
        if not self.ready.wait(5.0) or self.loop is None:
            raise A2ABridgeError("A2A async event loop did not start")

    def submit(self, awaitable: Any) -> Future:
        if self.loop is None or self.thread is None or not self.thread.is_alive():
            raise A2ABridgeError("A2A bridge is not running")
        return asyncio.run_coroutine_threadsafe(awaitable, self.loop)

    def run(self, awaitable: Any, timeout: float) -> Any:
        future = self.submit(awaitable)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as error:
            future.cancel()
            raise TimeoutError("A2A executor timed out") from error

    def is_healthy(self) -> bool:
        return (
            self.loop is not None
            and not self.loop.is_closed()
            and self.thread is not None
            and self.thread.is_alive()
        )

    def close(self) -> None:
        loop = self.loop
        thread = self.thread
        if loop is not None and thread is not None and thread.is_alive():
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5.0)
        self.loop = None
        self.thread = None


def _load_a2a() -> Dict[str, Any]:
    try:
        import httpx
        from a2a.client import ClientConfig, create_client
        from a2a.helpers import new_text_message
        from a2a.server.context import ServerCallContext
        from a2a.server.request_handlers import DefaultRequestHandler
        from a2a.server.tasks import InMemoryTaskStore
        from a2a.types import (
            AgentCapabilities,
            AgentCard,
            AgentInterface,
            AgentSkill,
            Role,
            SendMessageRequest,
            StreamResponse,
        )
        from google.protobuf.json_format import MessageToDict, ParseDict
    except ImportError as error:
        raise A2ABridgeError(
            'A2A integration requires: pip install "nexilume[a2a]"'
        ) from error
    return {
        "ServerCallContext": ServerCallContext,
        "DefaultRequestHandler": DefaultRequestHandler,
        "InMemoryTaskStore": InMemoryTaskStore,
        "AgentCapabilities": AgentCapabilities,
        "AgentCard": AgentCard,
        "AgentInterface": AgentInterface,
        "AgentSkill": AgentSkill,
        "Role": Role,
        "SendMessageRequest": SendMessageRequest,
        "StreamResponse": StreamResponse,
        "MessageToDict": MessageToDict,
        "ParseDict": ParseDict,
        "ClientConfig": ClientConfig,
        "create_client": create_client,
        "new_text_message": new_text_message,
        "httpx": httpx,
    }


class A2AExecutorBridge:
    """Run an official A2A ``AgentExecutor`` behind a Nexus Agent Server."""

    def __init__(
        self,
        executor: Any,
        server: NexusAgentServer,
        agent_card: Any,
        mappings: Mapping[str, CapabilityRegistration],
        *,
        task_store: Any = None,
        timeout: float = 30.0,
    ) -> None:
        if executor is None or agent_card is None:
            raise ValueError("executor and agent_card are required")
        if not isinstance(server, NexusAgentServer):
            raise TypeError("server must be a NexusAgentServer")
        if not mappings:
            raise ValueError("at least one A2A skill mapping is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        normalized: Dict[str, CapabilityRegistration] = {}
        intents = set()
        for skill, capability in mappings.items():
            if not isinstance(skill, str) or not skill or len(skill) > 127:
                raise ValueError("A2A skill must be 1..127 characters")
            if not isinstance(capability, CapabilityRegistration):
                raise TypeError("A2A mapping values must be CapabilityRegistration")
            if capability.intent in intents:
                raise ValueError("each mapped A2A skill must use a unique intent")
            normalized[skill] = capability
            intents.add(capability.intent)
        self.executor = executor
        self.server = server
        self.agent_card = agent_card
        self.mappings = normalized
        self.task_store = task_store
        self.timeout = float(timeout)
        self._sdk: Optional[Dict[str, Any]] = None
        self._request_handler: Any = None
        self._loop = _AsyncLoop()
        self._installed_intents = []

    @property
    def capabilities(self) -> tuple[CapabilityRegistration, ...]:
        return tuple(self.mappings.values())

    def is_healthy(self) -> bool:
        """Return whether A2A execution and the Agent listener are live."""

        return (
            bool(self._installed_intents)
            and self._request_handler is not None
            and self._loop.is_healthy()
            and self.server.is_healthy()
        )

    def start(self) -> None:
        if self._installed_intents:
            return
        sdk = _load_a2a()
        card_skills = {
            skill.id for skill in getattr(self.agent_card, "skills", ()) if skill.id
        }
        missing = sorted(set(self.mappings) - card_skills)
        if missing:
            raise A2ABridgeError(
                "mapped A2A skills are missing from Agent Card: "
                + ", ".join(missing)
            )
        for capability in self.mappings.values():
            if self.server.has_handler(capability.intent):
                raise A2ABridgeError(
                    f"Nexus intent already has a handler: {capability.intent}"
                )
        store = self.task_store or sdk["InMemoryTaskStore"]()
        self._request_handler = sdk["DefaultRequestHandler"](
            agent_executor=self.executor,
            task_store=store,
            agent_card=self.agent_card,
        )
        self._sdk = sdk
        self._loop.start()
        try:
            for skill, capability in self.mappings.items():
                self.server.add_handler(
                    capability.intent,
                    self._make_handler(skill),
                    stream_handler=self._make_stream_handler(skill),
                )
                self._installed_intents.append(capability.intent)
        except Exception:
            self.close()
            raise

    def _make_handler(self, skill: str) -> Any:
        def handle(envelope: AgentEnvelope) -> Any:
            return self.invoke(skill, envelope)

        return handle

    def _make_stream_handler(self, skill: str) -> Any:
        def handle(envelope: AgentEnvelope) -> Iterator[Mapping[str, Any]]:
            return self.invoke_stream(skill, envelope)

        return handle

    def _prepare_request(self, skill: str, envelope: AgentEnvelope) -> Tuple[Any, Any]:
        if self._sdk is None or self._request_handler is None:
            raise A2ABridgeError("A2A bridge is not running")
        if envelope.protocol != "a2a":
            raise AgentRequestError(
                400, "A2A_PROTOCOL_REQUIRED", "Envelope is not an A2A request"
            )
        if envelope.selector != skill:
            raise AgentRequestError(
                400, "A2A_SKILL_MISMATCH", "A2A selector does not match route"
            )
        request_data = envelope.protocol_request
        if request_data is None:
            raise AgentRequestError(
                400, "INVALID_A2A_REQUEST", "A2A SendMessageRequest is missing"
            )
        request = self._sdk["SendMessageRequest"]()
        try:
            self._sdk["ParseDict"](
                dict(request_data),
                request,
                ignore_unknown_fields=False,
            )
        except Exception as error:
            raise AgentRequestError(
                400, "INVALID_A2A_REQUEST", "invalid A2A SendMessageRequest"
            ) from error
        if not request.message.message_id:
            raise AgentRequestError(
                400, "INVALID_A2A_REQUEST", "A2A messageId is required"
            )
        context = self._sdk["ServerCallContext"](
            tenant=envelope.tenant,
            state={
                "nexus.task_id": envelope.task_id,
                "nexus.source_agent": envelope.source_agent,
                "nexus.target_agent": envelope.target_agent or "",
                "nexus.skill": skill,
            },
        )
        return request, context

    def invoke(self, skill: str, envelope: AgentEnvelope) -> Any:
        request, context = self._prepare_request(skill, envelope)
        try:
            response = self._loop.run(
                self._request_handler.on_message_send(request, context),
                self.timeout,
            )
        except TimeoutError as error:
            raise AgentRequestError(
                504, "A2A_EXECUTOR_TIMEOUT", "A2A executor timed out"
            ) from error
        except AgentRequestError:
            raise
        except Exception as error:
            raise AgentRequestError(
                502, "A2A_EXECUTION_FAILED", "A2A executor failed"
            ) from error
        return self._sdk["MessageToDict"](
            response,
            preserving_proto_field_name=False,
        )

    def invoke_stream(
        self, skill: str, envelope: AgentEnvelope
    ) -> Iterator[Mapping[str, Any]]:
        """Yield official ``StreamResponse`` objects one event at a time."""

        request, context = self._prepare_request(skill, envelope)
        assert self._sdk is not None
        assert self._request_handler is not None
        stream = self._request_handler.on_message_send_stream(request, context)
        try:
            while True:
                try:
                    event = self._loop.run(stream.__anext__(), self.timeout)
                except StopAsyncIteration:
                    break
                except TimeoutError as error:
                    raise AgentRequestError(
                        504,
                        "A2A_STREAM_TIMEOUT",
                        "A2A streaming executor timed out",
                    ) from error
                except AgentRequestError:
                    raise
                except Exception as error:
                    raise AgentRequestError(
                        502,
                        "A2A_STREAM_FAILED",
                        "A2A streaming executor failed",
                    ) from error
                field_by_type = {
                    "Task": "task",
                    "Message": "message",
                    "TaskStatusUpdateEvent": "status_update",
                    "TaskArtifactUpdateEvent": "artifact_update",
                }
                field = field_by_type.get(event.DESCRIPTOR.name)
                if field is None:
                    raise AgentRequestError(
                        502,
                        "A2A_STREAM_FAILED",
                        "A2A executor returned an unsupported stream event",
                    )
                response = self._sdk["StreamResponse"]()
                getattr(response, field).CopyFrom(event)
                yield self._sdk["MessageToDict"](
                    response,
                    preserving_proto_field_name=False,
                )
        finally:
            try:
                self._loop.run(stream.aclose(), min(self.timeout, 5.0))
            except Exception:
                pass

    def close(self) -> None:
        for intent in self._installed_intents:
            self.server.remove_handler(intent)
        self._installed_intents.clear()
        self._request_handler = None
        self._sdk = None
        self._loop.close()

    def __enter__(self) -> "A2AExecutorBridge":
        self.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    @contextmanager
    def registered(
        self,
        client: NexusAgentClient,
        *,
        auto_renew: bool = True,
        renew_fraction: float = 0.6,
        health_check: Optional[Callable[[], bool]] = None,
        reregister_on_not_found: bool = True,
    ) -> Iterator[tuple[AgentLease, ...]]:
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
            self.close()


def _required_text(name: str, value: str, maximum: int = 255) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string up to {maximum} characters")
    return value


def _absolute_http_url(name: str, value: str) -> str:
    text = _required_text(name, value, 2048)
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{name} must not contain a query or fragment")
    return text.rstrip("/")


def _part_text(parts: Sequence[Mapping[str, Any]]) -> str:
    values = []
    for part in parts:
        value = part.get("text")
        if isinstance(value, str):
            values.append(value)
    return "\n".join(values)


def _task_text(task: Mapping[str, Any]) -> str:
    values = []
    artifacts = task.get("artifacts", [])
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, Mapping):
                parts = artifact.get("parts", [])
                if isinstance(parts, list):
                    text = _part_text(parts)
                    if text:
                        values.append(text)
    return "\n".join(values)


_TERMINAL_TASK_STATES = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_FAILED",
    "TASK_STATE_REJECTED",
}


def _result_from_root(root: Mapping[str, Any]) -> A2AResult:
    message = root.get("message")
    task: Optional[Mapping[str, Any]] = None
    data: Any = None
    result_text = ""
    message_id = ""
    context_id = ""
    if isinstance(message, Mapping):
        message_id = str(message.get("messageId", ""))
        context_id = str(message.get("contextId", ""))
        parts = message.get("parts", [])
        if isinstance(parts, list):
            result_text = _part_text(parts)
            for part in parts:
                if isinstance(part, Mapping) and "data" in part:
                    data = part["data"]
                    if (
                        isinstance(data, Mapping)
                        and isinstance(data.get("status"), Mapping)
                    ):
                        task = data
                        break
    raw_task = root.get("task")
    if isinstance(raw_task, Mapping):
        task = raw_task
        data = raw_task
    if task is not None:
        message_id = message_id or str(task.get("id", ""))
        context_id = context_id or str(task.get("contextId", ""))
        result_text = _task_text(task) or result_text
    return A2AResult(
        message_id=message_id,
        context_id=context_id,
        text=result_text,
        data=data,
        task=task,
        raw=root,
    )


def _stream_event_from_root(
    root: Mapping[str, Any], *, event_id: str = ""
) -> A2AStreamEvent:
    kind = "unknown"
    body: Mapping[str, Any] = root
    task_id = ""
    context_id = ""
    state = ""
    event_text = ""
    last_chunk = False
    final = False

    for candidate, name in (
        ("task", "task"),
        ("message", "message"),
        ("statusUpdate", "status"),
        ("artifactUpdate", "artifact"),
    ):
        value = root.get(candidate)
        if isinstance(value, Mapping):
            kind = name
            body = value
            break

    if kind == "task":
        task_id = str(body.get("id", ""))
        context_id = str(body.get("contextId", ""))
        status = body.get("status")
        if isinstance(status, Mapping):
            state = str(status.get("state", ""))
        event_text = _task_text(body)
        final = state in _TERMINAL_TASK_STATES
    elif kind == "message":
        task_id = str(body.get("taskId", ""))
        context_id = str(body.get("contextId", ""))
        parts = body.get("parts")
        if isinstance(parts, list):
            event_text = _part_text(parts)
        final = True
    elif kind == "status":
        task_id = str(body.get("taskId", ""))
        context_id = str(body.get("contextId", ""))
        status = body.get("status")
        if isinstance(status, Mapping):
            state = str(status.get("state", ""))
            message = status.get("message")
            if isinstance(message, Mapping):
                parts = message.get("parts")
                if isinstance(parts, list):
                    event_text = _part_text(parts)
        final = state in _TERMINAL_TASK_STATES
    elif kind == "artifact":
        task_id = str(body.get("taskId", ""))
        context_id = str(body.get("contextId", ""))
        artifact = body.get("artifact")
        if isinstance(artifact, Mapping):
            parts = artifact.get("parts")
            if isinstance(parts, list):
                event_text = _part_text(parts)
        last_chunk = bool(body.get("lastChunk", False))

    return A2AStreamEvent(
        kind=kind,
        task_id=task_id,
        context_id=context_id,
        state=state,
        text=event_text,
        last_chunk=last_chunk,
        final=final,
        raw=root,
        event_id=event_id,
    )


class NexusA2AClient:
    """Small official-A2A caller for one router Card/Skill mapping.

    It creates the otherwise verbose edge Agent Card, official HTTP client and
    ``SendMessageRequest`` automatically, then unwraps the Nexus Adapter's Task
    data part into :class:`A2AResult`.
    """

    def __init__(
        self,
        router_url: str,
        *,
        card_id: str,
        skill: str,
        token: Optional[str] = None,
        transaction_token: Optional[str] = None,
        ca_file: Optional[str] = None,
        cert_file: Optional[str] = None,
        key_file: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.router_url = _absolute_http_url("router_url", router_url)
        self.card_id = _required_text("card_id", card_id, 95)
        self.skill = _required_text("skill", skill, 95)
        if bool(cert_file) != bool(key_file):
            raise ValueError("cert_file and key_file must be supplied together")
        if token and transaction_token:
            raise ValueError("token and transaction_token are mutually exclusive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.token = token
        self.transaction_token = transaction_token
        self.ca_file = ca_file
        self.cert_file = cert_file
        self.key_file = key_file
        self.timeout = float(timeout)
        self._sdk: Optional[Dict[str, Any]] = None
        self._http_clients: Dict[bool, Any] = {}
        self._clients: Dict[bool, Any] = {}
        self._stream_lock: Optional[asyncio.Lock] = None

    @property
    def edge_url(self) -> str:
        card = urllib.parse.quote(self.card_id, safe="")
        skill = urllib.parse.quote(self.skill, safe="")
        return f"{self.router_url}/a2a/{card}/{skill}"

    async def _get_client(self, *, streaming: bool) -> Any:
        if streaming in self._clients:
            return self._clients[streaming]
        sdk = self._sdk or _load_a2a()
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.transaction_token:
            headers["Txn-Token"] = self.transaction_token
        verify: Any = self.ca_file if self.ca_file else True
        cert: Any = None
        if self.cert_file and self.key_file:
            cert = (self.cert_file, self.key_file)
        http_client = sdk["httpx"].AsyncClient(
            headers=headers,
            verify=verify,
            cert=cert,
            timeout=self.timeout,
        )
        card = sdk["AgentCard"](
            name=f"Nexus route to {self.card_id}/{self.skill}",
            description="Nexus Router A2A capability edge",
            version="1.0.0",
            supported_interfaces=[sdk["AgentInterface"](
                protocol_binding="HTTP+JSON",
                url=self.edge_url,
                protocol_version="1.0",
            )],
            capabilities=sdk["AgentCapabilities"](streaming=streaming),
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain", "application/json"],
            skills=[sdk["AgentSkill"](
                id=self.skill,
                name=self.skill,
                description=f"Nexus mapped A2A skill {self.skill}",
                tags=["nexus"],
                input_modes=["text/plain"],
                output_modes=["text/plain", "application/json"],
            )],
        )
        config = sdk["ClientConfig"](
            streaming=streaming,
            httpx_client=http_client,
            supported_protocol_bindings=["HTTP+JSON"],
            accepted_output_modes=["text/plain", "application/json"],
        )
        client = await sdk["create_client"](
            agent=card,
            client_config=config,
        )
        self._sdk = sdk
        self._http_clients[streaming] = http_client
        self._clients[streaming] = client
        return client

    async def send(
        self,
        text: str,
        *,
        context_id: Optional[str] = None,
    ) -> A2AResult:
        """Send one text message and return a normalized result."""

        _required_text("text", text, 65536)
        sdk = self._sdk or _load_a2a()
        message = sdk["new_text_message"](
            text,
            role=sdk["Role"].ROLE_USER,
            context_id=context_id,
        )
        request = sdk["SendMessageRequest"](message=message)
        return await self.send_request(request)

    async def send_request(self, request: Any) -> A2AResult:
        """Send an advanced official ``SendMessageRequest``."""

        client = await self._get_client(streaming=False)
        assert self._sdk is not None
        events = []
        async for event in client.send_message(request):
            events.append(self._sdk["MessageToDict"](event))
        if len(events) != 1:
            raise A2ABridgeError(
                f"expected one non-streaming A2A event, received {len(events)}"
            )
        return _result_from_root(events[0])

    async def stream(
        self,
        text: str,
        *,
        context_id: Optional[str] = None,
        resume: Optional[bool] = None,
        max_reconnects: int = 3,
        reconnect_delay: float = 0.25,
    ) -> AsyncIterator[A2AStreamEvent]:
        """Yield A2A events and resume the same message task after a disconnect."""

        _required_text("text", text, 65536)
        sdk = self._sdk or _load_a2a()
        message = sdk["new_text_message"](
            text,
            role=sdk["Role"].ROLE_USER,
            context_id=context_id,
        )
        request = sdk["SendMessageRequest"](message=message)
        async for event in self.stream_request(
            request,
            resume=resume,
            max_reconnects=max_reconnects,
            reconnect_delay=reconnect_delay,
        ):
            yield event

    async def stream_request(
        self,
        request: Any,
        *,
        resume: Optional[bool] = None,
        last_event_id: int = 0,
        max_reconnects: int = 3,
        reconnect_delay: float = 0.25,
    ) -> AsyncIterator[A2AStreamEvent]:
        """Stream one official request with bounded automatic reconnection."""

        if not isinstance(last_event_id, int) or isinstance(last_event_id, bool) \
                or last_event_id < 0:
            raise ValueError("last_event_id must be a non-negative integer")
        if not 0 <= max_reconnects <= 100:
            raise ValueError("max_reconnects must be between 0 and 100")
        if not 0 <= reconnect_delay <= 60:
            raise ValueError("reconnect_delay must be between 0 and 60")
        if resume is None:
            resume = self.transaction_token is None
        if resume and self.transaction_token and max_reconnects > 0:
            raise ValueError(
                "resumable A2A streams require an access JWT; a one-time "
                "Transaction Token cannot authenticate a reconnect"
            )
        if self._stream_lock is None:
            self._stream_lock = asyncio.Lock()
        async with self._stream_lock:
            client = await self._get_client(streaming=True)
            assert self._sdk is not None
            http_client = self._http_clients[True]
            cursor = last_event_id
            reconnects = 0
            try:
                while True:
                    if cursor > 0:
                        http_client.headers["Last-Event-ID"] = str(cursor)
                    else:
                        http_client.headers.pop("Last-Event-ID", None)
                    saw_final = False
                    try:
                        async for event in client.send_message(request):
                            cursor += 1
                            root = self._sdk["MessageToDict"](
                                event,
                                preserving_proto_field_name=False,
                            )
                            normalized = _stream_event_from_root(
                                root, event_id=str(cursor)
                            )
                            saw_final = normalized.final
                            yield normalized
                            if saw_final:
                                return
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        if (
                            not resume
                            or cursor == last_event_id
                            or reconnects >= max_reconnects
                        ):
                            raise A2ABridgeError(
                                f"A2A stream disconnected and could not resume: {error}"
                            ) from error
                    else:
                        if saw_final or not resume:
                            return
                        if cursor == last_event_id:
                            raise A2ABridgeError(
                                "A2A stream ended without an event or terminal state"
                            )
                        if reconnects >= max_reconnects:
                            raise A2ABridgeError(
                                "A2A stream ended before completion and reconnect limit was reached"
                            )
                    reconnects += 1
                    if reconnect_delay > 0:
                        await asyncio.sleep(reconnect_delay)
            finally:
                http_client.headers.pop("Last-Event-ID", None)

    async def close(self) -> None:
        clients = list(self._clients.values())
        http_clients = list(self._http_clients.values())
        for client in clients:
            await client.close()
        for http_client in http_clients:
            if not getattr(http_client, "is_closed", False):
                await http_client.aclose()
        self._clients.clear()
        self._http_clients.clear()
        self._sdk = None

    async def __aenter__(self) -> "NexusA2AClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.close()


class NexusA2AAgent:
    """High-level lifecycle wrapper for an official A2A AgentExecutor."""

    def __init__(
        self,
        executor: Any,
        *,
        router: Union[str, NexusAgentClient],
        identity: str,
        endpoint: str,
        tenant: str,
        host: str = "0.0.0.0",
        port: int = 9443,
        token: Optional[str] = None,
        router_ca_file: Optional[str] = None,
        router_cert_file: Optional[str] = None,
        router_key_file: Optional[str] = None,
        cert_file: Optional[str] = None,
        key_file: Optional[str] = None,
        card_url: Optional[str] = None,
        name: str = "Nexus A2A Agent",
        description: str = "Official A2A AgentExecutor routed by Nexus",
        agent_version: str = "1.0.0",
        server: Optional[NexusAgentServer] = None,
        timeout: float = 30.0,
    ) -> None:
        if executor is None:
            raise ValueError("executor is required")
        self.executor = executor
        self.identity = _required_text("identity", identity)
        self.endpoint = _absolute_http_url("endpoint", endpoint)
        self.tenant = _required_text("tenant", tenant, 95)
        self.card_url = _absolute_http_url("card_url", card_url or endpoint)
        self.name = _required_text("name", name, 127)
        self.description = _required_text("description", description, 1024)
        self.agent_version = _required_text("agent_version", agent_version, 63)
        if isinstance(router, NexusAgentClient):
            self.router = router
        elif isinstance(router, str):
            self.router = NexusAgentClient(
                router,
                token=token,
                ca_file=router_ca_file,
                cert_file=router_cert_file,
                key_file=router_key_file,
                timeout=timeout,
            )
        else:
            raise TypeError("router must be a URL or NexusAgentClient")
        self.server = server or NexusAgentServer(
            host,
            port,
            cert_file=cert_file,
            key_file=key_file,
            request_timeout=timeout,
        )
        self.timeout = float(timeout)
        self._skills: Dict[str, _SkillDefinition] = {}
        self._bridge: Optional[A2AExecutorBridge] = None

    def expose(
        self,
        *,
        skill: str,
        intent: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        tags: Sequence[str] = ("nexus",),
        input_modes: Sequence[str] = ("text/plain",),
        output_modes: Sequence[str] = ("text/plain", "application/json"),
        version: int = 1,
        region: str = "local",
        route_id: Optional[str] = None,
        cost_microunits: int = 0,
        latency_ms: int = 0,
        trust: int = 50,
        lease_seconds: int = 30,
        public_ipv6: Optional[str] = None,
    ) -> "NexusA2AAgent":
        skill = _required_text("skill", skill, 127)
        intent = _required_text("intent", intent, 127)
        if skill in self._skills:
            raise ValueError(f"A2A skill is already exposed: {skill}")
        if any(item.capability.intent == intent for item in self._skills.values()):
            raise ValueError(f"Nexus intent is already exposed: {intent}")
        capability = CapabilityRegistration(
            intent=intent,
            origin=self.identity,
            endpoint=self.endpoint,
            tenant=self.tenant,
            version=version,
            region=region,
            route_id=route_id,
            cost_microunits=cost_microunits,
            latency_ms=latency_ms,
            trust=trust,
            lease_seconds=lease_seconds,
            public_ipv6=public_ipv6,
        )
        self._skills[skill] = _SkillDefinition(
            skill=skill,
            name=name or skill.replace("_", " ").replace("-", " ").title(),
            description=description or f"Nexus capability {intent}",
            tags=tuple(tags),
            input_modes=tuple(input_modes),
            output_modes=tuple(output_modes),
            capability=capability,
        )
        return self

    @property
    def capabilities(self) -> Tuple[CapabilityRegistration, ...]:
        return tuple(item.capability for item in self._skills.values())

    def build_card(self) -> Any:
        if not self._skills:
            raise A2ABridgeError("call expose() before starting the A2A Agent")
        sdk = _load_a2a()
        input_modes = sorted({mode for item in self._skills.values() for mode in item.input_modes})
        output_modes = sorted({mode for item in self._skills.values() for mode in item.output_modes})
        return sdk["AgentCard"](
            name=self.name,
            description=self.description,
            version=self.agent_version,
            supported_interfaces=[sdk["AgentInterface"](
                protocol_binding="HTTP+JSON",
                url=self.card_url,
                protocol_version="1.0",
            )],
            capabilities=sdk["AgentCapabilities"](streaming=True),
            default_input_modes=input_modes,
            default_output_modes=output_modes,
            skills=[sdk["AgentSkill"](
                id=item.skill,
                name=item.name,
                description=item.description,
                tags=list(item.tags),
                input_modes=list(item.input_modes),
                output_modes=list(item.output_modes),
            ) for item in self._skills.values()],
        )

    def _make_bridge(self) -> A2AExecutorBridge:
        if self._bridge is None:
            self._bridge = A2AExecutorBridge(
                self.executor,
                self.server,
                self.build_card(),
                {skill: item.capability for skill, item in self._skills.items()},
                timeout=self.timeout,
            )
        return self._bridge

    @contextmanager
    def registered(
        self,
        *,
        auto_renew: bool = True,
        renew_fraction: float = 0.6,
        health_check: Optional[Callable[[], bool]] = None,
        reregister_on_not_found: bool = True,
    ) -> Iterator[tuple[AgentLease, ...]]:
        """Install handlers and keep all exposed routes leased."""

        bridge = self._make_bridge()
        with bridge.registered(
            self.router,
            auto_renew=auto_renew,
            renew_fraction=renew_fraction,
            health_check=health_check,
            reregister_on_not_found=reregister_on_not_found,
        ) as leases:
            yield leases

    def run(
        self,
        *,
        auto_renew: bool = True,
        renew_fraction: float = 0.6,
        health_check: Optional[Callable[[], bool]] = None,
        reregister_on_not_found: bool = True,
    ) -> None:
        """Register every exposed skill, serve, then unregister on exit."""

        try:
            with self.registered(
                auto_renew=auto_renew,
                renew_fraction=renew_fraction,
                health_check=health_check,
                reregister_on_not_found=reregister_on_not_found,
            ):
                self.server.serve_forever()
        finally:
            self.server.server_close()

    def stop(self) -> None:
        self.server.shutdown()
