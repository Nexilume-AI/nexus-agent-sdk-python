import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from nexus_agent import McpToolResult
from nexus_agent.server import _mcp_json_default


class MCPResultTests(unittest.TestCase):
    def test_native_blocks_and_business_error_are_explicit(self):
        value = McpToolResult(content=[
            {"type": "text", "text": "工具"},
            {"type": "image", "data": "YWJj", "mimeType": "image/png"},
            {"type": "resource", "resource": {"uri": "test://asset", "text": "body"}},
        ], structured_content={"marker": "工具"}, is_error=True)
        wire = json.loads(json.dumps(value, default=_mcp_json_default))
        self.assertEqual(wire["nexus_mcp_result_version"], 1)
        self.assertTrue(wire["result"]["isError"])
        self.assertEqual([b["type"] for b in wire["result"]["content"]], ["text", "image", "resource"])
        self.assertEqual(wire["result"]["structuredContent"], {"marker": "工具"})

    def test_ordinary_business_object_is_not_reinterpreted(self):
        business = {"content": [{"type": "text", "text": "business"}], "isError": True}
        self.assertEqual(json.loads(json.dumps(business, default=_mcp_json_default)), business)

    def test_result_validation(self):
        for content in ([{"type": "unknown"}], [{"type": "image", "data": "bad"}], [{"type": "text", "text": 1}]):
            with self.assertRaises(ValueError):
                McpToolResult(content=content)
        with self.assertRaises(ValueError):
            McpToolResult(content=[], is_error="false")
        with self.assertRaises(ValueError):
            McpToolResult(content=[], structured_content={"bad": float("nan")})

    def test_inputs_are_snapshotted_not_mutated(self):
        content = [{"type": "text", "text": "first"}]
        result = McpToolResult(content=content)
        content[0]["text"] = "changed"
        wire = result.to_dict()
        wire["content"][0]["text"] = "changed again"
        self.assertEqual(result.to_dict()["content"][0]["text"], "first")

    def test_fastmcp_conversion_keeps_error_and_structured_content(self):
        from nexus_agent.fastmcp import fastmcp_result_to_mcp
        value = fastmcp_result_to_mcp({"content": [{"type": "text", "text": "rejected"}],
                                      "structuredContent": {"reason": "business"}, "isError": True})
        self.assertTrue(value.to_dict()["isError"])
        self.assertEqual(value.to_dict()["structuredContent"], {"reason": "business"})

    def test_pydantic_optional_annotations_can_be_null(self):
        value = McpToolResult(content=[{"type": "text", "text": "ok", "annotations": None}])
        self.assertNotIn("annotations", value.to_dict()["content"][0])

    def test_hosted_sync_and_async_facade_preserve_native_result(self):
        import asyncio
        from unittest.mock import patch
        from fastmcp import Client
        from nexus_agent import NexusAgent, NexusRunContext
        agent = NexusAgent(runtime="hosted", agent_id="native-result-test")
        @agent.capability("native_sync")
        def sync_result(arguments):
            return McpToolResult(content=[{"type": "image", "data": "YWJj", "mimeType": "image/png"}],
                                 structured_content={"marker": "sync"})
        @agent.capability("native_async")
        async def async_result(arguments):
            return McpToolResult(content=[{"type": "text", "text": "business rejection"}], is_error=True)
        async def run():
            async with Client(agent.as_mcp_server().fastmcp) as client:
                value = await client.call_tool("native_sync", {})
                self.assertEqual(value.content[0].type, "image")
                self.assertEqual(value.structured_content, {"marker": "sync"})
                value = await client.call_tool("native_async", {}, raise_on_error=False)
                self.assertTrue(value.is_error)
                self.assertEqual(value.content[0].text, "business rejection")
        with patch("nexus_agent.hosted.NexusRunContext.from_env", side_effect=lambda **_: NexusRunContext()):
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
