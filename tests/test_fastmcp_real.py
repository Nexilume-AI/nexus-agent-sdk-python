import asyncio
import json
import pathlib
import sys
import unittest
import urllib.request
import time

SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

try:
    from fastmcp import Context, FastMCP
except ImportError:
    FastMCP = None
    Context = None

from nexus_agent import CapabilityRegistration, NexusAgentServer  # noqa: E402
from nexus_agent.fastmcp import FastMCPBridge  # noqa: E402


@unittest.skipIf(FastMCP is None, "FastMCP optional dependency is not installed")
class RealFastMCPTest(unittest.TestCase):
    def test_real_async_tool_discovery_and_two_invoke_paths(self):
        mcp = FastMCP("Nexus acceptance")

        @mcp.tool
        async def add(a: int, b: int, ctx: Context) -> dict:
            """Add two integers."""

            await ctx.report_progress(1, 2, "validated")
            await asyncio.sleep(0.25)
            await ctx.report_progress(2, 2, "computed")
            return {"total": a + b}

        server = NexusAgentServer("127.0.0.1", 0)
        capability = CapabilityRegistration(
            intent="demo.math.add",
            origin="agent://demo/math",
            endpoint=f"http://127.0.0.1:{server.port}/invoke",
            tenant="demo",
        )
        bridge = FastMCPBridge(mcp, server, {"add": capability})

        def post(payload):
            envelope = {
                "version": "1.0",
                "intent": "demo.math.add",
                "intent_version": 1,
                "task_id": "task-real-fastmcp",
                "source_agent": "agent://demo/caller",
                "tenant": "demo",
                "hop_limit": 7,
                "constraints": {},
                "payload": payload,
            }
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.port}/invoke",
                data=json.dumps(envelope).encode("utf-8"),
                headers={
                    "Content-Type": "application/vnd.nexus.agent-envelope+json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read())

        def post_stream(payload):
            envelope = {
                "version": "1.0",
                "intent": "demo.math.add",
                "intent_version": 1,
                "task_id": "task-real-fastmcp-stream",
                "source_agent": "agent://demo/caller",
                "tenant": "demo",
                "hop_limit": 7,
                "constraints": {},
                "payload": payload,
            }
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.port}/invoke",
                data=json.dumps(envelope).encode("utf-8"),
                headers={
                    "Content-Type": "application/vnd.nexus.agent-envelope+json",
                    "Accept": "text/event-stream",
                },
                method="POST",
            )
            started = time.monotonic()
            events = []
            first_at = None
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(
                    response.headers.get_content_type(), "text/event-stream"
                )
                current = {}
                for raw_line in response:
                    line = raw_line.decode("utf-8").rstrip("\r\n")
                    if not line:
                        if "data" in current:
                            if first_at is None:
                                first_at = time.monotonic()
                            events.append(current)
                        current = {}
                    elif line.startswith("event: "):
                        current["event"] = line[7:]
                    elif line.startswith("data: "):
                        current["data"] = json.loads(line[6:])
            completed = time.monotonic()
            return events, first_at - started, completed - started

        bridge.start()
        thread = server.serve_in_thread()
        try:
            self.assertEqual([tool.name for tool in bridge.tools], ["add"])
            self.assertEqual(
                bridge.tools[0].input_schema["required"], ["a", "b"]
            )
            self.assertEqual(post({"a": 2, "b": 3}), {
                "total": 5,
            })
            self.assertEqual(post({
                "protocol": "mcp",
                "selector": "add",
                "request": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "add", "arguments": {"a": 7, "b": 8}},
                },
            }), {"total": 15})
            stream_events, first_delay, total_delay = post_stream({
                "protocol": "mcp",
                "selector": "add",
                "request": {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "add", "arguments": {"a": 20, "b": 22}},
                },
            })
            self.assertEqual(
                [event["event"] for event in stream_events],
                ["progress", "progress", "result"],
            )
            self.assertEqual(stream_events[0]["data"]["message"], "validated")
            self.assertEqual(stream_events[-1]["data"], {"result": {"total": 42}})
            self.assertLess(first_delay, total_delay - 0.15)
        finally:
            server.shutdown()
            bridge.stop()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
