import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
import pathlib
import sys
import types
import unittest
from unittest import mock

SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent import (  # noqa: E402
    AgentEnvelope,
    AgentRequestError,
    CapabilityRegistration,
    CloudRegistrationManifest,
    NexusAgentServer,
)
from nexus_agent.fastmcp import (  # noqa: E402
    CurrentNexusMCP,
    CurrentNexusRun,
    FastMCPBridge,
    FastMCPBridgeError,
    FastMCPToolError,
    NexusMCPFeedback,
    NexusMCPServer,
    fastmcp_result_to_json,
)
from nexus_agent import NexusRunContext  # noqa: E402


@dataclass
class FakeTool:
    name: str
    title: str = "Echo"
    description: str = "Echo a value"
    inputSchema: object = None
    outputSchema: object = None

    def __post_init__(self):
        if self.inputSchema is None:
            self.inputSchema = {
                "type": "object",
                "properties": {"value": {}},
            }


@dataclass
class FakeContent:
    text: str
    type: str = "text"

    def model_dump(self, **_kwargs):
        return {"type": self.type, "text": self.text}


@dataclass
class FakeResult:
    data: object = None
    structured_content: object = None
    content: object = None
    is_error: bool = False


class FakeClient:
    def __init__(self, tools, result=None, progress=None):
        self.tool_list = tools
        self.result = result or FakeResult(data={"ok": True})
        self.calls = []
        self.progress = progress or []
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_args):
        self.entered = False

    async def list_tools(self):
        return self.tool_list

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        handler = kwargs.get("progress_handler")
        if handler is not None:
            for update in self.progress:
                await handler(*update)
        return self.result


