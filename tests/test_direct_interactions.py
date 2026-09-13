import json
from pathlib import Path
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nexus_agent import NexusAgentClient, NexusAgentServer
from nexus_agent.auth import CloudTrustUnavailableError


def envelope(intent="demo.interactive"):
    return {
        "version": "1.0",
        "intent": intent,
        "intent_version": 1,
        "task_id": "caller-envelope-task",
        "source_agent": "agent://demo/caller",
        "tenant": "demo",
        "hop_limit": 8,
        "payload": {"value": 7},
    }


class DirectInteractionTest(unittest.TestCase):
    def setUp(self):
        self.server = NexusAgentServer(
            "127.0.0.1",
            0,
            path="/agent/v1/invoke",
            stream_path="/agent/v1/invoke-stream",
            direct_task_heartbeat_seconds=1,
        )

        @self.server.handler("demo.interactive")
        def interactive(request):
            context = request.run_context
            context.plan.set([
                {"id": "confirm", "title": "Confirm work", "status": "running"}
            ])
            context.shell.write("ready", stream="stdout")
            context.browser.frame(b"direct-browser-frame", content_type="image/png")
            reply = context.chat.ask(
                "Continue?",
                key="confirm",
                choices=(
                    {"value": "continue", "label": "Continue"},
                    {"value": "cancel", "label": "Cancel"},
                ),
                timeout=5,
            )
            context.plan.update("confirm", status="completed")
            return {"selection": reply.value}

        @self.server.handler("demo.async")
        async def asynchronous(request):
            await __import__("asyncio").sleep(0.02)
            request.run_context.chat.say("done")
            return {"ok": True}

        self.thread = self.server.serve_in_thread()
        self.client = NexusAgentClient(
            f"http://127.0.0.1:{self.server.port}",
            auth="none",
            timeout=10,
            use_environment_proxy=False,
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_invoke_interactive_waits_for_reply_and_keeps_events_private(self):
        seen = []
        for item in self.client.invoke_interactive(envelope()):
            seen.append(item)
            if item.input_required:
                item.reply("continue")
        result = [item for item in seen if item.event == "result"][-1]
        self.assertEqual(result.data["result"], {"selection": "continue"})
        display_events = [item.data["event"] for item in seen if item.event == "display"]
        self.assertTrue(any(event.get("type") == "ACTIVITY_SNAPSHOT" for event in display_events))
        self.assertTrue(any(event.get("name") == "nexus.computer.log" for event in display_events))
        frame = next(event for event in display_events if event.get("name") == "nexus.computer.frame")

        task = seen[0]._task
        self.assertIsNotNone(task)
        self.assertEqual(task.asset(frame["value"]["frame_id"]), b"direct-browser-frame")
        self.assertNotIn(task._token, repr(task))
        wrong = urllib.request.Request(
            f"http://127.0.0.1:{self.server.port}/agent/v1/tasks/{task.task_id}",
            headers={"X-Nexus-Task-Token": "wrong"},
        )
        with self.assertRaises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(wrong, timeout=2)
        self.assertEqual(captured.exception.code, 404)
        captured.exception.close()

    def test_respond_async_supports_async_handler_and_task_polling(self):
        task = self.client.invoke_async(envelope("demo.async"))
        self.assertNotIn(task._token, repr(task))
        self.assertEqual(task.result(timeout=3), {"ok": True})
        events = list(task.events())
        self.assertTrue(any(item.event == "display" for item in events))
        self.assertEqual(events[-1].event, "result")

    def test_task_cancel_is_scoped_to_one_token(self):
        waiting = threading.Event()

        @self.server.handler("demo.wait")
        def wait_handler(_request):
            waiting.set()
            time.sleep(1)
            return {"late": True}

        task = self.client.invoke_async(envelope("demo.wait"))
        self.assertTrue(waiting.wait(1))
        state = task.cancel()
        self.assertEqual(state["status"], "cancelled")
        with self.assertRaisesRegex(Exception, "cancelled"):
            task.result(timeout=1)


class RunContextExchangeTest(unittest.TestCase):
    def test_server_reports_missing_router_cloud_trust_explicitly(self):
        def unavailable(_request, *, timeout, exchange=False):
            del timeout, exchange
            raise CloudTrustUnavailableError(
                "router does not advertise Cloud TLS trust"
            )

        server = NexusAgentServer(
            "127.0.0.1", 0,
            path="/agent/v1/invoke",
            run_context_opener=unavailable,
        )
        server.handler("demo.exchange-trust")(lambda _request: {"ok": True})
        thread = server.serve_in_thread()
        try:
            payload = envelope("demo.exchange-trust")
            payload["nexus_run_context"] = {
                "exchange_url": "https://cloud.example.test/exchange",
                "exchange_token": "one-time-secret",
            }
            client = NexusAgentClient(
                f"http://127.0.0.1:{server.port}", auth="none"
            )
            with self.assertRaises(Exception) as failure:
                client.invoke(payload)
            self.assertEqual(
                getattr(failure.exception, "code", ""),
                "RUN_CONTEXT_TRUST_UNAVAILABLE",
            )
            self.assertNotIn("one-time-secret", str(failure.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_cloud_json_handler_flushes_final_events_before_result(self):
        context = mock.MagicMock()
        context.enabled = True
        context.run_id = "cloud-json-run"
        server = NexusAgentServer(
            "127.0.0.1", 0,
            path="/agent/v1/invoke",
            stream_path="/agent/v1/invoke-stream",
        )

        @server.handler("demo.cloud-json")
        def cloud_handler(request):
            self.assertIs(request.run_context, context)
            return {"ok": True}

        server_thread = server.serve_in_thread()
        try:
            payload = envelope("demo.cloud-json")
            payload["nexus_run_context"] = {
                "exchange_url": "https://context.invalid/exchange",
                "exchange_token": "one-time-secret",
            }
            client = NexusAgentClient(
                f"http://127.0.0.1:{server.port}",
                auth="none",
                use_environment_proxy=False,
            )
            with mock.patch(
                "nexus_agent.server.NexusRunContext.from_exchange",
                return_value=context,
            ):
                self.assertEqual(client.invoke(payload), {"ok": True})
            context.flush.assert_called_once_with()
            deadline = time.monotonic() + 1.0
            while context.close.call_count == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            context.close.assert_called_once_with()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_cloud_sync_handler_is_bridged_to_sse_with_heartbeat(self):
        events = []
        cloud_requests = []

        def cloud_opener(request, *, timeout, exchange=False):
            cloud_requests.append((request.full_url, exchange))
            parts = urllib.parse.urlsplit(request.full_url)
            mapped = urllib.request.Request(
                f"http://127.0.0.1:{cloud_server.server_port}{parts.path}",
                data=request.data,
                headers=dict(request.header_items()),
                method=request.get_method(),
            )
            return urllib.request.urlopen(mapped, timeout=timeout)

        class ExchangeHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                if self.path == "/events":
                    events.append(json.loads(body))
                    self.send_response(201)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = json.dumps({
                    "context": {
                        "X-Nexus-AGUI-Run-Id": "cloud-stream-run",
                        "X-Nexus-AGUI-Events-Url": "https://cloud.example.test/events",
                        "X-Nexus-AGUI-Token": "write-secret",
                        "X-Nexus-Interaction-Mode": "stream",
                    }
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *_args):
                return

        cloud_server = ThreadingHTTPServer(("127.0.0.1", 0), ExchangeHandler)
        exchange_thread = threading.Thread(target=cloud_server.serve_forever, daemon=True)
        exchange_thread.start()
        server = NexusAgentServer(
            "127.0.0.1", 0,
            path="/agent/v1/invoke",
            stream_path="/agent/v1/invoke-stream",
            direct_task_heartbeat_seconds=1,
            run_context_opener=cloud_opener,
        )

        @server.handler("demo.cloud-stream")
        def cloud_handler(request):
            self.assertEqual(request.run_context.run_id, "cloud-stream-run")
            time.sleep(1.1)
            request.run_context.chat.say("durable final message")
            return {"ok": True}

        server_thread = server.serve_in_thread()
        try:
            payload = envelope("demo.cloud-stream")
            payload["nexus_run_context"] = {
                "exchange_url": "https://cloud.example.test/exchange",
                "exchange_token": "exchange-secret",
            }
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.port}/agent/v1/invoke-stream",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read().decode()
            self.assertIn(": nexus-heartbeat", raw)
            self.assertIn("event: result", raw)
            self.assertIn('{"ok":true}', raw)
            self.assertEqual(
                [item.get("type") for item in events],
                ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"],
            )
            self.assertEqual(events[1]["delta"], "durable final message")
            self.assertTrue(any(exchange for _url, exchange in cloud_requests))
            self.assertTrue(any(not exchange for _url, exchange in cloud_requests))
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            cloud_server.shutdown()
            cloud_server.server_close()
            exchange_thread.join(timeout=2)

    def test_server_exchanges_once_and_removes_reference_from_envelope(self):
        exchanges = []

        class ExchangeHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                exchanges.append(self.headers.get("X-Nexus-Run-Context-Token"))
                if len(exchanges) > 1:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = json.dumps({
                    "data": {
                        "context": {
                            "X-Nexus-AGUI-Run-Id": "cloud-run-1",
                            "X-Nexus-AGUI-Events-Url": "http://127.0.0.1:1/events",
                            "X-Nexus-AGUI-Token": "cloud-write-secret",
                            "X-Nexus-Interaction-Mode": "stream",
                        }
                    },
                    "request_id": "exchange-e2e",
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *_args):
                return

        exchange = ThreadingHTTPServer(("127.0.0.1", 0), ExchangeHandler)
        exchange_thread = threading.Thread(target=exchange.serve_forever, daemon=True)
        exchange_thread.start()

        def cloud_opener(request, *, timeout, exchange=False):
            parts = urllib.parse.urlsplit(request.full_url)
            mapped = urllib.request.Request(
                f"http://127.0.0.1:{exchange_server.server_port}{parts.path}",
                data=request.data,
                headers=dict(request.header_items()),
                method=request.get_method(),
            )
            return urllib.request.urlopen(mapped, timeout=timeout)

        exchange_server = exchange
        server = NexusAgentServer(
            "127.0.0.1", 0, path="/agent/v1/invoke",
            run_context_opener=cloud_opener,
        )

        @server.handler("demo.exchange")
        def handler(request):
            self.assertTrue(request.run_context.enabled)
            self.assertEqual(request.run_context.run_id, "cloud-run-1")
            self.assertNotIn("nexus_run_context", request.raw)
            return {"ok": True}

        server_thread = server.serve_in_thread()
        try:
            payload = envelope("demo.exchange")
            payload["nexus_run_context"] = {
                "exchange_url": "https://cloud.example.test/exchange",
                "exchange_token": "one-time-secret",
            }
            client = NexusAgentClient(
                f"http://127.0.0.1:{server.port}",
                auth="none",
                use_environment_proxy=False,
            )
            self.assertEqual(client.invoke(payload), {"ok": True})
            with self.assertRaises(Exception) as second:
                client.invoke(payload)
            self.assertNotIn("one-time-secret", str(second.exception))
            close = getattr(second.exception, "close", None)
            if callable(close):
                close()
            self.assertEqual(exchanges, ["one-time-secret", "one-time-secret"])
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            exchange.shutdown()
            exchange.server_close()
            exchange_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
