import io
import json
import unittest
from unittest.mock import Mock

from nexus_agent.training import TrainingClient, TrainingTrajectory, _NoRedirect


class TrainingTests(unittest.TestCase):
    def test_tool_pairs_masks_and_terminal_status(self):
        episode = TrainingTrajectory(task_id="task-1", session_id="chat-1")
        episode.message("user", "Calculate")
        episode.tool_call("call-1", "add", {"a": 2, "b": 3})
        episode.tool_result("call-1", {"answer": 5})
        episode.message("assistant", "5", trainable=False)
        data = episode.payload("failed")
        self.assertEqual(data["messages"][1]["tool_calls"][0]["id"], data["messages"][2]["tool_call_id"])
        self.assertEqual(data["messages"][-1]["weight"], 0)
        with self.assertRaises(ValueError): episode.payload("running")

    def test_upload_requires_consent_and_retains_idempotency_key(self):
        client = TrainingClient("https://cloud.example", token="test-token", tenant_id="tenant", dataset_id="10000000-0000-0000-0000-000000000001")
        episode = TrainingTrajectory(task_id="task-1")
        episode.message("user", "Hi"); episode.message("assistant", "Hello")
        response = Mock()
        response.__enter__ = Mock(return_value=io.BytesIO(b'{"ok":true,"data":{"id":"sample"}}'))
        response.__exit__ = Mock(return_value=False)
        client._opener = Mock(); client._opener.open.return_value = response
        with self.assertRaises(ValueError): client.submit(episode, request_key="attempt-1", rights_confirmed=False)
        client._opener.open.assert_not_called()
        self.assertEqual(client.submit(episode, request_key="attempt-1", rights_confirmed=True), {"id": "sample"})
        req = client._opener.open.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(body["request_key"], "attempt-1")
        self.assertNotIn("test-token", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Bearer test-token")

    def test_credentials_cannot_be_redirected_or_sent_over_plain_http(self):
        with self.assertRaises(ValueError): TrainingClient("http://remote.example", token="x", tenant_id="t", dataset_id="10000000-0000-0000-0000-000000000001")
        with self.assertRaises(RuntimeError): _NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")


if __name__ == "__main__": unittest.main()