class FastMCPBridgeTest(unittest.TestCase):
    def setUp(self):
        self.server = NexusAgentServer("127.0.0.1", 0)
        self.capability = CapabilityRegistration(
            intent="chip.verilog.verify.lint",
            origin="agent://demo/linter",
            endpoint=f"http://127.0.0.1:{self.server.port}/invoke",
            tenant="demo",
        )

    def tearDown(self):
        self.server.server_close()

    def envelope(self, payload, *, intent="chip.verilog.verify.lint"):
        return AgentEnvelope(
            version="1.0",
            intent=intent,
            intent_version=1,
            task_id="task-fastmcp-1",
            source_agent="agent://demo/caller",
            tenant="demo",
            hop_limit=7,
            payload=payload,
            target_agent="agent://demo/linter",
            route_id="route-fastmcp-1",
        )

    def bridge(self, client):
        return FastMCPBridge(
            object(),
            self.server,
            {"lint_verilog": self.capability},
            client_factory=lambda _mcp: client,
        )

    def test_bridge_health_requires_tool_loop_and_agent_listener(self):
        bridge = self.bridge(FakeClient([FakeTool("lint_verilog")]))
        bridge.start()
        self.assertFalse(bridge.is_healthy())
        thread = self.server.serve_in_thread()
        try:
            self.assertTrue(bridge.is_healthy())
            bridge.stop()
            self.assertFalse(bridge.is_healthy())
        finally:
            self.server.shutdown()
            thread.join(timeout=2)
            bridge.stop()

    def test_enumerates_maps_and_invokes_direct_payload(self):
        client = FakeClient([FakeTool("lint_verilog")], FakeResult(data={"errors": 0}))
        bridge = self.bridge(client)
        bridge.start()
        try:
            self.assertEqual([tool.name for tool in bridge.tools], ["lint_verilog"])
            self.assertTrue(self.server.has_handler(self.capability.intent))
            result = bridge.handle(self.envelope({"source": "module top; endmodule"}))
            self.assertEqual(result, {"errors": 0})
            name, arguments, options = client.calls[0]
            self.assertEqual(name, "lint_verilog")
            self.assertEqual(arguments, {"source": "module top; endmodule"})
            self.assertEqual(options["meta"]["nexus.task_id"], "task-fastmcp-1")
            self.assertEqual(
                options["meta"]["nexus.target_agent"], "agent://demo/linter"
            )
            self.assertFalse(options["raise_on_error"])
        finally:
            bridge.stop()
        self.assertFalse(client.entered)
        self.assertFalse(self.server.has_handler(self.capability.intent))

    def test_reuses_fastmcp_metadata_for_implicit_cloud_tool(self):
        capability = CapabilityRegistration(
            intent=self.capability.intent,
            origin=self.capability.origin,
            endpoint=self.capability.endpoint,
            tenant=self.capability.tenant,
            cloud=CloudRegistrationManifest(
                publish=True,
                agent_name="Cloud linter",
            ),
        )
        tool = FakeTool(
            "lint_verilog",
            title="Verilog lint",
            description="Validate synthesizable Verilog.",
            inputSchema={
                "type": "object",
                "properties": {"source": {"type": "string"}},
                "required": ["source"],
            },
        )
        bridge = FastMCPBridge(
            object(),
            self.server,
            {"lint_verilog": capability},
            client_factory=lambda _mcp: FakeClient([tool]),
        )
        bridge.start()
        try:
            published = bridge.mappings[0].capability.cloud.tool
            self.assertEqual(published.name, "lint_verilog")
            self.assertEqual(published.title, "Verilog lint")
            self.assertEqual(published.description, "Validate synthesizable Verilog.")
            self.assertEqual(
                published.input_schema["properties"]["source"],
                {"type": "string"},
            )
        finally:
            bridge.stop()

    def test_extracts_mcp_tools_call_arguments(self):
        client = FakeClient([FakeTool("lint_verilog")])
        bridge = self.bridge(client)
        payload = {
            "protocol": "mcp",
            "selector": "lint_verilog",
            "request": {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "lint_verilog",
                    "arguments": {"source": "module top; endmodule"},
                },
            },
        }
        try:
            self.assertEqual(bridge.handle(self.envelope(payload)), {"ok": True})
            self.assertEqual(
                client.calls[0][1], {"source": "module top; endmodule"}
            )
        finally:
            bridge.stop()

    def test_rejects_mcp_selector_mismatch_before_call(self):
        client = FakeClient([FakeTool("lint_verilog")])
        bridge = self.bridge(client)
        payload = {
            "protocol": "mcp",
            "selector": "other_tool",
            "request": {
                "method": "tools/call",
                "params": {"name": "other_tool", "arguments": {}},
            },
        }
        with self.assertRaises(AgentRequestError) as captured:
            bridge.handle(self.envelope(payload))
        self.assertEqual(captured.exception.code, "MCP_TOOL_MISMATCH")
        self.assertEqual(client.calls, [])

    def test_async_call_uses_shared_client(self):
        client = FakeClient([FakeTool("lint_verilog")], FakeResult(data=42))
        bridge = self.bridge(client)

        async def invoke():
            return await bridge.call_tool("lint_verilog", {"source": "x"})

        try:
            self.assertEqual(asyncio.run(invoke()), 42)
        finally:
            bridge.stop()

    def test_missing_mapped_tool_fails_discovery_and_closes_client(self):
        client = FakeClient([FakeTool("another_tool")])
        bridge = self.bridge(client)
        with self.assertRaises(FastMCPBridgeError) as captured:
            bridge.start()
        self.assertIn("lint_verilog", str(captured.exception))
        self.assertFalse(client.entered)
        self.assertFalse(bridge.started)

    def test_tool_error_becomes_bounded_agent_error(self):
        result = FakeResult(
            content=[FakeContent("source does not parse")],
            is_error=True,
        )
        bridge = self.bridge(FakeClient([FakeTool("lint_verilog")], result))
        try:
            with self.assertRaises(AgentRequestError) as captured:
                bridge.handle(self.envelope({"source": "bad"}))
            self.assertEqual(captured.exception.status, 502)
            self.assertEqual(captured.exception.code, "FASTMCP_TOOL_FAILED")
            self.assertIn("does not parse", captured.exception.message)
        finally:
            bridge.stop()

    def test_existing_intent_handler_is_not_overwritten(self):
        self.server.add_handler(self.capability.intent, lambda _envelope: {})
        bridge = self.bridge(FakeClient([FakeTool("lint_verilog")]))
        with self.assertRaises(FastMCPBridgeError):
            bridge.start()

    def test_streams_progress_and_one_terminal_result(self):
        client = FakeClient(
            [FakeTool("lint_verilog")],
            FakeResult(data={"errors": 0}),
            progress=[
                (1.0, 2.0, "parsing"),
                (2.0, 2.0, "complete"),
            ],
        )
        bridge = self.bridge(client)
        try:
            events = list(bridge.handle_stream(self.envelope({"source": "ok"})))
        finally:
            bridge.stop()
        self.assertEqual(
            [event.event for event in events],
            ["progress", "progress", "result"],
        )
        self.assertEqual([event.event_id for event in events], ["1", "2", "3"])
        self.assertEqual(json.loads(events[0].data), {
            "progress": 1.0,
            "total": 2.0,
            "message": "parsing",
        })
        self.assertEqual(json.loads(events[-1].data), {
            "result": {"errors": 0},
        })
        options = client.calls[0][2]
        self.assertTrue(callable(options["progress_handler"]))
        self.assertEqual(
            options["meta"]["nexus.source_agent"], "agent://demo/caller"
        )

    def test_progress_limit_becomes_terminal_stream_error(self):
        client = FakeClient(
            [FakeTool("lint_verilog")],
            progress=[(1.0, 3.0, "one"), (2.0, 3.0, "two")],
        )
        bridge = FastMCPBridge(
            object(),
            self.server,
            {"lint_verilog": self.capability},
            max_stream_events=2,
            client_factory=lambda _mcp: client,
        )
        try:
            events = list(bridge.handle_stream(self.envelope({"source": "ok"})))
        finally:
            bridge.stop()
        self.assertEqual([event.event for event in events], ["progress", "error"])
        error = json.loads(events[-1].data)
        self.assertEqual(error["code"], "FASTMCP_STREAM_FAILED")
        self.assertIn("limit", error["message"])

    def test_stream_tool_error_is_bounded_event(self):
        client = FakeClient(
            [FakeTool("lint_verilog")],
            FakeResult(content=[FakeContent("bad input")], is_error=True),
        )
        bridge = self.bridge(client)
        try:
            events = list(bridge.handle_stream(self.envelope({"source": "bad"})))
        finally:
            bridge.stop()
        self.assertEqual([event.event for event in events], ["error"])
        self.assertEqual(
            json.loads(events[0].data)["code"], "FASTMCP_TOOL_FAILED"
        )


