import asyncio
import json
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from nexus_agent.inbox import AsyncRunInbox, NexusFollowUpUnavailable, RunInbox
from nexus_agent.reporting import NexusRunContext


class RunInboxTests(unittest.TestCase):
    def test_attachment_receive_requires_explicit_opt_in_and_async_preserves_references(self):
        ctx = self.context()
        ctx.inbox.mode = "steer_and_queue"
        row = {"id": "image-message", "turn_index": 2, "content": "Read this", "status": "received",
               "attachments": [{"asset_id": "image-ref", "content_type": "image/png"}],
               "files": [{"file_id": "file-ref", "name": "notes.txt"}]}
        ctx._interaction_request_url = Mock(return_value={"items": [row]})
        with self.assertRaises(NexusFollowUpUnavailable):
            ctx.inbox._poll()
        self.assertEqual(ctx.inbox.receive_pending(), [])
        ctx.inbox.attachments_enabled = True
        ctx.inbox._poll()
        item = ctx.inbox.receive_pending()[0]
        self.assertEqual(item.attachments, tuple(row["attachments"]))
        async def scenario():
            message = (await ctx.aio.inbox.receive_pending())[0]
            self.assertEqual(message.files, tuple(row["files"]))
            await message.acknowledge()
        asyncio.run(scenario())
        self.assertEqual(ctx.inbox.receive_pending(), [])

    def test_attachment_negotiation_fails_closed_on_old_cloud(self):
        ctx = self.context()
        ctx._interaction_request_url = Mock(return_value={"mode": "queue", "protocol": 1})
        with self.assertRaisesRegex(NexusFollowUpUnavailable, "upgrade Cloud"):
            ctx.inbox.configure("queue", attachments=True)
        self.assertFalse(ctx.inbox.attachments_enabled)
        ctx._interaction_request_url.return_value["attachment_protocol"] = 1
        ctx.inbox.configure("queue", attachments=True)
        self.assertTrue(ctx.inbox.attachments_enabled)
        self.assertEqual(ctx._interaction_request_url.call_args.kwargs["payload"]["attachment_protocol"], 1)

    def context(self):
        ctx = NexusRunContext(run_id="run-a", checkpoint_url="https://cloud.example/api/v1/internal/agent-runs/run-a/checkpoint/",
            interaction_token="secret-not-in-logs", turn_index=2)
        ctx.control = Mock(return_value={"follow_up_protocol": 1})
        ctx.raise_if_cancelled = Mock()
        return ctx

    def test_old_cloud_and_invalid_mode_fail_explicitly(self):
        ctx = self.context()
        ctx.control.return_value = {}
        with self.assertRaises(NexusFollowUpUnavailable):
            ctx.inbox.configure()
        with self.assertRaises(ValueError):
            ctx.inbox.configure("parallel")

    def test_receive_is_bounded_deduplicated_and_scoped_until_ack(self):
        ctx = self.context()
        ctx.inbox.mode = "steer_and_queue"
        ctx._interaction_request_url = Mock(return_value={"items": [
            {"id": "one", "turn_index": 2, "content": "保留文件", "status": "received"},
            {"id": "old", "turn_index": 1, "content": "old turn", "status": "received"},
        ]})
        ctx.inbox._poll()
        ctx.inbox._poll()
        items = ctx.inbox.receive_pending()
        self.assertEqual([item.content for item in items], ["保留文件"])
        self.assertNotIn("secret-not-in-logs", repr(items))
        items[0].acknowledge()
        ctx.inbox._poll()
        self.assertEqual(ctx.inbox.receive_pending(), [])
        self.assertEqual(ctx._interaction_request_url.call_args.args[0],
            "https://cloud.example/api/v1/internal/agent-runs/run-a/inbox/")

    def test_async_receive_and_ack_preserve_event_loop(self):
        ctx = self.context()
        ctx.inbox.mode = "steer_and_queue"
        ctx._interaction_request_url = Mock(return_value={"items": [{"id": "one", "turn_index": 2, "content": "next", "status": "received"}]})
        ctx.inbox._poll()
        async def scenario():
            updates = await ctx.aio.inbox.receive_pending()
            self.assertEqual(updates[0].id, "one")
            await updates[0].reject()
            self.assertEqual(await ctx.aio.inbox.receive_pending(), [])
        asyncio.run(scenario())
        self.assertEqual(ctx._interaction_request_url.call_args.kwargs["payload"]["status"], "rejected")

    def test_receiver_runs_in_background_and_closes_without_running_business_work(self):
        ctx = self.context()
        received = threading.Event()
        def request(url, **kwargs):
            data = kwargs["payload"]
            if data["action"] == "configure":
                return {"mode": "steer_and_queue", "protocol": 1}
            received.set()
            return {"items": []}
        ctx._interaction_request_url = Mock(side_effect=request)
        ctx.inbox.configure()
        self.assertTrue(received.wait(2))
        ctx.inbox.close()
        self.assertFalse(ctx.inbox._thread.is_alive())

    def test_failed_ack_retains_message_and_tls_uses_context_transport(self):
        ctx = self.context()
        ctx.inbox.mode = "steer_and_queue"
        ctx._interaction_request_url = Mock(return_value={"items": [{"id": "one", "turn_index": 2, "content": "next", "status": "received"}]})
        ctx.inbox._poll()
        ctx._interaction_request_url.side_effect = NexusFollowUpUnavailable("offline")
        with self.assertRaises(NexusFollowUpUnavailable):
            ctx.inbox.receive_pending()[0].acknowledge()
        self.assertEqual(len(ctx.inbox.receive_pending()), 1)

    def test_request_uses_existing_cloud_tls_opener_and_turn_token(self):
        ctx = self.context()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"data":{"items":[]}}'
        with patch.object(ctx, "_open_cloud", return_value=response) as opener:
            ctx.inbox._request("receive")
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("X-nexus-interaction-token"), "secret-not-in-logs")
        self.assertEqual(json.loads(request.data)["turn_index"], 2)

    def test_async_capability_configures_inbox_before_handler_and_closes_it(self):
        from nexus_agent import NexusAgent
        agent = NexusAgent(router="http://127.0.0.1:7446", tenant="test", agent_id="inbox",
            token="test-token", listen_host="127.0.0.1", port=0, advertise_address="192.0.2.20", cloud_publish=False)
        ctx = self.context()
        order = []
        ctx.inbox.configure = Mock(side_effect=lambda mode: order.append(mode))
        ctx.inbox.close = Mock(side_effect=lambda: order.append("closed"))
        @agent.capability("demo.inbox", pass_envelope=True, follow_up="steer_and_queue")
        async def handler(envelope, ctx: NexusRunContext):
            order.append("handler")
            return "done"
        try:
            result = asyncio.run(agent.server._handlers["demo.inbox"](SimpleNamespace(run_context=ctx)))
            self.assertEqual(result, "done")
            self.assertEqual(order, ["steer_and_queue", "handler", "closed"])
        finally:
            agent.server.server_close()
