"""Same native handlers over edge Invoke and real FastMCP transports."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import socket
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nexus_agent import NexusAgent, NexusRunContext, McpToolDescriptor, SseEvent, current_run
from nexus_agent.errors import NexusAgentError


def test_hosted_declaration_never_discovers_authenticates_or_listens():
    with mock.patch.dict(os.environ, {"NEXUS_AGENT_RUNTIME_MODE": "hosted"}), \
         mock.patch("nexus_agent.agent.resolve_router_url", side_effect=AssertionError("discovery")), \
         mock.patch("nexus_agent.agent.NexusAgentServer", side_effect=AssertionError("socket")), \
         mock.patch("nexus_agent.agent.AutoTokenProvider", side_effect=AssertionError("auth")):
        agent = NexusAgent(router="auto", cloud_name="Dual runtime")
        @agent.capability("echo")
        def echo(payload):
            return payload
        assert agent.runtime_mode == "hosted"
        assert agent.server.handlers
        with pytest.raises(NexusAgentError, match="do not register"):
            agent.registrations()
        with pytest.raises(NexusAgentError, match="edge endpoint"):
            _ = agent.backend_endpoint
        for method in (agent.invoke, agent.invoke_async, agent.invoke_interactive, agent.invoke_stream):
            with pytest.raises(NexusAgentError, match="Router-to-Router"):
                method("test", {})


def test_trusted_boot_selects_hosted_before_importing_unmodified_native_source():
    source = Path(__file__).parents[1] / "examples" / "dual_runtime_agent.py"
    boot = Path(os.environ.get("NEXUS_SERVER_SOURCE", "__server_source_not_configured__")) / "apps" / "agents" / "python_profile" / "nexus_boot.py"
    if not boot.is_file():
        pytest.skip("Requires the separate Nexus Server source checkout")
    spec = importlib.util.spec_from_file_location("test_nexus_boot", boot)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    locate = importlib.util.spec_from_file_location
    def redirect(name, path, *args, **kwargs):
        if name != "uploaded_agent":
            return locate(name, path, *args, **kwargs)
        assert os.environ["NEXUS_AGENT_RUNTIME_MODE"] == "hosted"
        assert path == "/opt/nexus-python/agent.py"
        return locate(name, source)
    with mock.patch.dict(os.environ, {"NEXUS_AGENT_RUNTIME_MODE": "openwrt"}), \
         mock.patch("importlib.util.spec_from_file_location", side_effect=redirect), \
         mock.patch("nexus_agent.agent.resolve_router_url", side_effect=AssertionError("Router accessed")):
        server = module.load_server("agent")
        assert server.nexus_tool_policy["assist"]["interactive"]


def test_router_discovery_failure_does_not_silently_enable_hosting():
    with mock.patch.dict(os.environ, {"NEXUS_AGENT_RUNTIME_MODE": "openwrt"}), \
         mock.patch("nexus_agent.agent.resolve_router_url", side_effect=NexusAgentError("discovery failed")):
        with pytest.raises(NexusAgentError, match="discovery failed"):
            NexusAgent()


def test_real_mcp_sync_async_stream_context_and_policy():
    from fastmcp import Client
    from fastmcp.exceptions import ToolError
    agent = NexusAgent(runtime="hosted", computer_requirement="optional",
                       workspace_capabilities=("files.read",), mobile_requirement="required",
                       mobile_capabilities=("mobile.observe",))
    policy = McpToolDescriptor(name="inspect", task=True, continuable=True, interactive=True,
                              mobile_scopes=("mobile.observe",), input_schema={"type": "object",
                              "properties": {"content": {"type": "string"}}, "required": ["content"]})
    @agent.capability("inspect", tool=policy)
    def inspect_files(payload, ctx: NexusRunContext):
        assert current_run() is ctx
        ctx.plan.set([{"id": "one", "title": "Inspect", "status": "running"}])
        ctx.chat.say(payload["content"])
        return {"message": payload["content"], "run_id": ctx.run_id}

    @agent.capability("async")
    async def echo_async(payload, ctx: NexusRunContext):
        await asyncio.sleep(0.01)
        assert current_run() is ctx
        return {"run_id": ctx.run_id}

    @agent.capability("envelope", pass_envelope=True)
    def echo_envelope(envelope):
        return {"name": envelope.protocol_request["params"]["name"], "intent": envelope.intent}

    @agent.stream_capability("sync_stream")
    def sync_stream(payload, ctx: NexusRunContext):
        assert current_run() is ctx
        yield SseEvent(event="progress", data='{"progress":1}')
        yield SseEvent(event="result", data=json.dumps({"message": "同步完成"}))

    @agent.stream_capability("async_stream")
    async def async_stream(payload, ctx: NexusRunContext):
        await asyncio.sleep(0)
        assert current_run() is ctx
        yield SseEvent(event="result", data=json.dumps({"message": "异步完成"}))

    @agent.capability("hidden", tool=False)
    def hidden(payload):
        raise AssertionError("never exported")

    @agent.capability("fail")
    def fail(payload):
        raise ToolError("secret-in-arbitrary-error")

    @agent.capability("image")
    def image(payload):
        return {"content": [{"type": "image", "mimeType": "image/png", "data": "aW1hZ2U="}]}

    events, contexts = [], []
    def context_factory(**_):
        ctx = NexusRunContext(run_id=f"run-{len(contexts)}", _event_sink=lambda event: events.append(event) or True)
        contexts.append(ctx)
        return ctx

    async def run():
        async with Client(agent.as_mcp_server().fastmcp) as client:
            tools = await client.list_tools()
            assert len(tools) == 7
            tool = next(t for t in tools if t.name == "inspect")
            assert tool.inputSchema == policy.input_schema
            assert tool.meta["nexus"]["continuable"] is True
            assert tool.meta["nexus"]["agent_contract"]["mobile"]["requirement"] == "required"
            first, second = await asyncio.gather(client.call_tool("inspect", {"content": "中文🙂"}), client.call_tool("async", {}))
            assert first.data["message"] == "中文🙂"
            assert first.data["run_id"] != second.data["run_id"]
            assert (await client.call_tool("envelope", {})).data == {"name": "envelope", "intent": "envelope"}
            assert (await client.call_tool("sync_stream", {})).data["message"] == "同步完成"
            assert (await client.call_tool("async_stream", {})).data["message"] == "异步完成"
            image = await client.call_tool("image", {})
            assert image.content[0].type == "image"
            assert image.content[0].data == "aW1hZ2U="
            bad = await client.call_tool("inspect", {}, raise_on_error=False)
            assert bad.is_error
            bad = await client.call_tool("fail", {}, raise_on_error=False)
            assert bad.is_error
            assert "secret-in-arbitrary-error" not in str(bad)
    with mock.patch("nexus_agent.hosted.NexusRunContext.from_env", side_effect=context_factory):
        asyncio.run(run())
    assert events
    assert agent.as_mcp_server() is agent.as_mcp_server()
    with pytest.raises(NexusAgentError, match="Declare all capabilities"):
        agent.capability("too_late")(lambda payload: payload)


def test_duplicate_names_and_no_exported_tools_fail_closed():
    for duplicate in (False, True):
        agent = NexusAgent(runtime="hosted")
        for intent in ("one", "two"):
            agent.capability(intent, tool=McpToolDescriptor(name="same") if duplicate else False)(lambda p: p)
        with pytest.raises(NexusAgentError):
            agent.as_mcp_server()


def test_same_example_imports_without_router_and_main_does_not_run():
    source = Path(__file__).parents[1] / "examples" / "dual_runtime_agent.py"
    with mock.patch.dict(os.environ, {"NEXUS_AGENT_RUNTIME_MODE": "hosted"}), \
         mock.patch("nexus_agent.agent.resolve_router_url", side_effect=AssertionError("Router accessed")):
        spec = importlib.util.spec_from_file_location("dual_runtime_example", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.agent.as_mcp_server().nexus_tool_policy["assist"]["continuable"]


def test_same_source_over_real_http_mcp_and_edge_invoke_with_chat():
    """No Docker/Router hardware substitutes: this tests the actual HTTP adapters.

    The fake Cloud callback supplies only a per-Run Chat reply and records real
    urllib Reporter requests. No business handler or MCP transport is mocked.
    """
    import uvicorn
    from fastmcp import Client
    source = Path(__file__).parents[1] / "examples" / "dual_runtime_agent.py"
    events, questions = [], []
    class CloudCallback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def reply(self, body):
            data = json.dumps({"ok": True, "data": body}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            caller = self.path.split("/")[1]
            if self.path.endswith("/events"):
                assert self.headers["X-Nexus-AGUI-Event-Id"]
                events.append((caller, payload))
                self.reply({"id": "event"})
            else:
                assert self.headers["X-Nexus-Interaction-Token"] == "test-interaction-" + caller
                questions.append((caller, payload))
                self.reply({"id": "question"})
        def do_GET(self):
            self.reply({"status": "answered", "response": {"value": "continue"}})
    callback = ThreadingHTTPServer(("127.0.0.1", 0), CloudCallback)
    callback_thread = threading.Thread(target=callback.serve_forever, daemon=True)
    callback_thread.start()
    modules = []
    def load(mode):
        with mock.patch.dict(os.environ, {"NEXUS_AGENT_RUNTIME_MODE": mode}), \
             mock.patch("nexus_agent.agent.resolve_router_url", return_value="http://127.0.0.1:1"), \
             mock.patch("nexus_agent.agent.resolve_advertise_address", return_value="127.0.0.1"):
            spec = importlib.util.spec_from_file_location("dual_" + mode, source)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules.append(module)
            return module.agent
    def headers(caller):
        base = f"http://127.0.0.1:{callback.server_port}/{caller}"
        return {"X-Nexus-AGUI-Run-Id": caller, "X-Nexus-AGUI-Events-Url": base + "/events",
                "X-Nexus-AGUI-Token": "test-events-" + caller,
                "X-Nexus-Interaction-Url": base + "/interactions/", "X-Nexus-Interaction-Token": "test-interaction-" + caller,
                "X-Nexus-Interaction-Mode": "stream", "X-Nexus-Run-Turn": "1"}
    hosted = load("hosted")
    edge = load("openwrt")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = uvicorn.Config(hosted.as_mcp_server().http_app(), log_level="critical")
    serving = uvicorn.Server(config)
    mcp_thread = threading.Thread(target=serving.run, kwargs={"sockets": [listener]}, daemon=True)
    edge_thread = edge.server.serve_in_thread(daemon=True)
    mcp_thread.start()
    try:
        deadline = time.monotonic() + 10
        while not serving.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert serving.started
        async def call(caller):
            from fastmcp.client.transports import StreamableHttpTransport
            async with Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", headers=headers(caller))) as client:
                tools = await client.list_tools()
                assert [t.name for t in tools] == ["assist"]
                result = await client.call_tool("assist", {"content": "您好🙂 " + caller})
                assert result.data["selection"] == "continue"
        async def calls():
            await asyncio.gather(call("caller-a"), call("caller-b"))
        asyncio.run(calls())
        envelope = {"version": "1.0", "intent": "assist", "intent_version": 1, "task_id": "edge-call",
                    "source_agent": "agent://test/caller", "tenant": "default", "hop_limit": 1,
                    "payload": {"content": "您好🙂 edge"}}
        request = urllib.request.Request(f"http://127.0.0.1:{edge.server.port}/invoke",
            data=json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8", **headers("edge")})
        with urllib.request.urlopen(request, timeout=10) as response:
            assert json.load(response)["selection"] == "continue"
        assert {caller for caller, _ in questions} == {"caller-a", "caller-b", "edge"}
        for caller in ("caller-a", "caller-b", "edge"):
            selected = [event for owner, event in events if owner == caller]
            assert any(event["type"] == "ACTIVITY_SNAPSHOT" for event in selected)
            assert any(event["type"] == "TEXT_MESSAGE_END" for event in selected)
            for other in {"caller-a", "caller-b", "edge"} - {caller}:
                assert "您好🙂 " + other not in json.dumps(selected, ensure_ascii=False)
    finally:
        serving.should_exit = True
        mcp_thread.join(timeout=10)
        listener.close()
        edge.server.shutdown()
        edge.server.server_close()
        edge_thread.join(timeout=3)
        callback.shutdown()
        callback.server_close()
        callback_thread.join(timeout=3)
