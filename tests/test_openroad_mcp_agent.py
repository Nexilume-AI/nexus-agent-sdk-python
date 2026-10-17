import copy
import importlib.util
import json
import pathlib
import sys
import unittest
import tempfile
import os
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("openroad_bridge", ROOT / "examples/openroad_mcp_agent/agent.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class OpenRoadRegistrationTests(unittest.TestCase):
    def setUp(self):
        fixture = pathlib.Path(__file__).parent / "fixtures/openroad_mcp_tools_1_1_0.json"
        self.tools = json.loads(fixture.read_text(encoding="utf-8-sig"))["tools"]

    def test_all_upstream_tools_keep_schema_constraints_and_full_description(self):
        self.assertEqual(len(self.tools), 15)
        for tool in self.tools:
            with self.subTest(tool=tool["name"]):
                before = copy.deepcopy(tool)
                descriptor = bridge.describe_tool(tool)
                self.assertEqual(tool, before)
                self.assertEqual(descriptor.name, tool["name"])
                for key, value in tool["inputSchema"].items():
                    self.assertEqual(descriptor.input_schema[key], value)
                self.assertIn(tool["description"], descriptor.input_schema["description"])
                self.assertTrue(descriptor.description)
                self.assertTrue(descriptor.input_schema["examples"])

    def test_paginated_discovery_registers_new_tools_without_code_changes(self):
        extra = {"name": "new_eda_tool", "description": "A newly installed tool.",
                 "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}
        mcp = Mock()
        mcp.request.side_effect = [{"tools": self.tools, "nextCursor": "page2"}, {"tools": [extra]}]
        tools = bridge.discover_tools(mcp)
        self.assertEqual(len(tools), 16)
        self.assertEqual(bridge.describe_tool(tools[extra["name"]]).input_schema["required"], ["path"])
        self.assertEqual(mcp.request.call_args.args, ("tools/list", {"cursor": "page2"}))

    def test_duplicate_tools_and_repeated_cursors_fail_explicitly(self):
        for pages in (
            [{"tools": [self.tools[0], self.tools[0]]}],
            [{"tools": [self.tools[0]], "nextCursor": "same"}, {"tools": [], "nextCursor": "same"}],
        ):
            mcp = Mock()
            mcp.request.side_effect = pages
            with self.assertRaises(ValueError):
                bridge.discover_tools(mcp)

    def test_oversized_schema_is_rejected_instead_of_losing_constraints(self):
        tool = copy.deepcopy(self.tools[0])
        tool["inputSchema"]["properties"]["command"]["enum"] = ["x" * 2200]
        with self.assertRaisesRegex(ValueError, "2048"):
            bridge.describe_tool(tool)

    def test_images_native_error_and_metadata_survive(self):
        original = {"content": [{"type": "text", "text": "report"},
                                {"type": "image", "mimeType": "image/png", "data": "YQ=="}],
                    "isError": True, "_meta": {"example": "value"}, "structuredContent": {"image": "report"}}
        self.assertEqual(bridge.native_result(original).to_dict(), original)

    def test_upstream_business_error_is_reported_as_mcp_tool_error(self):
        result = bridge.native_result({"content": [{"type": "text", "text": '{"error":"Invalid stage"}'}]}).to_dict()
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"], {"error": "Invalid stage"})

    def test_cloud_export_checks_hash_and_uses_run_private_reference(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            # macOS /var is an OS alias; the fixture must use a canonical trusted root.
            folder=str(pathlib.Path(folder).resolve())
            jid,aid='a'*32,'b'*24
            root=pathlib.Path(folder)/'jobs'/jid;root.mkdir(parents=True)
            (root/'state.json').write_text(json.dumps({'artifacts_ready':True}))
            (root/'artifacts.zip').write_bytes(b'example-output')
            (root/'artifacts.json').write_text(json.dumps([{'artifact_id':aid,'path':'artifacts.zip','size':14,'sha256':hashlib.sha256(b'example-output').hexdigest()}]))
            from nexus_agent import NexusRunContext
            context=NexusRunContext(run_id='00000000-0000-0000-0000-000000000001',display_asset_url='https://cloud.example/api/v1/internal/agent-runs/run/assets/')
            context.output.upload_file=Mock(return_value={'file_id':'00000000-0000-0000-0000-000000000002','state':'ready'})
            with patch.object(bridge,'current_run',return_value=context):
                result=bridge.export_artifact({'job_id':jid,'artifact_id':aid},folder).to_dict()
                self.assertFalse(result.get('isError',False))
                self.assertTrue(result['structuredContent']['authentication_required'])
                self.assertIn('/api/v1/agent-runs/'+context.run_id+'/files/',result['structuredContent']['download_url'])
                context.output.upload_file.assert_called_once()
                (root/'artifacts.zip').write_bytes(b'tampered-file!')
                self.assertTrue(bridge.export_artifact({'job_id':jid,'artifact_id':aid},folder).to_dict()['isError'])
                context.output.upload_file.assert_called_once()


class ResultStoreTests(unittest.TestCase):
    def test_sse_binary_pages_are_bounded_without_changing_request(self):
        payload={'job_id':'a'*32,'artifact_id':'b'*24,'limit':262144}
        self.assertEqual(bridge.streaming_payload('read_eda_artifact',payload)['limit'],49152)
        self.assertEqual(payload['limit'],262144)
        self.assertEqual(bridge.streaming_payload('read_eda_artifact',{'limit':100})['limit'],100)
        self.assertEqual(bridge.streaming_payload('get_eda_job',payload),payload)

    def test_stream_starts_before_execution_and_calls_handler_once(self):
        handler = Mock(return_value=bridge.object_result({"output": "done"}))
        stream = bridge.stream_call(handler, {"command":"test"})
        self.assertEqual(next(stream).event, "progress")
        handler.assert_not_called()
        result = next(stream)
        self.assertEqual(result.event, "result")
        handler.assert_called_once_with({"command":"test"})
        self.assertEqual(list(stream), [])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = bridge.ResultStore(self.tmp.name)

    def collect(self, rid, kind):
        offset, parts = 0, []
        while True:
            result = self.store.read({"result_id": rid, "kind": kind, "offset": offset})
            self.assertLessEqual(bridge.result_bytes(result), bridge.RESPONSE_BUDGET)
            page = result.to_dict()["structuredContent"]
            parts.append(page["text"])
            if page["eof"]: break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        return "".join(parts)

    def test_large_unicode_control_output_roundtrips_without_loss(self):
        log = ('综合😀\n\t"\\\x01' * 5000)
        original = bridge.object_result({"output": log, "execution_time": 0.161, "session_id": "test", "error": None})
        summary = self.store.bound(original)
        self.assertLessEqual(bridge.result_bytes(summary), bridge.RESPONSE_BUDGET)
        value = summary.to_dict()["structuredContent"]
        self.assertTrue(value["truncated"])
        self.assertEqual(value["execution_time"], 0.161)
        self.assertEqual(self.collect(value["result_id"], "log"), log)
        self.assertEqual(json.loads(self.collect(value["result_id"], "result")), original.to_dict())

    def test_small_result_preserved_and_oversized_error_keeps_error_flag(self):
        small = bridge.object_result({"output": "ok"})
        self.assertIs(self.store.bound(small), small)
        large = bridge.object_result({"output": "x" * 23000, "error": "failed"}, True)
        self.assertTrue(self.store.bound(large).to_dict()["isError"])

    def test_image_result_can_be_recovered_from_saved_json(self):
        original = bridge.McpToolResult(content=[{"type": "image", "mimeType": "image/png", "data": "YWFh" * 6000}])
        rid = self.store.bound(original).to_dict()["structuredContent"]["result_id"]
        self.assertEqual(json.loads(self.collect(rid, "result")), original.to_dict())

    def test_invalid_ids_offsets_expiry_and_quota(self):
        for args in ({"result_id": "../escape"}, {"result_id": "a" * 32, "limit": 10000}):
            self.assertTrue(self.store.read(args).to_dict()["isError"])
        self.store.max_items = 1
        first = self.store.bound(bridge.object_result({"output": "中" * 9000})).to_dict()["structuredContent"]["result_id"]
        self.assertTrue(self.store.read({"result_id": first, "offset": 1}).to_dict()["isError"])
        second = self.store.bound(bridge.object_result({"output": "x" * 23000})).to_dict()["structuredContent"]["result_id"]
        self.assertEqual(self.store.read({"result_id": first}).to_dict()["structuredContent"]["error"], "RESULT_NOT_FOUND")
        path = pathlib.Path(self.tmp.name) / (second + ".json")
        os.utime(path, (0, 0))
        self.assertEqual(self.store.read({"result_id": second}).to_dict()["structuredContent"]["error"], "RESULT_NOT_FOUND")

    def test_new_tool_descriptor_fits_registration_limits(self):
        descriptor = bridge.describe_tool(bridge.RESULT_READER)
        self.assertIn("result_id", descriptor.input_schema["required"])


if __name__ == "__main__":
    unittest.main()
