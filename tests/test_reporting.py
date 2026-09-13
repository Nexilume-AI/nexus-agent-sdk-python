import asyncio
import json
import pathlib
import sys
import threading
import types
from enum import Enum
import unittest
from decimal import Decimal
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent import (  # noqa: E402
    MemoryDeleteResult,
    NexusAgent,
    NexusBrowserActionFailed,
    NexusBrowserStaleObservation,
    NexusBrowserUnavailable,
    NexusChatUnavailable,
    NexusComputerError,
    NexusMemoryConflict,
    NexusMemoryUnavailable,
    NexusMobileActionFailed,
    NexusMobilePermissionRequired,
    NexusMobileUnavailable,
    NexusReportingConfig,
    NexusRunContext,
    SSHWorkspaceConnectionCreate,
    SSHWorkspaceConnectionUpdate,
    current_run,
)
from nexus_agent.models import AgentEnvelope  # noqa: E402
from nexus_agent.fastmcp import CurrentNexusRun  # noqa: E402
import nexus_agent.reporting as reporting  # noqa: E402


class EventHandler(BaseHTTPRequestHandler):
    events = []
    workspace_requests = []
    connections = {}
    memory_item = {}
    interactions = {}
    checkpoint = {}
    mobile_requests = []
    terminal_requests = []
    run_messages = []
    browser_requests = []
    browser_revision = 0
    browser_error = None

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        if self.path == "/browser/":
            type(self).browser_requests.append((body, dict(self.headers)))
            if type(self).browser_error is not None:
                code, message = type(self).browser_error
                return self._json(400, {"error": {"code": code, "message": message}})
            if body.get("operation") == "close":
                return self._json(200, {"closed": True})
            type(self).browser_revision += 1
            revision = type(self).browser_revision
            import base64
            return self._json(200, {
                "observation_id": f"attached-{revision}",
                "revision": revision,
                "url": body.get("url") or "https://example.test/next",
                "title": "Attached Computer",
                "viewport": body.get("viewport") or [1280, 720],
                "image_base64": base64.b64encode(b"jpeg-frame").decode("ascii"),
                "content_type": "image/jpeg",
                "dom": {
                    "nodes": [{
                        "ref": "e1",
                        "tag": "button",
                        "role": "button",
                        "name": "Continue",
                        "text": "Continue",
                        "selector": "[data-nexus-browser-ref=\"e1\"]",
                        "bounds": [20, 30, 100, 40],
                    }],
                    "truncated": False,
                },
                "frame_published": True,
                "computer_name": "Caller Computer",
            })
        if self.path == "/mobile/":
            type(self).mobile_requests.append((self.command, self.path, body, dict(self.headers)))
            command_id = f"mobile-{len(type(self).mobile_requests)}"
            result = {"ok": True}
            if body.get("action") == "observe":
                result = {"packageName": "com.example", "nodes": [{"text": "Continue"}]}
            elif body.get("action") == "capture_screen":
                import base64
                result = {
                    "screenshot_base64": base64.b64encode(b"webp-image").decode("ascii"),
                    "content_type": "image/webp",
                    "width": 1080,
                    "height": 1920,
                }
            return self._json(201, {
                "id": command_id,
                "action": body["action"],
                "status": "succeeded",
                "result": result,
            })
        if self.path == "/interactions/":
            interaction_id = "interaction-1"
            type(self).interactions[interaction_id] = {
                "id": interaction_id,
                "key": body["key"],
                "status": "answered",
                "response": {"value": "continue", "text": "Continue"},
                "answered_at": "2026-08-14T00:00:00Z",
            }
            return self._json(201, type(self).interactions[interaction_id])
        if self.path == "/display-assets/":
            return self._json(201, {
                "id": "asset-1",
                "url": "/api/v1/agent-runs/run-1/display-assets/asset-1/",
                "content_type": body["content_type"],
                "size_bytes": len(body["content_base64"]),
            })
        if self.path.startswith("/manage/"):
            type(self).workspace_requests.append((self.command, self.path, body, dict(self.headers)))
            if self.path == "/manage/connections/":
                connection_id = str(uuid.uuid4())
                value = {
                    "id": connection_id,
                    "name": body["name"],
                    "ssh_host": body["ssh_host"],
                    "ssh_port": body.get("ssh_port", 22),
                    "ssh_user": body["ssh_user"],
                    "auth_mode": body.get("auth_mode", "private_key"),
                    "workspace_root": body.get("workspace_root", "~/.nexus"),
                    "status": "active",
                    "metadata": body.get("metadata", {}),
                }
                type(self).connections[connection_id] = value
                return self._json(201, value)
            if self.path.endswith("/test/") or self.path == "/manage/connections/validate/":
                return self._json(200, {"status": "succeeded", "facts": {"os": "linux"}, "checks": []})
            if self.path == "/manage/bindings/":
                return self._json(200, {
                    "id": "binding-1",
                    "connection_id": body["connection_id"],
                    "is_default": body.get("make_default", True),
                    "computer_enabled": True,
                    "workspace_root": "~/.nexus/agents/a/workspace",
                    "output_root": "~/.nexus/agents/a/runs/run-1/outputs",
                    "workspace_path": "/workspace",
                    "terminal_path": "/terminal",
                })
            return self._json(404, {})
        if self.path.startswith("/terminal"):
            type(self).terminal_requests.append((self.command, self.path, body, dict(self.headers)))
            return self._json(200, {
                "command_id": "command-1",
                "exit_code": 0,
                "stdout": "hello\n",
                "stderr": "",
                "duration_ms": 1,
                "displayed": body.get("display", True),
            })
        if self.path.startswith("/workspace"):
            return self._json(201, {"path": "notes.txt", "size_bytes": len(body.get("content", ""))})
        type(self).events.append((body, dict(self.headers)))
        return self._json(201, {})

    def do_GET(self):
        if self.path == "/mobile/":
            type(self).mobile_requests.append((self.command, self.path, None, dict(self.headers)))
            body = {
                "enabled": True,
                "available": True,
                "platform": "android",
                "status": "online",
                "capabilities": ["mobile.observe", "mobile.screen.capture", "mobile.tap", "mobile.swipe"],
            }
        elif self.path.startswith("/interactions/"):
            interaction_id = self.path.rstrip("/").rsplit("/", 1)[-1]
            body = type(self).interactions.get(interaction_id, {})
        elif self.path.startswith("/context/"):
            body = {
                "run": {"id": "run-1", "turn_index": 2, "tool_name": "assistant", "status": "running"},
                "project": {"id": "project-1", "name": "Research", "instructions": "Use verified sources.", "revision": 3},
                "messages": list(type(self).run_messages),
                "resources": {"computer_attached": True, "mobile_attached": False, "input_files": []},
            }
        elif self.path == "/checkpoint/":
            body = dict(type(self).checkpoint)
        elif self.path == "/manage/connections/":
            type(self).workspace_requests.append((self.command, self.path, None, dict(self.headers)))
            body = {"items": list(type(self).connections.values())}
        elif self.path.startswith("/manage/connections/"):
            connection_id = self.path.rstrip("/").rsplit("/", 1)[-1]
            body = type(self).connections.get(connection_id, {})
        elif self.path.startswith("/manage/bindings/"):
            body = {"items": []}
        elif self.path.startswith("/memory"):
            body = {"items": [dict(type(self).memory_item)] if type(self).memory_item else []}
        elif self.path.startswith("/terminal"):
            body = {"enabled": True, "status": "active", "viewer_mode": "read_only"}
        elif self.path.startswith("/workspace"):
            body = {
                "items": [{
                    "name": "input.txt",
                    "path": "input.txt",
                    "relative_path": "input.txt",
                    "type": "file",
                    "size_bytes": 5,
                    "modified_at": 1,
                }]
            }
        else:
            body = {"items": []}
        return self._json(200, body)

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        if self.path == "/checkpoint/":
            revision = int(type(self).checkpoint.get("revision") or 0) + 1
            type(self).checkpoint = {
                "stage": body["stage"],
                "data": body.get("data", {}),
                "revision": revision,
                "updated_at": "2026-08-14T00:00:00Z",
            }
            return self._json(200, type(self).checkpoint)
        return self._json(404, {})

    def do_PATCH(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).workspace_requests.append((self.command, self.path, body, dict(self.headers)))
        if self.path.startswith("/memory/"):
            if not type(self).memory_item or self.path.rstrip("/").rsplit("/", 1)[-1] != type(self).memory_item["id"]:
                return self._json(404, {"ok": False, "error": {"code": "NOT_FOUND"}})
            if body.get("expected_revision") != type(self).memory_item["revision"]:
                return self._json(409, {
                    "ok": False,
                    "error": {
                        "code": "MEMORY_REVISION_CONFLICT",
                        "current_revision": type(self).memory_item["revision"],
                    },
                })
            field_names = {
                "content_text": "text",
                "content_json": "content",
                "memory_type": "kind",
                "confidence": "confidence",
                "sensitivity_level": "sensitivity",
                "consent_status": "consent",
                "license_status": "license",
            }
            for source, target in field_names.items():
                if source in body:
                    type(self).memory_item[target] = body[source]
            type(self).memory_item["revision"] += 1
            return self._json(200, type(self).memory_item)
        connection_id = self.path.rstrip("/").rsplit("/", 1)[-1]
        type(self).connections[connection_id].update({
            key: value for key, value in body.items() if key not in {"private_key", "password"}
        })
        return self._json(200, type(self).connections[connection_id])

    def do_DELETE(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        type(self).workspace_requests.append((self.command, self.path, body, dict(self.headers)))
        if self.path.startswith("/memory/"):
            if not type(self).memory_item or self.path.rstrip("/").rsplit("/", 1)[-1] != type(self).memory_item["id"]:
                return self._json(404, {"ok": False, "error": {"code": "NOT_FOUND"}})
            if body.get("expected_revision") != type(self).memory_item["revision"]:
                return self._json(409, {
                    "ok": False,
                    "error": {
                        "code": "MEMORY_REVISION_CONFLICT",
                        "current_revision": type(self).memory_item["revision"],
                    },
                })
            result = {
                "id": type(self).memory_item["id"],
                "deleted": True,
                "revision": type(self).memory_item["revision"] + 1,
            }
            type(self).memory_item = {}
            return self._json(200, result)
        connection_id = self.path.rstrip("/").rsplit("/", 1)[-1]
        type(self).connections.pop(connection_id, None)
        self.send_response(204)
        self.end_headers()

    def _json(self, status, body):
        if status < 400 and self.path.startswith(("/manage/", "/workspace", "/terminal", "/mobile")):
            body = {"ok": True, "data": body, "error": None, "request_id": "sdk-test"}
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ReportingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), EventHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.events_url = f"http://127.0.0.1:{cls.server.server_port}/events"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        EventHandler.events.clear()
        EventHandler.workspace_requests.clear()
        EventHandler.connections.clear()
        EventHandler.memory_item = {
            "id": "memory-1",
            "kind": "preference",
            "text": "remember",
            "content": {"format": "markdown"},
            "scope": "caller",
            "confidence": "0.9000",
            "sensitivity": "internal",
            "consent": "approved",
            "license": "internal",
            "revision": 1,
            "created_at": "2026-08-13T00:00:00Z",
            "updated_at": "2026-08-13T00:00:00Z",
        }
        EventHandler.interactions.clear()
        EventHandler.mobile_requests.clear()
        EventHandler.terminal_requests.clear()
        EventHandler.run_messages = [
            {
                "id": "message-1",
                "sequence": 1,
                "role": "user",
                "content": "Previous question",
                "content_blocks": [{"type": "markdown", "text": "Previous question"}],
                "created_at": "2026-08-18T00:00:00Z",
            }
        ]
        EventHandler.browser_requests.clear()
        EventHandler.browser_revision = 0
        EventHandler.browser_error = None
        EventHandler.checkpoint = {}

    def context(self):
        return NexusRunContext(
            run_id="run-1",
            events_url=self.events_url,
            token="secret-write-token",
            config=NexusReportingConfig(retry_delay=0),
        )

    def test_trace_memory_and_output_are_fifo_agui_events(self):
        context = self.context()
        with context.trace.step("research"):
            with context.trace.tool("search", arguments={"query": "chips"}) as call:
                call.result({"hits": 2})
        context.memory.add("Reusable fact", consent="approved")
        context.output.created(
            "outputs/report.md",
            content_type="text/markdown",
            producer_step="compose",
        )

        report = context.close()
        self.assertEqual(report.sent, 8)
        self.assertEqual(report.failed, 0)
        self.assertEqual(
            [item[0]["type"] for item in EventHandler.events],
            [
                "STEP_STARTED",
                "TOOL_CALL_START",
                "TOOL_CALL_ARGS",
                "TOOL_CALL_RESULT",
                "TOOL_CALL_END",
                "STEP_FINISHED",
                "CUSTOM",
                "CUSTOM",
            ],
        )
        memory = EventHandler.events[-2][0]
        output = EventHandler.events[-1][0]
        self.assertEqual(memory["name"], "nexus.memory.item")
        self.assertEqual(output["value"]["workspace_path"], "outputs/report.md")
        self.assertTrue(
            all(
                headers.get("X-Nexus-Agui-Event-Id")
                for _, headers in EventHandler.events
            )
        )
        self.assertNotIn("secret-write-token", repr(context))
        self.assertNotIn("secret-write-token", repr(report))

    def test_plan_shell_browser_chat_and_checkpoint_helpers(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        context = NexusRunContext(
            run_id="run-1",
            events_url=self.events_url,
            token="event-token",
            interaction_url=base + "/interactions/",
            checkpoint_url=base + "/checkpoint/",
            display_asset_url=base + "/display-assets/",
            interaction_token="interactive-token",
            interaction_mode="stream",
            config=NexusReportingConfig(retry_delay=0),
        )
        context.plan.set([
            {"id": "inspect", "title": "Inspect workspace", "status": "running"},
            {"id": "confirm", "title": "Confirm operation", "status": "pending"},
        ])
        context.plan.update("inspect", status="completed")
        context.display.title("Workspace inspection complete")
        context.shell.write("$ python inspect.py", stream="command")
        context.browser.frame(
            b"\x89PNG\r\n\x1a\nframe",
            content_type="image/png",
            title="Workspace",
            url="https://user:password@example.test/path?token=private&view=main#secret",
            observation_id="observation-4",
            revision=4,
            action="fill",
            action_status="succeeded",
            dom_node_count=12,
        )
        reply = context.chat.ask(
            "Continue?",
            key="confirm-save",
            choices=[{"value": "continue", "label": "Continue"}],
            timeout=2,
        )
        checkpoint = context.checkpoint.save(stage="confirmed", data={"selection": reply.value})
        context.chat.say("Saved")
        report = context.close()

        self.assertEqual(reply.value, "continue")
        self.assertEqual(checkpoint.revision, 1)
        self.assertEqual(report.failed, 0)
        names = [event.get("name") for event, _headers in EventHandler.events]
        types = [event["type"] for event, _headers in EventHandler.events]
        self.assertIn("ACTIVITY_SNAPSHOT", types)
        self.assertIn("ACTIVITY_DELTA", types)
        self.assertIn("nexus.computer.log", names)
        self.assertIn("nexus.computer.frame", names)
        self.assertIn("nexus.display.title", names)
        frame = next(
            event["value"]
            for event, _headers in EventHandler.events
            if event.get("name") == "nexus.computer.frame"
        )
        self.assertEqual(frame["observation_id"], "observation-4")
        self.assertEqual(frame["revision"], 4)
        self.assertEqual(frame["action"], "fill")
        self.assertEqual(frame["dom_node_count"], 12)
        self.assertNotIn("user:password", frame["url"])
        self.assertNotIn("private", frame["url"])
        self.assertNotIn("#secret", frame["url"])
        self.assertIn("view=main", frame["url"])
        self.assertGreaterEqual(types.count("TEXT_MESSAGE_CONTENT"), 2)
        self.assertNotIn("interactive-token", repr(context))

    def test_attached_browser_uses_delegate_without_local_chrome_fallback(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        context = NexusRunContext(
            run_id="run-attached",
            events_url=self.events_url,
            token="event-token",
            browser_enabled=True,
            browser_delegate_url=base + "/browser/",
            browser_delegate_token="browser-token",
            browser_computer_name="Caller Computer",
            workspace_capabilities=("browser.control",),
            config=NexusReportingConfig(retry_delay=0),
        )
        with mock.patch("nexus_agent.browser._browser_worker") as local_worker:
            browser = context.browser.attached_session(viewport=(900, 640))
            first = browser.open("https://example.test/start")
            second = browser.element("e1").click()
            browser.close()

        local_worker.assert_not_called()
        self.assertEqual(first.revision, 1)
        self.assertEqual(first.image, b"jpeg-frame")
        self.assertEqual(first.dom.by_ref("e1").name, "Continue")
        self.assertEqual(second.revision, 2)
        self.assertEqual(
            [request[0]["operation"] for request in EventHandler.browser_requests],
            ["open", "action", "close"],
        )
        self.assertTrue(all(
            headers.get("X-Nexus-Browser-Delegate-Token") == "browser-token"
            for _body, headers in EventHandler.browser_requests
        ))

    def test_attached_browser_preserves_recoverable_browser_failure_types(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        context = NexusRunContext(
            run_id="run-attached-errors",
            events_url=self.events_url,
            token="event-token",
            browser_enabled=True,
            browser_delegate_url=base + "/browser/",
            browser_delegate_token="browser-token",
            workspace_capabilities=("browser.control",),
            config=NexusReportingConfig(retry_delay=0),
        )
        browser = context.browser.attached_session()

        EventHandler.browser_error = (
            "BROWSER_STALE_OBSERVATION",
            "Browser observation is stale; observe the page again",
        )
        with self.assertRaises(NexusBrowserStaleObservation):
            browser.observe()

        EventHandler.browser_error = (
            "BROWSER_UNAVAILABLE",
            "Browser automation is unavailable on this Computer",
        )
        with self.assertRaises(NexusBrowserUnavailable):
            browser.observe()

    def test_attached_browser_action_failure_keeps_safe_server_detail(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        context = NexusRunContext(
            run_id="run-attached-action-error",
            events_url=self.events_url,
            token="event-token",
            browser_enabled=True,
            browser_delegate_url=base + "/browser/",
            browser_delegate_token="browser-token",
            workspace_capabilities=("browser.control",),
            config=NexusReportingConfig(retry_delay=0),
        )
        EventHandler.browser_error = (
            "BROWSER_ACTION_FAILED",
            "Browser coordinates are outside the viewport",
        )

        with self.assertRaises(NexusBrowserActionFailed) as raised:
            context.browser.attached_session().observe()

        self.assertEqual(str(raised.exception), "Browser coordinates are outside the viewport")

    def test_async_attached_browser_uses_the_same_delegate(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        context = NexusRunContext(
            run_id="run-attached-async",
            events_url=self.events_url,
            token="event-token",
            browser_enabled=True,
            browser_delegate_url=base + "/browser/",
            browser_delegate_token="browser-token",
            browser_computer_name="Caller Computer",
            workspace_capabilities=("browser.control",),
            config=NexusReportingConfig(retry_delay=0),
        )

        async def exercise():
            browser = context.aio.browser.attached_session(viewport=(800, 600))
            opened = await browser.open("https://example.test/async")
            observed = await browser.observe()
            await browser.close()
            return opened, observed

        with mock.patch("nexus_agent.browser._browser_worker") as local_worker:
            opened, observed = asyncio.run(exercise())

        local_worker.assert_not_called()
        self.assertEqual(opened.revision, 1)
        self.assertEqual(observed.revision, 2)
        self.assertEqual(
            [request[0]["operation"] for request in EventHandler.browser_requests],
            ["open", "observe", "close"],
        )

    def test_managed_attached_browser_reuses_the_operation_idempotency_key(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        context = NexusRunContext(
            run_id="run-attached-recovery",
            events_url=self.events_url,
            token="event-token",
            interaction_token="interaction-token",
            recovery_url=base + "/operations/",
            recovery_managed=True,
            browser_enabled=True,
            browser_delegate_url=base + "/browser/",
            browser_delegate_token="browser-token",
            workspace_capabilities=("browser.control",),
            config=NexusReportingConfig(retry_delay=0),
        )
        prepared = {
            "id": "d12721b2-e38e-4ff6-9997-d7a493a9885a",
            "status": "running",
            "execute": True,
            "replayed": False,
            "result": None,
            "idempotency_key": "nxo_browser_action",
        }
        with mock.patch.object(
            context,
            "_interaction_request_url",
            side_effect=[prepared, {"status": "succeeded"}],
        ):
            observation = context.browser.attached_session().open("https://example.test/recovery")

        self.assertGreater(observation.revision, 0)
        request_body = EventHandler.browser_requests[-1][0]
        self.assertEqual(request_body["idempotency_key"], "nxo_browser_action")

    def test_display_title_is_run_scoped_and_chat_has_no_cross_run_history(self):
        context = self.context()
        self.assertTrue(context.display.title("  Caller workspace inspected  "))
        self.assertFalse(hasattr(context.chat, "history"))
        self.assertEqual(context.close().failed, 0)
        event = EventHandler.events[-1][0]
        self.assertEqual(event["type"], "CUSTOM")
        self.assertEqual(event["name"], "nexus.display.title")
        self.assertEqual(event["value"]["title"], "Caller workspace inspected")

    def test_chat_ask_is_fail_closed_for_json_transport(self):
        context = NexusRunContext(
            run_id="run-json",
            events_url=self.events_url,
            token="event-token",
            interaction_url="http://example.invalid/interactions/",
            interaction_token="private-token",
            interaction_mode="json",
        )
        with self.assertRaises(NexusChatUnavailable):
            context.chat.ask("Continue?", key="confirm")

    def test_resumed_run_exposes_run_context_and_namespaces_interactions_by_turn(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        context = NexusRunContext(
            run_id="run-1",
            events_url=self.events_url,
            token="event-token",
            interaction_url=base + "/interactions/",
            context_url=base + "/context/",
            context_token="context-token",
            interaction_token="interactive-token",
            interaction_mode="task",
            turn_index=2,
            config=NexusReportingConfig(retry_delay=0),
        )

        self.assertTrue(context.is_resumed)
        self.assertEqual(context.run.messages()[0]["content"], "Previous question")
        self.assertEqual(context.project.id, "project-1")
        self.assertEqual(context.project.name, "Research")
        self.assertEqual(context.project.instructions, "Use verified sources.")
        self.assertEqual(context.project.revision, 3)
        reply = context.chat.ask("Continue?", key="confirm", timeout=2)

        self.assertEqual(reply.key, "confirm")
        self.assertEqual(EventHandler.interactions[reply.interaction_id]["key"], "turn-2:confirm")
        self.assertEqual(asyncio.run(context.aio.run.messages())[0]["sequence"], 1)
        self.assertNotIn("private-token", repr(context))
        self.assertNotIn("context-token", repr(context))

    def test_header_context_overrides_environment_and_no_context_is_noop(self):
        with mock.patch.dict(
            "os.environ",
            {
                "NEXUS_AGUI_RUN_ID": "environment-run",
                "NEXUS_AGUI_EVENTS_URL": "http://environment.invalid/events",
                "NEXUS_AGUI_TOKEN": "environment-token",
            },
            clear=True,
        ):
            context = NexusRunContext.from_env(
                headers={
                    "x-nexus-agui-run-id": "header-run",
                    "x-nexus-agui-events-url": self.events_url,
                    "x-nexus-agui-token": "header-token",
                }
            )
        self.assertEqual(context.run_id, "header-run")
        context.emit({"type": "STATE_SNAPSHOT", "snapshot": {"ok": True}})
        self.assertEqual(context.close().sent, 1)

        with mock.patch.dict("os.environ", {}, clear=True):
            disabled = NexusRunContext.from_env()
        self.assertFalse(disabled.enabled)
        self.assertFalse(disabled.emit({"type": "CUSTOM", "name": "test", "value": {}}))
        self.assertEqual(disabled.close().sent, 0)

    def test_native_handler_injects_run_context_and_current_run(self):
        agent = NexusAgent(
            router="http://127.0.0.1:1",
            auth="none",
            tenant="demo",
            agent_id="reporter",
            listen_host="127.0.0.1",
            advertise_address="192.0.2.1",
        )
        context = self.context()
        seen = []

        @agent.capability("demo.report", public_ipv6=False)
        def report(payload, ctx: NexusRunContext):
            seen.append((ctx is context, current_run() is context, payload))
            ctx.emit({"type": "STATE_SNAPSHOT", "snapshot": {"ok": True}})
            return {"ok": True}

        envelope = AgentEnvelope(
            version="1.0",
            intent="demo.report",
            intent_version=1,
            task_id="task-1",
            source_agent="agent://demo/caller",
            tenant="demo",
            hop_limit=8,
            payload={"message": "hello"},
            run_context=context,
        )
        try:
            result = agent.server._handlers["demo.report"](envelope)
            self.assertEqual(result, {"ok": True})
            self.assertEqual(seen, [(True, True, {"message": "hello"})])
            self.assertEqual(context.close().sent, 1)
        finally:
            agent.server.server_close()

    def test_native_stream_keeps_context_during_iteration(self):
        agent = NexusAgent(
            router="http://127.0.0.1:1",
            auth="none",
            tenant="demo",
            agent_id="stream-reporter",
            listen_host="127.0.0.1",
            advertise_address="192.0.2.2",
        )
        context = self.context()
        seen = []

        @agent.capability("demo.stream", public_ipv6=False)
        def sync_fallback(payload):
            return payload

        @agent.stream_capability("demo.stream", public_ipv6=False)
        def stream(payload, ctx: NexusRunContext):
            seen.append((ctx is context, current_run() is context))
            yield {"progress": 1}
            seen.append((ctx is context, current_run() is context))

        envelope = AgentEnvelope(
            version="1.0",
            intent="demo.stream",
            intent_version=1,
            task_id="stream-task",
            source_agent="agent://demo/caller",
            tenant="demo",
            hop_limit=8,
            payload={},
            run_context=context,
        )
        try:
            items = list(agent.server._stream_handlers["demo.stream"](envelope))
            self.assertEqual(items, [{"progress": 1}])
            self.assertEqual(seen, [(True, True), (True, True)])
        finally:
            context.close()
            agent.server.server_close()

    def test_delivery_failure_is_fail_open(self):
        context = NexusRunContext(
            run_id="run-failure",
            events_url="http://127.0.0.1:1/events",
            token="never-logged-token",
            config=NexusReportingConfig(
                request_timeout=0.1,
                max_retries=0,
                retry_delay=0,
                flush_timeout=1,
            ),
        )
        self.assertTrue(context.emit({"type": "STEP_STARTED", "stepName": "safe"}))
        report = context.close()
        self.assertEqual(report.failed, 1)
        self.assertNotIn("never-logged-token", report.last_error)

    def test_fastmcp_dependency_uses_request_headers_and_flushes(self):
        dependencies = types.ModuleType("fastmcp.dependencies")
        server_dependencies = types.ModuleType("fastmcp.server.dependencies")
        dependencies.Depends = lambda function: function
        server_dependencies.get_http_headers = lambda: {
            "X-Nexus-AGUI-Run-Id": "fastmcp-run",
            "X-Nexus-AGUI-Events-Url": self.events_url,
            "X-Nexus-AGUI-Token": "fastmcp-token",
        }
        with mock.patch.dict(
            sys.modules,
            {
                "fastmcp.dependencies": dependencies,
                "fastmcp.server.dependencies": server_dependencies,
            },
        ):
            dependency = CurrentNexusRun()
            manager = dependency()
            with manager as context:
                self.assertEqual(context.run_id, "fastmcp-run")
                self.assertIs(current_run(), context)
                context.emit({"type": "STEP_STARTED", "stepName": "fastmcp"})

        self.assertEqual(context.report().sent, 1)

    def test_emit_accepts_official_style_model_and_private_visibility(self):
        class EventType(Enum):
            STEP_STARTED = "STEP_STARTED"

        class OfficialEvent:
            def model_dump(self, **_options):
                return {"type": EventType.STEP_STARTED, "stepName": "official"}

        context = self.context()
        self.assertTrue(context.emit(OfficialEvent(), visibility="private"))
        self.assertEqual(context.close().sent, 1)
        self.assertEqual(EventHandler.events[0][0]["type"], "STEP_STARTED")
        self.assertEqual(EventHandler.events[0][0]["visibility"], "private")

    def test_run_scoped_computer_workspace_memory_and_output_helpers(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        context = NexusRunContext(
            run_id="run-computer",
            events_url=self.events_url,
            token="agui-token",
            workspace_token="run-workspace-token",
            computer_enabled=True,
            terminal_url=base + "/terminal",
            workspace_url=base + "/workspace",
            memory_url=base + "/memory",
            output_root="~/.nexus/agents/a/runs/r/output",
            config=NexusReportingConfig(retry_delay=0),
        )
        self.assertTrue(context.computer.enabled)
        self.assertEqual(context.terminal.status()["viewer_mode"], "read_only")
        self.assertEqual(context.memory.recall()[0]["scope"], "caller")
        command = context.terminal.run("printf hello")
        self.assertEqual(command.exit_code, 0)
        self.assertEqual(command.stdout, "hello\n")
        self.assertEqual(context.workspace.list()[0].name, "input.txt")
        context.workspace.write_text("notes.txt", "hello")
        context.output.write_text("report.md", "# report", content_type="text/markdown")
        context.memory.add("private preference", consent="approved")
        self.assertEqual(context.close().failed, 0)
        memory_event = [item[0] for item in EventHandler.events if item[0].get("name") == "nexus.memory.item"][-1]
        self.assertEqual(memory_event["value"]["scope"], "caller")
        self.assertNotIn("run-workspace-token", repr(context))

    def test_terminal_run_can_hide_command_from_private_display_shell(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        context = NexusRunContext(
            run_id="run-terminal-display",
            events_url=self.events_url,
            token="agui-token",
            workspace_token="workspace-token",
            computer_enabled=True,
            terminal_url=base + "/terminal",
            config=NexusReportingConfig(retry_delay=0),
        )

        visible = context.terminal.run("printf visible")
        hidden = context.terminal.run("printf hidden", display=False)
        async_hidden = asyncio.run(
            context.aio.terminal.run("printf async-hidden", display=False)
        )

        self.assertTrue(visible.displayed)
        self.assertFalse(hidden.displayed)
        self.assertFalse(async_hidden.displayed)
        self.assertEqual(
            [request[2]["display"] for request in EventHandler.terminal_requests],
            [True, False, False],
        )

    def test_one_cloud_opener_covers_all_run_context_services(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        cloud_requests = []

        def cloud_opener(request, *, timeout, exchange=False):
            cloud_requests.append(
                (
                    request.full_url,
                    exchange,
                    dict(request.header_items()),
                    timeout,
                    request.get_method(),
                )
            )
            return urllib.request.urlopen(request, timeout=timeout)

        context = NexusRunContext(
            run_id="run-one-tls-context",
            events_url=self.events_url,
            token="event-token",
            computer_enabled=True,
            terminal_url=base + "/terminal",
            workspace_url=base + "/workspace",
            memory_url=base + "/memory",
            workspace_token="workspace-token",
            interaction_url=base + "/interactions/",
            interaction_token="interaction-token",
            interaction_mode="stream",
            checkpoint_url=base + "/checkpoint/",
            display_asset_url=base + "/display-assets/",
            mobile_enabled=True,
            mobile_delegate_url=base + "/mobile/",
            mobile_delegate_token="mobile-token",
            mobile_capabilities=("mobile.observe",),
            config=NexusReportingConfig(retry_delay=0),
            _cloud_opener=cloud_opener,
        )
        context.plan.set([{"id": "inspect", "title": "Inspect", "status": "running"}])
        context.chat.say("Working")
        self.assertEqual(context.chat.ask("Continue?", key="continue").value, "continue")
        self.assertEqual(context.terminal.status()["status"], "active")
        self.assertEqual(context.terminal.run("printf hello", timeout=30).stdout, "hello\n")
        self.assertEqual(context.workspace.list()[0].name, "input.txt")
        context.workspace.write_text("notes.txt", "hello")
        context.output.write_text("report.md", "# report")
        self.assertEqual(context.memory.recall()[0]["id"], "memory-1")
        self.assertTrue(context.mobile.status().enabled)
        self.assertEqual(context.mobile.observe().data["packageName"], "com.example")
        context.browser.frame(b"png", content_type="image/png", title="frame")
        self.assertEqual(context.close().failed, 0)

        urls = [url for url, _exchange, _headers, _timeout, _method in cloud_requests]
        for path in (
            "/events", "/interactions/", "/terminal", "/workspace",
            "/memory", "/mobile/", "/display-assets/",
        ):
            self.assertTrue(any(path in url for url in urls), path)
        self.assertTrue(
            all(exchange is False for _url, exchange, _headers, _timeout, _method in cloud_requests)
        )
        self.assertTrue(
            any(
                url.endswith("/terminal") and method == "POST" and timeout == 35.0
                for url, _exchange, _headers, timeout, method in cloud_requests
            )
        )
        workspace_requests = [
            headers
            for url, _exchange, headers, _timeout, _method in cloud_requests
            if "/workspace" in url or "/terminal" in url or "/memory" in url
        ]
        self.assertTrue(workspace_requests)
        for headers in workspace_requests:
            normalized = {key.lower(): value for key, value in headers.items()}
            self.assertEqual(normalized["x-nexus-workspace-token"], "workspace-token")
            self.assertEqual(normalized["x-nexus-workspace-delegate-token"], "workspace-token")
            self.assertEqual(normalized["authorization"], "Bearer workspace-token")

    def test_memory_update_delete_are_revision_checked_and_secret_safe(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        context = NexusRunContext(
            run_id="run-memory",
            events_url=self.events_url,
            token="agui-token",
            workspace_token="memory-write-token",
            memory_url=base + "/memory/",
            config=NexusReportingConfig(retry_delay=0),
        )
        recalled = context.memory.recall()
        self.assertEqual(recalled[0]["revision"], 1)
        self.assertEqual(recalled[0]["scope"], "caller")

        updated = context.memory.update(
            recalled[0]["id"],
            revision=recalled[0]["revision"],
            text="Prefer concise Markdown",
            data={},
            confidence=0.98,
        )
        self.assertEqual(updated["text"], "Prefer concise Markdown")
        self.assertEqual(updated["content"], {})
        self.assertEqual(updated["revision"], 2)

        with self.assertRaises(NexusMemoryConflict) as conflict:
            context.memory.update(updated["id"], revision=1, text="stale value")
        self.assertEqual(conflict.exception.current_revision, 2)
        self.assertNotIn("memory-write-token", str(conflict.exception))
        self.assertNotIn("stale value", str(conflict.exception))

        deleted: MemoryDeleteResult = context.memory.delete(updated["id"], revision=2)
        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["revision"], 3)
        self.assertEqual(context.memory.recall(), [])
        memory_requests = [request for request in EventHandler.workspace_requests if request[1].startswith("/memory/")]
        self.assertEqual([request[0] for request in memory_requests], ["PATCH", "PATCH", "DELETE"])
        self.assertTrue(all(request[3].get("X-Nexus-Workspace-Token") == "memory-write-token" for request in memory_requests))
        self.assertNotIn("memory-write-token", repr(context))

    def test_memory_mutation_is_fail_closed_without_managed_context(self):
        context = NexusRunContext()
        with self.assertRaises(NexusMemoryUnavailable):
            context.memory.update("memory-1", revision=1, text="safe")
        with self.assertRaises(NexusMemoryUnavailable):
            context.memory.delete("memory-1", revision=1)
        with self.assertRaises(ValueError):
            context.memory.update("memory-1", revision=1)
        with self.assertRaises(ValueError):
            context.memory.delete("memory-1", revision=0)

    def test_workspace_delegate_crud_binding_and_async_api_are_secret_safe(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        secret_key = "-----BEGIN PRIVATE KEY-----never-print-this"
        secret_password = "never-print-password"
        context = NexusRunContext(
            run_id="run-workspace",
            events_url=self.events_url,
            token="agui-token",
            workspace_token="delegate-token",
            workspace_delegate_url=base + "/manage/",
            workspace_delegate_token="delegate-token",
            workspace_capabilities=(
                "connection.list", "connection.create", "connection.update",
                "connection.delete", "connection.test", "connection.bind",
                "files.list", "files.read", "files.write", "command.execute",
            ),
            config=NexusReportingConfig(retry_delay=0),
        )
        create = SSHWorkspaceConnectionCreate(
            name="Research Computer",
            ssh_host="computer.internal",
            ssh_user="agent",
            private_key=secret_key,
            password=secret_password,
        )
        self.assertNotIn(secret_key, repr(create))
        self.assertNotIn(secret_password, repr(create))

        connection = context.computer.connections.create(
            create.name,
            create.ssh_host,
            create.ssh_user,
            private_key=create.private_key,
            password=create.password,
        )
        self.assertEqual(connection.ssh_host, "computer.internal")
        self.assertNotIn(secret_key, repr(connection))
        self.assertNotIn(secret_password, repr(connection))
        self.assertEqual(context.computer.connections.test(connection.id).status, "succeeded")
        updated = context.computer.connections.update(
            connection.id,
            SSHWorkspaceConnectionUpdate(name="Renamed", password="replacement-secret"),
        )
        self.assertEqual(updated.name, "Renamed")
        self.assertEqual(context.computer.connections.list()[0].id, connection.id)
        binding = context.computer.bind(connection.id)
        self.assertEqual(binding["connection_id"], connection.id)
        self.assertTrue(context.computer.enabled)
        self.assertEqual(context.workspace_root, "~/.nexus/agents/a/workspace")

        async def async_list():
            return await context.aio.computer.connections.list()

        import asyncio
        self.assertEqual(asyncio.run(async_list())[0].name, "Renamed")
        context.computer.connections.delete(connection.id)
        self.assertEqual(context.computer.connections.list(), ())
        create_payload = EventHandler.workspace_requests[0][2]
        self.assertIn("private_key", create_payload)
        self.assertIn("password", create_payload)
        self.assertNotIn(secret_key, repr(connection))
        self.assertNotIn("delegate-token", repr(context))

    def test_workspace_delegate_rejects_ungranted_and_non_managed_operations(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        context = NexusRunContext(
            run_id="run-limited",
            events_url=self.events_url,
            token="agui-token",
            workspace_delegate_url=base + "/manage/",
            workspace_delegate_token="delegate-token",
            workspace_capabilities=("connection.list",),
        )
        with self.assertRaises(NexusComputerError) as denied:
            context.computer.connections.create("Denied", "host", "user")
        self.assertNotIn("delegate-token", str(denied.exception))

        unmanaged = NexusRunContext()
        with self.assertRaises(NexusComputerError):
            unmanaged.computer.connections.list()

    def test_mobile_delegate_supports_status_observe_screen_pixel_actions_and_async(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        token = "mobile-delegate-never-print"
        context = NexusRunContext(
            run_id="run-mobile",
            events_url=self.events_url,
            token="agui-token",
            mobile_enabled=True,
            mobile_delegate_url=base + "/mobile/",
            mobile_delegate_token=token,
            mobile_capabilities=(
                "mobile.observe", "mobile.screen.capture", "mobile.tap", "mobile.swipe",
            ),
        )

        status = context.mobile.status()
        self.assertTrue(status.enabled)
        self.assertTrue(status.available)
        self.assertEqual(status.platform, "android")
        self.assertEqual(context.mobile.observe().data["packageName"], "com.example")
        screen = context.mobile.capture_screen()
        self.assertEqual(screen.content, b"webp-image")
        self.assertNotIn("webp-image", repr(screen))
        context.mobile.tap(x=420, y=860)
        context.mobile.swipe(500, 1400, 500, 400, duration_ms=450)

        action_payloads = [request[2] for request in EventHandler.mobile_requests if request[0] == "POST"]
        self.assertEqual(action_payloads[-2]["arguments"]["coordinate_space"], "pixels")
        self.assertEqual(action_payloads[-1]["arguments"]["coordinate_space"], "pixels")
        self.assertTrue(all(
            request[3].get("X-Nexus-Mobile-Delegate-Token") == token
            for request in EventHandler.mobile_requests
        ))
        self.assertNotIn(token, repr(context))

        async def async_observe():
            return await context.aio.mobile.observe()

        import asyncio
        self.assertEqual(asyncio.run(async_observe()).data["packageName"], "com.example")

    def test_mobile_delegate_is_fail_closed_when_unmanaged_or_scope_is_missing(self):
        unmanaged = NexusRunContext()
        self.assertFalse(unmanaged.mobile.enabled)
        with self.assertRaises(NexusMobileUnavailable):
            unmanaged.mobile.observe()

        base = f"http://127.0.0.1:{self.server.server_port}"
        limited = NexusRunContext(
            run_id="run-mobile-limited",
            events_url=self.events_url,
            token="agui-token",
            mobile_enabled=True,
            mobile_delegate_url=base + "/mobile/",
            mobile_delegate_token="limited-token",
            mobile_capabilities=("mobile.observe",),
        )
        with self.assertRaises(NexusMobilePermissionRequired) as error:
            limited.mobile.type_text("private text")
        self.assertNotIn("private text", str(error.exception))
        self.assertNotIn("limited-token", str(error.exception))

    def test_mobile_command_retries_only_transient_status_reads(self):
        context = NexusRunContext(
            run_id="run-mobile-transient",
            events_url=self.events_url,
            token="agui-token",
            mobile_enabled=True,
            mobile_delegate_url="https://mobile.invalid/",
            mobile_delegate_token="delegate-token",
            mobile_capabilities=("mobile.observe",),
        )
        responses = [
            {"id": "command-1", "action": "observe", "status": "queued"},
            reporting._NexusMobileTransportUnavailable("temporary"),
            {
                "id": "command-1",
                "action": "observe",
                "status": "succeeded",
                "result": {"packageName": "com.example"},
            },
        ]
        with (
            mock.patch.object(context, "_mobile_request", side_effect=responses) as request,
            mock.patch.object(reporting.time, "sleep"),
        ):
            observation = context.mobile.observe(timeout=1)
        self.assertEqual(observation.data["packageName"], "com.example")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args_list[0].kwargs["method"], "POST")
        self.assertEqual(request.call_args_list[1].kwargs["method"], "GET")

    def test_mobile_command_safely_retries_transient_create_with_one_request_id(self):
        context = NexusRunContext(
            run_id="run-mobile-create-retry",
            events_url=self.events_url,
            token="agui-token",
            mobile_enabled=True,
            mobile_delegate_url="https://mobile.invalid/",
            mobile_delegate_token="delegate-token",
            mobile_capabilities=("mobile.observe",),
        )
        responses = [
            reporting._NexusMobileTransportUnavailable("temporary"),
            {
                "id": "command-1",
                "action": "observe",
                "status": "succeeded",
                "result": {"packageName": "com.example"},
            },
        ]
        with (
            mock.patch.object(context, "_mobile_request", side_effect=responses) as request,
            mock.patch.object(reporting.time, "sleep"),
        ):
            observation = context.mobile.observe(timeout=1)
        self.assertEqual(observation.data["packageName"], "com.example")
        self.assertEqual(request.call_count, 2)
        first = request.call_args_list[0].kwargs
        second = request.call_args_list[1].kwargs
        self.assertEqual(first["method"], "POST")
        self.assertEqual(second["method"], "POST")
        self.assertEqual(
            first["payload"]["client_request_id"],
            second["payload"]["client_request_id"],
        )

    def test_mobile_command_preserves_terminal_failure_status_without_sensitive_arguments(self):
        context = NexusRunContext(
            run_id="run-mobile-terminal-status",
            events_url=self.events_url,
            token="agui-token",
            mobile_enabled=True,
            mobile_delegate_url="https://mobile.invalid/",
            mobile_delegate_token="delegate-token",
            mobile_capabilities=("mobile.type_text",),
        )
        cases = {
            "rejected": "MOBILE_ACTION_REJECTED",
            "canceled": "MOBILE_ACTION_CANCELLED",
            "deleted": "MOBILE_ACTION_CANCELLED",
            "failed": "MOBILE_ACTION_FAILED",
        }
        for status, code in cases.items():
            with self.subTest(status=status), mock.patch.object(
                context,
                "_mobile_request",
                return_value={"id": "command-1", "action": "type_text", "status": status},
            ):
                with self.assertRaises(NexusMobileActionFailed) as error:
                    context.mobile.type_text("private text")
                self.assertEqual(error.exception.code, code)
                self.assertNotIn("private text", str(error.exception))
                self.assertNotIn("delegate-token", str(error.exception))


    def test_billing_report_uses_decimal_and_turn_scoped_token(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"accepted":true,"billing_status":"reported"}'

        context = NexusRunContext(
            run_id="run-billing",
            events_url=self.events_url,
            token="agui-token",
            turn_index=2,
            billing_url="https://cloud.example/api/v1/internal/agent-runs/run-billing/billing-report/",
            billing_token="billing-secret",
            billing_currency="USD",
            billing_max_cost="0.050000",
        )
        captured = {}

        def opener(request, **_kwargs):
            captured["request"] = request
            return Response()

        context._cloud_opener = opener
        result = context.billing.report(
            amount=Decimal("0.0321"),
            line_items=[
                {
                    "code": "model",
                    "description": "Model inference",
                    "amount": Decimal("0.0321"),
                }
            ],
            idempotency_key="turn-2-final",
        )
        self.assertTrue(result["accepted"])
        request = captured["request"]
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(request.headers["X-nexus-billing-token"], "billing-secret")
        payload = json.loads(request.data)
        self.assertEqual(payload["turn_index"], 2)
        self.assertEqual(payload["amount"], "0.032100")
        self.assertEqual(payload["line_items"][0]["amount"], "0.032100")

    def test_billing_report_rejects_float_and_missing_context(self):
        context = NexusRunContext(
            run_id="run-billing",
            events_url=self.events_url,
            token="agui-token",
            billing_url="https://cloud.example/billing/",
            billing_token="billing-secret",
            billing_currency="USD",
            billing_max_cost="0.050000",
        )
        with self.assertRaises(TypeError):
            context.billing.report(
                amount=0.01,
                idempotency_key="float-is-not-safe",
            )
        with self.assertRaises(ValueError):
            context.billing.report(
                amount="0.01",
                line_items=[
                    {
                        "code": "unsafe code",
                        "description": "Rejected before transport",
                        "amount": "0.01",
                    }
                ],
                idempotency_key="invalid-code",
            )
        with self.assertRaises(ValueError):
            context.billing.report(
                amount="0.02",
                line_items=[
                    {
                        "code": "model",
                        "description": "Does not match total",
                        "amount": "0.01",
                    }
                ],
                idempotency_key="invalid-total",
            )
        with self.assertRaises(reporting.NexusBillingUnavailable):
            NexusRunContext().billing.report(
                amount=Decimal("0"),
                idempotency_key="missing",
            )

    def test_async_billing_report_uses_sync_contract(self):
        context = NexusRunContext(
            run_id="run-billing",
            events_url=self.events_url,
            token="agui-token",
            billing_url="https://cloud.example/billing/",
            billing_token="billing-secret",
            billing_currency="USD",
            billing_max_cost="1.000000",
        )
        with mock.patch.object(
            context.billing,
            "report",
            return_value={"accepted": True},
        ) as report:
            value = asyncio.run(
                context.aio.billing.report(
                    amount=Decimal("0.5"),
                    idempotency_key="async-final",
                )
            )
        self.assertTrue(value["accepted"])
        report.assert_called_once()

    def test_managed_recovery_replays_committed_operation_without_callback(self):
        context = NexusRunContext(
            run_id="run-recovery",
            events_url=self.events_url,
            token="agui-token",
            interaction_token="interaction-secret",
            recovery_url="https://cloud.example/operations/",
            recovery_managed=True,
            recovery_attempt=2,
            recovery_is_replay=True,
            recovery_last_committed_operation=4,
        )
        prepare = {
            "id": "d12721b2-e38e-4ff6-9997-d7a493a9885a",
            "status": "succeeded",
            "execute": False,
            "replayed": True,
            "result": {"value": {"ok": True}},
            "idempotency_key": "nxo_safe",
        }
        callback = mock.Mock()
        with mock.patch.object(context, "_interaction_request_url", return_value=prepare) as request:
            result = context.recovery.call("memory.update", {"id": "memory-1"}, callback)
        self.assertEqual(result, {"ok": True})
        callback.assert_not_called()
        self.assertTrue(context.recovery.managed)
        self.assertEqual(context.recovery.attempt, 2)
        self.assertTrue(context.recovery.is_replay)
        self.assertEqual(context.recovery.last_committed_operation, 4)
        request.assert_called_once()

    def test_display_message_and_tool_ids_are_stable_across_recovery_attempts(self):
        def capture():
            events = []
            context = NexusRunContext(
                run_id="stable-run",
                turn_index=2,
                events_url="direct://events",
                token="token",
                _event_sink=lambda event: events.append(dict(event)) or True,
            )
            context.chat.say("Stable reply")
            with context.trace.tool("lookup"):
                pass
            return events

        first = capture()
        second = capture()
        self.assertEqual(
            [event.get("messageId") or event.get("toolCallId") for event in first],
            [event.get("messageId") or event.get("toolCallId") for event in second],
        )


if __name__ == "__main__":
    unittest.main()
