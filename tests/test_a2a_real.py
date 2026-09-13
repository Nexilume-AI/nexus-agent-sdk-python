from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

try:
    from a2a.helpers import (
        get_message_text,
        new_task_from_user_message,
        new_text_message,
        new_text_part,
    )
    from a2a.server.agent_execution import AgentExecutor, RequestContext
    from a2a.server.events import EventQueue
    from a2a.server.tasks import TaskUpdater
    from a2a.types import (
        AgentCapabilities,
        AgentCard,
        AgentInterface,
        AgentSkill,
        Role,
        SendMessageRequest,
        TaskState,
    )
    from google.protobuf.json_format import MessageToDict
    A2A_AVAILABLE = True
except ImportError:
    AgentExecutor = object
    A2A_AVAILABLE = False

from nexus_agent import CapabilityRegistration, NexusAgentServer  # noqa: E402
from nexus_agent.a2a import (  # noqa: E402
    A2ABridgeError,
    A2AExecutorBridge,
    NexusA2AAgent,
    NexusA2AClient,
)


class FriendlyA2AEdgeHandler(BaseHTTPRequestHandler):
    requests = []
    resumable_executions = 0

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length))
        type(self).requests.append((self.path, request, dict(self.headers)))
        message = request["message"]
        if self.path.endswith("/message:stream"):
            context_id = message.get("contextId", "friendly-context")
            events = [
                {
                    "task": {
                        "id": "friendly-stream-task-1",
                        "contextId": context_id,
                        "status": {"state": "TASK_STATE_SUBMITTED"},
                    }
                },
                {
                    "statusUpdate": {
                        "taskId": "friendly-stream-task-1",
                        "contextId": context_id,
                        "status": {"state": "TASK_STATE_WORKING"},
                    }
                },
                {
                    "artifactUpdate": {
                        "taskId": "friendly-stream-task-1",
                        "contextId": context_id,
                        "artifact": {
                            "artifactId": "friendly-stream-artifact-1",
                            "parts": [{"text": "friendly-stream-chunk"}],
                        },
                        "lastChunk": True,
                    }
                },
                {
                    "statusUpdate": {
                        "taskId": "friendly-stream-task-1",
                        "contextId": context_id,
                        "status": {"state": "TASK_STATE_COMPLETED"},
                    }
                },
            ]
            parts = message.get("parts", [])
            requested_text = parts[0].get("text", "") if parts else ""
            resume_cursor = self.headers.get("Last-Event-ID")
            if requested_text == "resume-me":
                if resume_cursor is None:
                    type(self).resumable_executions += 1
                    events = events[:2]
                else:
                    events = events[int(resume_cursor):]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            for event in events:
                raw = ("data: " + json.dumps(event) + "\n\n").encode("utf-8")
                self.wfile.write(raw)
                self.wfile.flush()
            return
        result = {
            "message": {
                "messageId": "a2a:" + message["messageId"],
                "contextId": message.get("contextId", "friendly-context"),
                "role": "ROLE_AGENT",
                "parts": [{
                    "data": {
                        "id": "friendly-task-1",
                        "contextId": message.get(
                            "contextId", "friendly-context"
                        ),
                        "status": {"state": "TASK_STATE_COMPLETED"},
                        "artifacts": [{
                            "artifactId": "friendly-artifact-1",
                            "parts": [{"text": "friendly-called:hello"}],
                        }],
                    },
                    "mediaType": "application/json",
                }],
            }
        }
        raw = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/a2a+json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@unittest.skipUnless(A2A_AVAILABLE, "official a2a-sdk is not installed")
