from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import unittest
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


RUN_DOCKER_E2E = os.environ.get("NEXUS_RUN_DOCKER_INTERACTIVE_E2E") == "1"


class _NexusCallbackServer(ThreadingHTTPServer):
    events: list[dict[str, Any]]
    interaction: dict[str, Any]
    checkpoint: dict[str, Any]
    public_base: str


class _NexusCallbackHandler(BaseHTTPRequestHandler):
    server: _NexusCallbackServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        value = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        return value if isinstance(value, dict) else {}

    def _reply(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, *, events: bool = False) -> bool:
        if events:
            return self.headers.get("Authorization") == "Bearer docker-e2e-token"
        return self.headers.get("X-Nexus-Interaction-Token") == "docker-e2e-token"

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/events/":
            if not self._authorized(events=True):
                self._reply(404, {})
                return
            self.server.events.append(self._json_body())
            self._reply(201, {"ok": True})
            return
        if not self._authorized():
            self._reply(404, {})
            return
        if self.path == "/display-assets/":
            frame = self._json_body()
            self.server.asset = frame
            self._reply(
                201,
                {
                    "id": "docker-frame-1",
                    "url": self.server.public_base + "/assets/docker-frame-1",
                },
            )
            return
        if self.path == "/interactions/":
            self.server.interaction = self._json_body()
            self._reply(201, {"id": "docker-interaction-1", "status": "pending"})
            return
        self._reply(404, {})

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._reply(404, {})
            return
        if self.path == "/checkpoint/":
            self._reply(200, self.server.checkpoint)
            return
        if self.path == "/interactions/docker-interaction-1/":
            self._reply(
                200,
                {
                    "id": "docker-interaction-1",
                    "status": "answered",
                    "response": {"value": "continue", "text": "continue"},
                    "answered_at": "2026-08-14T00:00:00Z",
                },
            )
            return
        self._reply(404, {})

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authorized() or self.path != "/checkpoint/":
            self._reply(404, {})
            return
        value = self._json_body()
        previous_revision = int(self.server.checkpoint.get("revision") or 0)
        self.server.checkpoint = {
            "stage": value.get("stage"),
            "data": value.get("data") or {},
            "revision": previous_revision + 1,
            "updated_at": "2026-08-14T00:00:00Z",
        }
        self._reply(200, self.server.checkpoint)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _docker_executable() -> str:
    configured = os.environ.get("NEXUS_DOCKER_EXE", "").strip()
    executable = configured or shutil.which("docker") or ""
    if not executable:
        raise unittest.SkipTest("Docker CLI is unavailable")
    return executable


def _mcp_request(
    *,
    url: str,
    payload: dict[str, Any],
    session_id: str = "",
    nexus_headers: dict[str, str] | None = None,
) -> tuple[dict[str, Any], str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
        **(nexus_headers or {}),
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8")
        response_session = str(response.headers.get("Mcp-Session-Id") or session_id)
    data_lines = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
    return (json.loads(data_lines[-1]) if data_lines else {}, response_session)


@unittest.skipUnless(
    RUN_DOCKER_E2E,
    "Set NEXUS_RUN_DOCKER_INTERACTIVE_E2E=1 to run the real Docker scenario.",
)
class DockerInteractiveDisplayE2ETests(unittest.TestCase):
    def test_container_reports_all_display_surfaces_and_completes_chat(self) -> None:
        docker = _docker_executable()
        image = os.environ.get(
            "NEXUS_INTERACTIVE_DISPLAY_IMAGE",
            "nexus-hosted-interactive-display-agent:0.27.0",
        )
        callback = _NexusCallbackServer(("0.0.0.0", 0), _NexusCallbackHandler)
        callback.events = []
        callback.interaction = {}
        callback.checkpoint = {}
        callback.asset = {}
        callback_port = int(callback.server_address[1])
        callback.public_base = f"http://host.docker.internal:{callback_port}"
        callback_thread = threading.Thread(target=callback.serve_forever, daemon=True)
        callback_thread.start()
        mcp_port = _free_port()
        container_name = "nexus-interactive-display-e2e-" + uuid.uuid4().hex[:10]
        container_id = ""
        try:
            run = subprocess.run(
                [
                    docker,
                    "run",
                    "--rm",
                    "-d",
                    "--name",
                    container_name,
                    "--add-host",
                    "host.docker.internal:host-gateway",
                    "-p",
                    f"127.0.0.1:{mcp_port}:8000",
                    image,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            container_id = run.stdout.strip()
            mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
            deadline = time.monotonic() + 20
            while True:
                try:
                    initialized, session_id = _mcp_request(
                        url=mcp_url,
                        payload={
                            "jsonrpc": "2.0",
                            "id": "initialize",
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2025-06-18",
                                "capabilities": {},
                                "clientInfo": {"name": "nexus-docker-e2e", "version": "1"},
                            },
                        },
                    )
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.25)
            self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
            _mcp_request(
                url=mcp_url,
                session_id=session_id,
                payload={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            headers = {
                "X-Nexus-AGUI-Run-Id": "docker-e2e-run",
                "X-Nexus-AGUI-Events-Url": callback.public_base + "/events/",
                "X-Nexus-AGUI-Token": "docker-e2e-token",
                "X-Nexus-Interaction-Url": callback.public_base + "/interactions/",
                "X-Nexus-Interaction-Token": "docker-e2e-token",
                "X-Nexus-Interaction-Mode": "stream",
                "X-Nexus-Checkpoint-Url": callback.public_base + "/checkpoint/",
                "X-Nexus-Display-Asset-Url": callback.public_base + "/display-assets/",
            }
            called, _ = _mcp_request(
                url=mcp_url,
                session_id=session_id,
                nexus_headers=headers,
                payload={
                    "jsonrpc": "2.0",
                    "id": "interactive-call",
                    "method": "tools/call",
                    "params": {"name": "interactive_display", "arguments": {}},
                },
            )

            self.assertFalse(called["result"].get("isError"), called)
            self.assertTrue(callback.events, called)
            event_types = [event.get("type") for event in callback.events]
            event_names = [event.get("name") for event in callback.events]
            self.assertIn("ACTIVITY_SNAPSHOT", event_types)
            self.assertIn("ACTIVITY_DELTA", event_types)
            self.assertIn("TEXT_MESSAGE_CONTENT", event_types)
            self.assertIn("nexus.computer.log", event_names)
            self.assertIn("nexus.computer.frame", event_names)
            self.assertEqual(callback.interaction["key"], "confirm-finish")
            self.assertEqual(callback.interaction["kind"], "confirm")
            self.assertEqual(callback.checkpoint["stage"], "finalizing")
            self.assertEqual(callback.checkpoint["data"], {"selection": "continue"})
            self.assertEqual(callback.asset["content_type"], "image/png")
            uploaded_frame = base64.b64decode(callback.asset["content_base64"], validate=True)
            self.assertLessEqual(len(uploaded_frame), 2 * 1024 * 1024)
        finally:
            callback.shutdown()
            callback.server_close()
            if container_id:
                subprocess.run(
                    [docker, "rm", "-f", container_id],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )


if __name__ == "__main__":
    unittest.main()