class FastMCPResultTest(unittest.TestCase):
    def test_hydrated_result_is_json_normalized(self):
        result = FakeResult(data={
            "at": datetime(2026, 8, 5, 7, 30, tzinfo=timezone.utc),
            "blob": b"abc",
        })
        self.assertEqual(fastmcp_result_to_json(result), {
            "at": "2026-08-05T07:30:00+00:00",
            "blob": {"encoding": "base64", "data": "YWJj"},
        })

    def test_content_blocks_are_preserved_without_structured_data(self):
        result = FakeResult(content=[FakeContent("done")])
        self.assertEqual(fastmcp_result_to_json(result), {
            "content": [{"type": "text", "text": "done"}],
        })

    def test_error_result_raises(self):
        with self.assertRaises(FastMCPToolError):
            fastmcp_result_to_json(FakeResult(
                content=[FakeContent("failed")], is_error=True
            ))


class NexusMCPServerTest(unittest.TestCase):
    def fastmcp_modules(self):
        fastmcp = types.ModuleType("fastmcp")
        dependencies = types.ModuleType("fastmcp.dependencies")
        server_dependencies = types.ModuleType("fastmcp.server.dependencies")

        class FakeFastMCP:
            def __init__(self, name, **options):
                self.name = name
                self.options = options
                self.tools = {}
                self.run_calls = []
                self.http_paths = []

            def tool(self, *, name):
                def register(function):
                    self.tools[name] = function
                    return function
                return register

            def run(self, **options):
                self.run_calls.append(options)
                return options

            def http_app(self, path="/mcp", transport="http"):
                self.http_paths.append(path)
                return ("legacy", path, "/messages/") if transport == "sse" else ("modern", path)

        fastmcp.FastMCP = FakeFastMCP
        dependencies.Depends = lambda function: function
        server_dependencies.get_http_headers = lambda: {}
        server_dependencies.get_context = lambda: object()
        return {
            "fastmcp": fastmcp,
            "fastmcp.dependencies": dependencies,
            "fastmcp.server.dependencies": server_dependencies,
        }

    def test_workspace_tool_catalog_is_explicit_allowlist(self):
        with mock.patch.dict(sys.modules, self.fastmcp_modules()):
            server = NexusMCPServer("Workspace Agent")
            server.enable_workspace_tools({"connection.list", "files.read"})
            self.assertEqual(
                set(server.fastmcp.tools),
                {"nexus_workspace_connections_list", "nexus_workspace_file_read"},
            )
            server.enable_workspace_tools({"files.read", "command.execute"})
            self.assertEqual(
                set(server.fastmcp.tools),
                {
                    "nexus_workspace_connections_list",
                    "nexus_workspace_file_read",
                    "nexus_workspace_command_execute",
                },
            )
            with self.assertRaises(ValueError):
                server.enable_workspace_tools({"connection.read_secret"})

    def test_streamable_http_is_default_and_legacy_is_opt_in(self):
        with mock.patch.dict(sys.modules, self.fastmcp_modules()):
            server = NexusMCPServer("Transport Agent")
            result = server.run(host="0.0.0.0", port=8000)
            self.assertEqual(result["transport"], "http")
            self.assertEqual(server.http_app(), ("modern", "/mcp"))
            combined = server.http_app(legacy_sse=True)
            self.assertEqual(combined.modern, ("modern", "/mcp"))
            self.assertEqual(combined.legacy, ("legacy", "/sse", "/messages/"))

    def test_tool_policy_metadata_is_explicit_and_continuable_requires_task(self):
        with mock.patch.dict(sys.modules, self.fastmcp_modules()):
            server = NexusMCPServer("Interactive Agent")

            @server.tool(task=True, continuable=True, demo=True)
            async def interact():
                return "ok"

            self.assertIn("interact", server.fastmcp.tools)
            self.assertEqual(
                server.nexus_tool_policy["interact"],
                {"task": True, "continuable": True, "demo": True, "recovery_protocol": 1},
            )
            @server.tool(chat=True)
            async def chat_turn():
                return "ok"

            self.assertEqual(
                server.nexus_tool_policy["chat_turn"],
                {
                    "task": True,
                    "continuable": False,
                    "demo": False,
                    "recovery_protocol": 1,
                    "chat": True,
                    "interactive": True,
                },
            )
            with self.assertRaisesRegex(ValueError, "only one chat tool"):
                server.tool(chat=True)(lambda: None)
            with self.assertRaises(ValueError):
                server.tool(task=False, continuable=True)(lambda: None)

    def test_current_run_dependencies_are_context_managers(self):
        with mock.patch.dict(sys.modules, self.fastmcp_modules()):
            combined_dependency = CurrentNexusMCP()
            run_dependency = CurrentNexusRun()

            combined_manager = combined_dependency()
            run_manager = run_dependency()
            self.assertTrue(hasattr(combined_manager, "__enter__"))
            self.assertTrue(hasattr(run_manager, "__enter__"))

            with combined_manager as combined:
                self.assertIsInstance(combined.run, NexusRunContext)
                self.assertTrue(callable(combined.terminal.run))
                self.assertTrue(callable(combined.workspace.list))
            with run_manager as run:
                self.assertIsInstance(run, NexusRunContext)

    def test_feedback_is_dual_channel_and_fail_open(self):
        class MCPContext:
            def __init__(self):
                self.progress = []
                self.logs = []

            async def report_progress(self, **value):
                self.progress.append(value)

            async def info(self, message):
                self.logs.append(message)

        mcp = MCPContext()
        run = NexusRunContext()
        feedback = NexusMCPFeedback(mcp, run)

        async def exercise():
            await feedback.progress(1, 3, "Reading workspace")
            await feedback.log("info", "Analysis completed token=must-not-leak")

        asyncio.run(exercise())
        self.assertEqual(mcp.progress[0]["progress"], 1)
        self.assertEqual(mcp.logs, ["Analysis completed token=[redacted]"])


if __name__ == "__main__":
    unittest.main()