class RealA2ABridgeTest(unittest.TestCase):
    class EchoExecutor(AgentExecutor):
        def __init__(self):
            self.calls = []

        async def execute(
            self, context: RequestContext, event_queue: EventQueue
        ) -> None:
            text = get_message_text(context.message)
            self.calls.append({
                "text": text,
                "message_id": context.message.message_id,
                "state": dict(context.call_context.state),
            })
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
            updater = TaskUpdater(
                event_queue=event_queue,
                task_id=task.id,
                context_id=task.context_id,
            )
            await updater.update_status(TaskState.TASK_STATE_WORKING)
            await updater.add_artifact(
                parts=[new_text_part(f"official-a2a-called:{text}")],
                name="Nexus A2A echo result",
            )
            await updater.update_status(TaskState.TASK_STATE_COMPLETED)

        async def cancel(
            self, context: RequestContext, event_queue: EventQueue
        ) -> None:
            raise NotImplementedError

    def setUp(self):
        self.server = NexusAgentServer("127.0.0.1", 0)
        self.executor = self.EchoExecutor()
        self.card = AgentCard(
            name="Nexus A2A Echo",
            description="Official A2A executor behind Nexus",
            version="1.0.0",
            supported_interfaces=[AgentInterface(
                protocol_binding="HTTP+JSON",
                url="http://nexus.invalid/a2a/card/echo",
                protocol_version="1.0",
            )],
            capabilities=AgentCapabilities(streaming=True),
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
            skills=[AgentSkill(
                id="echo",
                name="Echo",
                description="Echo one A2A text message",
                tags=["a2a", "nexus"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            )],
        )
        self.capability = CapabilityRegistration(
            intent="demo.a2a.echo",
            origin="agent://demo/a2a-echo",
            endpoint=f"http://127.0.0.1:{self.server.port}/invoke",
            tenant="demo",
        )
        self.bridge = A2AExecutorBridge(
            self.executor,
            self.server,
            self.card,
            {"echo": self.capability},
        )
        self.bridge.start()
        self.thread = self.server.serve_in_thread()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.bridge.close()

    def _post(self, selector="echo", request=None, *, streaming=False):
        if request is None:
            request = SendMessageRequest(
                message=new_text_message(
                    "hello through Nexus",
                    role=Role.ROLE_USER,
                    context_id="ctx-a2a-1",
                )
            )
        envelope = {
            "version": "1.0",
            "intent": "demo.a2a.echo",
            "intent_version": 1,
            "task_id": "a2a:msg-a2a-1",
            "source_agent": "agent://demo/a2a-caller",
            "target_agent": "agent://demo/a2a-echo",
            "tenant": "demo",
            "hop_limit": 7,
            "constraints": {"region": "local"},
            "payload": {
                "protocol": "a2a",
                "authority": "card",
                "selector": selector,
                "request": MessageToDict(request),
            },
        }
        headers = {
            "Content-Type": "application/vnd.nexus.agent-envelope+json",
            "X-Nexus-Route-Id": "0123456789abcdef0123456789abcdef",
        }
        if streaming:
            headers["Accept"] = "text/event-stream"
        http_request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.port}/invoke",
            data=json.dumps(envelope).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        return urllib.request.urlopen(http_request, timeout=5)

    def test_bridge_health_requires_executor_loop_and_listener(self):
        self.assertTrue(self.bridge.is_healthy())
        self.bridge.close()
        self.assertFalse(self.bridge.is_healthy())

    def test_official_executor_is_called_and_returns_task(self):
        with self._post() as response:
            result = json.loads(response.read())
        self.assertEqual(result["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(
            result["artifacts"][0]["parts"][0]["text"],
            "official-a2a-called:hello through Nexus",
        )
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.executor.calls[0]["text"], "hello through Nexus")
        self.assertEqual(
            self.executor.calls[0]["state"]["nexus.target_agent"],
            "agent://demo/a2a-echo",
        )
        self.assertEqual(
            self.executor.calls[0]["state"]["nexus.skill"], "echo"
        )

    def test_skill_mismatch_is_rejected_before_executor(self):
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self._post(selector="other").read()
        self.assertEqual(captured.exception.code, 400)
        result = json.loads(captured.exception.read())
        captured.exception.close()
        self.assertEqual(result["code"], "A2A_SKILL_MISMATCH")
        self.assertEqual(self.executor.calls, [])

    def test_official_executor_streams_task_updates_and_artifact(self):
        with self._post(streaming=True) as response:
            self.assertEqual(response.headers.get_content_type(), "text/event-stream")
            raw = response.read().decode("utf-8")
        events = [
            json.loads(line[6:])
            for line in raw.splitlines()
            if line.startswith("data: ")
        ]
        self.assertGreaterEqual(len(events), 4)
        self.assertIn("task", events[0])
        self.assertTrue(any("artifactUpdate" in event for event in events))
        self.assertEqual(
            events[-1]["statusUpdate"]["status"]["state"],
            "TASK_STATE_COMPLETED",
        )

    def test_missing_card_skill_fails_start(self):
        self.bridge.close()
        bridge = A2AExecutorBridge(
            self.executor,
            self.server,
            self.card,
            {"missing": self.capability},
        )
        with self.assertRaisesRegex(A2ABridgeError, "missing from Agent Card"):
            bridge.start()
        bridge.close()

    def test_friendly_agent_builds_card_and_capability(self):
        server = NexusAgentServer("127.0.0.1", 0)
        agent = NexusA2AAgent(
            self.executor,
            router="http://127.0.0.1:7788",
            identity="agent://demo/friendly-echo",
            endpoint=f"http://127.0.0.1:{server.port}/invoke",
            tenant="demo",
            server=server,
            card_url="http://router.test/a2a/friendly-card/echo",
            name="Friendly Echo",
        )
        returned = agent.expose(
            skill="echo",
            intent="demo.friendly.echo",
            description="Friendly echo skill",
            public_ipv6="auto",
        )
        self.assertIs(returned, agent)
        card = agent.build_card()
        self.assertEqual(card.name, "Friendly Echo")
        self.assertEqual(card.skills[0].id, "echo")
        self.assertTrue(card.capabilities.streaming)
        self.assertEqual(
            card.supported_interfaces[0].url,
            "http://router.test/a2a/friendly-card/echo",
        )
        self.assertEqual(agent.capabilities[0].intent, "demo.friendly.echo")
        self.assertEqual(agent.capabilities[0].public_ipv6, "auto")
        self.assertEqual(
            agent.capabilities[0].origin, "agent://demo/friendly-echo"
        )
        with self.assertRaisesRegex(ValueError, "already exposed"):
            agent.expose(skill="echo", intent="demo.other")
        server.server_close()

    def test_friendly_client_sends_and_unwraps_task(self):
        FriendlyA2AEdgeHandler.requests.clear()
        edge = ThreadingHTTPServer(
            ("127.0.0.1", 0), FriendlyA2AEdgeHandler
        )
        thread = threading.Thread(target=edge.serve_forever, daemon=True)
        thread.start()

        async def call():
            async with NexusA2AClient(
                f"http://127.0.0.1:{edge.server_port}",
                card_id="friendly card",
                skill="echo",
                token="test-token",
            ) as client:
                self.assertEqual(
                    client.edge_url,
                    f"http://127.0.0.1:{edge.server_port}/a2a/"
                    "friendly%20card/echo",
                )
                return await client.send(
                    "hello", context_id="friendly-context"
                )

        try:
            result = asyncio.run(call())
        finally:
            edge.shutdown()
            edge.server_close()
            thread.join(timeout=2)
        self.assertEqual(result.text, "friendly-called:hello")
        self.assertEqual(
            result.task["status"]["state"], "TASK_STATE_COMPLETED"
        )
        self.assertEqual(len(FriendlyA2AEdgeHandler.requests), 1)
        path, request, headers = FriendlyA2AEdgeHandler.requests[0]
        self.assertEqual(
            path, "/a2a/friendly%20card/echo/message:send"
        )
        self.assertEqual(headers["Authorization"], "Bearer test-token")
        self.assertEqual(request["message"]["parts"][0]["text"], "hello")

    def test_friendly_client_streams_normalized_official_events(self):
        FriendlyA2AEdgeHandler.requests.clear()
        edge = ThreadingHTTPServer(
            ("127.0.0.1", 0), FriendlyA2AEdgeHandler
        )
        thread = threading.Thread(target=edge.serve_forever, daemon=True)
        thread.start()

        async def call():
            async with NexusA2AClient(
                f"http://127.0.0.1:{edge.server_port}",
                card_id="friendly-card",
                skill="echo",
                transaction_token="one-task-token",
            ) as client:
                return [
                    event
                    async for event in client.stream(
                        "hello streaming", context_id="friendly-stream-context"
                    )
                ]

        try:
            events = asyncio.run(call())
        finally:
            edge.shutdown()
            edge.server_close()
            thread.join(timeout=2)
        self.assertEqual(
            [event.kind for event in events],
            ["task", "status", "artifact", "status"],
        )
        self.assertEqual(events[1].state, "TASK_STATE_WORKING")
        self.assertEqual(events[2].text, "friendly-stream-chunk")
        self.assertTrue(events[2].last_chunk)
        self.assertFalse(events[2].final)
        self.assertEqual(events[-1].state, "TASK_STATE_COMPLETED")
        self.assertTrue(events[-1].final)
        path, request, headers = FriendlyA2AEdgeHandler.requests[0]
        self.assertEqual(path, "/a2a/friendly-card/echo/message:stream")
        self.assertEqual(headers["Txn-Token"], "one-task-token")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(
            request["message"]["parts"][0]["text"], "hello streaming"
        )

    def test_friendly_client_resumes_same_official_a2a_request(self):
        FriendlyA2AEdgeHandler.requests.clear()
        FriendlyA2AEdgeHandler.resumable_executions = 0
        edge = ThreadingHTTPServer(
            ("127.0.0.1", 0), FriendlyA2AEdgeHandler
        )
        thread = threading.Thread(target=edge.serve_forever, daemon=True)
        thread.start()

        async def call():
            async with NexusA2AClient(
                f"http://127.0.0.1:{edge.server_port}",
                card_id="friendly-card",
                skill="echo",
                token="resume-jwt",
            ) as client:
                return [
                    event async for event in client.stream(
                        "resume-me", reconnect_delay=0
                    )
                ]

        try:
            events = asyncio.run(call())
        finally:
            edge.shutdown()
            edge.server_close()
            thread.join(timeout=2)
        self.assertEqual(
            [event.event_id for event in events], ["1", "2", "3", "4"]
        )
        self.assertTrue(events[-1].final)
        self.assertEqual(FriendlyA2AEdgeHandler.resumable_executions, 1)
        requests = [item for item in FriendlyA2AEdgeHandler.requests
                    if item[0].endswith("/message:stream")]
        self.assertEqual(len(requests), 2)
        headers = {name.lower(): value for name, value in requests[1][2].items()}
        self.assertEqual(headers["last-event-id"], "2")

    def test_friendly_client_rejects_two_token_types(self):
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            NexusA2AClient(
                "http://127.0.0.1:7790",
                card_id="card",
                skill="echo",
                token="access",
                transaction_token="transaction",
            )


if __name__ == "__main__":
    unittest.main()
