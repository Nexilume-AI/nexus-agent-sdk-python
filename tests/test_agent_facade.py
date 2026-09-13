import json
import ipaddress
import os
import pathlib
import socket
import sys
import threading
import unittest
import urllib.request
from types import SimpleNamespace
from typing import TypedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent import (  # noqa: E402
    McpToolDescriptor,
    NexusAgent,
    NexusAgentError,
    SseEvent,
)
from nexus_agent.agent import (  # noqa: E402
    resolve_advertise_address,
    resolve_router_url,
)


class FacadeProxyHandler(BaseHTTPRequestHandler):
    requests = []
    cloud_responses = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).requests.append((self.path, body, dict(self.headers)))
        if self.path == "/agent/v1/register":
            self._json(201, {
                "route_id": "1234567890abcdef1234567890abcdef",
                "generation": 1,
                "lease_seconds": body["lease_seconds"],
                "removed": False,
                "public_ipv6": "2001:db8:100::20",
                "public_port": 7443,
                "public_endpoint": {
                    "scheme": "http",
                    "address": "2001:db8:100::20",
                    "port": 7443,
                },
            })
        elif self.path == "/agent/v1/unregister":
            self._json(200, {
                "route_id": body["route_id"],
                "generation": 2,
                "lease_seconds": 0,
                "removed": True,
            })
        elif self.path == "/agent/v1/invoke":
            self._json(200, {"seen": body})
        else:
            self._json(404, {"code": "NOT_FOUND", "message": "not found"})

    def do_GET(self):
        type(self).requests.append((self.path, None, dict(self.headers)))
        if self.path == "/agent/v1/cloud-registration":
            body = (
                type(self).cloud_responses.pop(0)
                if type(self).cloud_responses
                else {
                    "state": "ready",
                    "origin": "agent://demo/echo-server",
                    "registration_id": "edge-registration-1",
                    "agent_id": "cloud-agent-1",
                    "runtime_id": "runtime-1",
                    "transport": "relay",
                    "mcp_url": "/api/v1/agents/cloud-agent-1/mcp/",
                }
            )
            self._json(200, body)
        else:
            self._json(404, {"code": "NOT_FOUND", "message": "not found"})

    def _json(self, status, body):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class NexusAgentFacadeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), FacadeProxyHandler)
        cls.proxy_thread = threading.Thread(
            target=cls.proxy.serve_forever, daemon=True
        )
        cls.proxy_thread.start()
        cls.router_url = f"http://127.0.0.1:{cls.proxy.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.proxy_thread.join(timeout=2)

    def setUp(self):
        FacadeProxyHandler.requests.clear()
        FacadeProxyHandler.cloud_responses.clear()

    def _agent(self):
        return NexusAgent(
            router=self.router_url,
            token="test-jwt",
            tenant="demo",
            agent_id="echo-server",
            listen_host="127.0.0.1",
            port=0,
            advertise_address="192.0.2.20",
            lease_seconds=30,
        )

    def test_decorator_starts_registers_invokes_and_unregisters(self):
        agent = self._agent()
        received = []

        @agent.capability("demo.echo")
        def echo(payload):
            received.append(payload)
            return {"echo": payload}

        lines = []
        handle = agent.start(auto_renew=False, print_fn=lines.append)
        try:
            registration = FacadeProxyHandler.requests[0][1]
            self.assertEqual(registration["origin"], "agent://demo/echo-server")
            self.assertEqual(registration["tenant"], "demo")
            self.assertEqual(registration["public_ipv6"], "auto")
            self.assertEqual(
                registration["endpoint"],
                f"http://192.0.2.20:{agent.server.port}/invoke",
            )
            self.assertEqual(handle.published[0].public_url,
                             "http://[2001:db8:100::20]:7443")
            self.assertIn("Transport: Plain HTTP + JWT", lines)

            envelope = {
                "version": "1.0",
                "intent": "demo.echo",
                "intent_version": 1,
                "task_id": "task-facade-1",
                "source_agent": "agent://demo/caller",
                "tenant": "demo",
                "hop_limit": 8,
                "constraints": {},
                "payload": {"message": "hello"},
            }
            request = urllib.request.Request(
                f"http://127.0.0.1:{agent.server.port}/invoke",
                data=json.dumps(envelope).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                result = json.load(response)
            self.assertEqual(result, {"echo": {"message": "hello"}})
            self.assertEqual(received, [{"message": "hello"}])
        finally:
            handle.close()

        self.assertEqual(
            [item[0] for item in FacadeProxyHandler.requests],
            ["/agent/v1/register", "/agent/v1/unregister"],
        )
        with self.assertRaisesRegex(NexusAgentError, "new NexusAgent"):
            agent.start(auto_renew=False)

    def test_default_public_ipv6_uses_relay_without_address_request(self):
        agent = self._agent()
        agent.client.router_auth_metadata = mock.Mock(return_value=SimpleNamespace(
            cloud=SimpleNamespace(
                transports=SimpleNamespace(direct_ipv6=False, relay=True, auto=True)
            )
        ))

        @agent.capability("demo.relay")
        def relay(payload):
            return payload

        handle = agent.start(auto_renew=False, announce=False)
        try:
            registration = FacadeProxyHandler.requests[0][1]
            self.assertNotIn("public_ipv6", registration)
        finally:
            handle.close()

    def test_explicit_public_ipv6_overrides_transport_discovery(self):
        agent = self._agent()
        agent.client.router_auth_metadata = mock.Mock()

        @agent.capability("demo.direct", public_ipv6=True)
        def direct(payload):
            return payload

        handle = agent.start(auto_renew=False, announce=False)
        try:
            registration = FacadeProxyHandler.requests[0][1]
            self.assertEqual(registration["public_ipv6"], "auto")
            agent.client.router_auth_metadata.assert_not_called()
        finally:
            handle.close()

    def test_agent_can_invoke_without_repeating_identity(self):
        agent = self._agent()
        try:
            result = agent.invoke(
                "demo.remote",
                {"value": 7},
                target_agent="agent://remote/worker-2",
                task_id="task-outbound-1",
            )
            envelope = result["seen"]
            self.assertEqual(envelope["tenant"], "demo")
            self.assertEqual(envelope["source_agent"], agent.origin)
            self.assertEqual(envelope["target_agent"], "agent://remote/worker-2")
        finally:
            agent.server.server_close()

    def test_explicit_no_jwt_ignores_ambient_environment_token(self):
        with mock.patch.dict(os.environ, {"NEXUS_AGENT_TOKEN": "ambient-jwt"}):
            agent = NexusAgent(
                router=self.router_url,
                auth="none",
                tenant="demo",
                agent_id="no-jwt-caller",
                listen_host="127.0.0.1",
                port=0,
                advertise_address="192.0.2.21",
                lease_seconds=30,
            )
        try:
            result = agent.invoke("demo.remote", {"value": 9})
            self.assertEqual(result["seen"]["source_agent"], agent.origin)
            headers = FacadeProxyHandler.requests[-1][2]
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("Txn-Token", headers)
        finally:
            agent.server.server_close()

    def test_explicit_cloud_ca_overrides_automatic_router_trust(self):
        router_opener = mock.Mock()
        explicit_opener = mock.Mock()
        provider = SimpleNamespace(
            refreshable=True,
            get_token=mock.Mock(return_value="lan-token"),
            open_cloud_request=router_opener,
        )
        resolver = SimpleNamespace(open_cloud_request=explicit_opener)
        with mock.patch(
            "nexus_agent.agent.StaticCloudTrustResolver", return_value=resolver
        ) as create_resolver:
            agent = NexusAgent(
                router=self.router_url,
                token_provider=provider,
                cloud_ca_file="advanced-ca.pem",
                tenant="demo",
                agent_id="explicit-cloud-ca",
                listen_host="127.0.0.1",
                advertise_address="192.0.2.22",
            )
        try:
            create_resolver.assert_called_once_with("advanced-ca.pem")
            self.assertIs(agent.server.run_context_opener, explicit_opener)
            self.assertIsNot(agent.server.run_context_opener, router_opener)
        finally:
            agent.server.server_close()

    def test_stream_wrapper_builds_identity_and_resume_options(self):
        agent = self._agent()
        try:
            events = [SseEvent(data="done", event="result", event_id="1")]
            agent.client.invoke_stream = mock.Mock(return_value=iter(events))
            result = list(agent.invoke_stream(
                "demo.progress",
                {"job": 4},
                target_agent="agent://remote/worker-3",
                task_id="task-stream-1",
                max_reconnects=5,
            ))
            self.assertEqual(result, events)
            envelope = agent.client.invoke_stream.call_args.args[0]
            options = agent.client.invoke_stream.call_args.kwargs
            self.assertEqual(envelope["source_agent"], agent.origin)
            self.assertEqual(envelope["target_agent"], "agent://remote/worker-3")
            self.assertEqual(options["max_reconnects"], 5)
        finally:
            agent.server.server_close()

    def test_pass_envelope_and_stream_decorators_share_registration(self):
        agent = self._agent()

        @agent.capability("demo.inspect", pass_envelope=True)
        def inspect(envelope):
            return envelope.task_id

        @agent.stream_capability("demo.inspect")
        def progress(payload):
            yield payload

        registrations = agent.registrations()
        self.assertEqual(len(registrations), 1)
        self.assertEqual(registrations[0].intent, "demo.inspect")
        agent.server.server_close()

    def test_cloud_tool_is_inferred_and_mcp_arguments_are_unwrapped(self):
        class EchoInput(TypedDict):
            message: str

        agent = self._agent()
        received = []

        @agent.capability("demo.echo")
        def echo(payload: EchoInput):
            """Return the caller's message unchanged."""

            received.append(payload)
            return payload

        registration = agent.registrations()[0].to_dict()
        cloud = registration["cloud"]
        self.assertTrue(cloud["publish"])
        self.assertEqual(cloud["agent_name"], "echo-server")
        self.assertEqual(cloud["tool"]["name"], "demo.echo")
        self.assertNotIn("mobile_scopes", cloud["tool"])
        self.assertEqual(
            cloud["tool"]["input_schema"]["properties"]["message"],
            {"type": "string"},
        )
        self.assertEqual(len(cloud["manifest_digest"]), 64)

        thread = agent.server.serve_in_thread()
        try:
            envelope = {
                "version": "1.0",
                "intent": "demo.echo",
                "intent_version": 1,
                "task_id": "task-mcp-1",
                "source_agent": "agent://demo/caller",
                "tenant": "demo",
                "hop_limit": 8,
                "constraints": {},
                "payload": {
                    "protocol": "mcp",
                    "selector": "demo.echo",
                    "request": {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "demo.echo",
                            "arguments": {"message": "hello cloud"},
                        },
                    },
                },
            }
            request = urllib.request.Request(
                f"http://127.0.0.1:{agent.server.port}/invoke",
                data=json.dumps(envelope).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                result = json.load(response)
            self.assertEqual(result, {"message": "hello cloud"})
            self.assertEqual(received, [{"message": "hello cloud"}])
        finally:
            agent.server.shutdown()
            thread.join(timeout=2)
            agent.server.server_close()

    def test_explicit_cloud_tool_override_and_lan_only(self):
        agent = self._agent()
        descriptor = McpToolDescriptor(
            name="stable_echo",
            title="Stable echo",
            description="Explicit metadata",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )

        @agent.capability("demo.explicit", tool=descriptor)
        def explicit(payload):
            return payload

        @agent.capability("demo.lan", tool=False)
        def lan_only(payload):
            return payload

        registrations = {item.intent: item.to_dict() for item in agent.registrations()}
        self.assertEqual(
            registrations["demo.explicit"]["cloud"]["tool"]["name"],
            "stable_echo",
        )
        self.assertNotIn("cloud", registrations["demo.lan"])
        agent.server.server_close()

    def test_computer_contract_is_normalized_and_part_of_manifest_digest(self):
        agent = NexusAgent(
            router=self.router_url,
            token="test-jwt",
            tenant="demo",
            agent_id="computer-agent",
            listen_host="127.0.0.1",
            port=0,
            advertise_address="192.0.2.20",
            lease_seconds=30,
            computer_requirement="required",
            workspace_capabilities=(
                "command.execute",
                "files.read",
                "files.list",
                "files.read",
                "files.write",
            ),
        )

        @agent.capability("demo.computer", public_ipv6=False)
        def computer(payload):
            return payload

        cloud = agent.registrations()[0].to_dict()["cloud"]
        self.assertEqual(cloud["computer"], {
            "requirement": "required",
            "workspace_capabilities": [
                "files.list", "files.read", "files.write", "command.execute"
            ],
        })
        without_computer = dict(cloud)
        without_computer.pop("manifest_digest")
        without_computer.pop("computer")
        self.assertNotEqual(
            cloud["manifest_digest"],
            __import__("hashlib").sha256(
                json.dumps(
                    without_computer,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        )
        agent.server.server_close()

    def test_computer_contract_validation_and_default_compatibility(self):
        with self.assertRaisesRegex(ValueError, "disabled Computer"):
            NexusAgent(
                router=self.router_url,
                token="test-jwt",
                tenant="demo",
                agent_id="bad-computer-agent",
                listen_host="127.0.0.1",
                advertise_address="192.0.2.20",
                workspace_capabilities=("files.read",),
            )
        with self.assertRaisesRegex(ValueError, "unsupported Workspace"):
            NexusAgent(
                router=self.router_url,
                token="test-jwt",
                tenant="demo",
                agent_id="bad-scope-agent",
                listen_host="127.0.0.1",
                advertise_address="192.0.2.20",
                computer_requirement="required",
                workspace_capabilities=("personal.files.read",),
            )

        agent = self._agent()

        @agent.capability("demo.default-computer")
        def default_computer(payload):
            return payload

        self.assertNotIn(
            "computer", agent.registrations()[0].to_dict()["cloud"]
        )
        agent.server.server_close()

    def test_old_router_rejects_non_default_computer_contract_before_register(self):
        agent = NexusAgent(
            router=self.router_url,
            token="test-jwt",
            tenant="demo",
            agent_id="old-router-computer-agent",
            listen_host="127.0.0.1",
            port=0,
            advertise_address="192.0.2.20",
            lease_seconds=30,
            computer_requirement="required",
            workspace_capabilities=("files.read",),
        )
        agent.client.router_auth_metadata = mock.Mock(return_value=SimpleNamespace(
            cloud=SimpleNamespace(
                manifest_schema_version=1,
                transports=SimpleNamespace(direct_ipv6=False, relay=True, auto=True),
            )
        ))

        @agent.capability("demo.old-router", public_ipv6=False)
        def old_router(payload):
            return payload

        try:
            with self.assertRaisesRegex(
                NexusAgentError, "CLOUD_COMPUTER_CONTRACT_UNSUPPORTED"
            ):
                agent.start(auto_renew=False, announce=False)
        finally:
            agent.server.server_close()

    def test_schema_v3_router_rejects_browser_control_contract(self):
        agent = NexusAgent(
            router=self.router_url,
            token="test-jwt",
            tenant="demo",
            agent_id="attached-browser-agent",
            listen_host="127.0.0.1",
            port=0,
            advertise_address="192.0.2.20",
            lease_seconds=30,
            computer_requirement="required",
            workspace_capabilities=("browser.control",),
        )
        agent.client.router_auth_metadata = mock.Mock(return_value=SimpleNamespace(
            cloud=SimpleNamespace(
                manifest_schema_version=3,
                transports=SimpleNamespace(direct_ipv6=False, relay=True, auto=True),
            )
        ))

        @agent.capability("demo.attached-browser", public_ipv6=False)
        def attached_browser(payload):
            return payload

        try:
            with self.assertRaisesRegex(
                NexusAgentError, "CLOUD_BROWSER_CONTRACT_UNSUPPORTED"
            ):
                agent.start(auto_renew=False, announce=False)
        finally:
            agent.server.server_close()

    def test_mobile_contract_and_tool_scopes_are_published_in_manifest(self):
        scopes = (
            "mobile.observe",
            "mobile.screen.capture",
            "mobile.tap",
            "mobile.type_text",
        )
        agent = NexusAgent(
            router=self.router_url,
            token="test-jwt",
            tenant="demo",
            agent_id="mobile-agent",
            listen_host="127.0.0.1",
            advertise_address="192.0.2.20",
            mobile_requirement="required",
            mobile_capabilities=scopes,
        )
        descriptor = McpToolDescriptor(
            name="validate_mobile",
            task=True,
            interactive=True,
            mobile_scopes=("mobile.observe", "mobile.type_text"),
        )

        @agent.capability("demo.mobile", public_ipv6=True, tool=descriptor)
        def mobile(payload):
            return payload

        cloud = agent.registrations()[0].to_dict()["cloud"]
        self.assertEqual(cloud["mobile"], {
            "requirement": "required",
            "mobile_capabilities": list(scopes),
        })
        self.assertEqual(
            cloud["tool"]["mobile_scopes"],
            ["mobile.observe", "mobile.type_text"],
        )
        agent.server.server_close()

    def test_mobile_contract_requires_router_manifest_schema_three(self):
        agent = NexusAgent(
            router=self.router_url,
            token="test-jwt",
            tenant="demo",
            agent_id="old-router-mobile-agent",
            listen_host="127.0.0.1",
            advertise_address="192.0.2.20",
            mobile_requirement="required",
            mobile_capabilities=("mobile.observe",),
        )
        agent.client.router_auth_metadata = mock.Mock(return_value=SimpleNamespace(
            cloud=SimpleNamespace(
                manifest_schema_version=2,
                transports=SimpleNamespace(direct_ipv6=True, relay=False, auto=True),
            )
        ))

        @agent.capability("demo.mobile-old", public_ipv6=True)
        def mobile_old(payload):
            return payload

        try:
            with self.assertRaisesRegex(
                NexusAgentError, "CLOUD_MOBILE_CONTRACT_UNSUPPORTED"
            ):
                agent.start(auto_renew=False, announce=False)
        finally:
            agent.server.server_close()

    def test_wait_for_cloud_returns_formal_agent(self):
        FacadeProxyHandler.cloud_responses.extend([
            {"state": "unavailable", "message": "node presence is recovering"},
            {"state": "pending", "message": "reconciling"},
            {
                "state": "ready",
                "origin": "agent://demo/echo-server",
                "registration_id": "edge-registration-1",
                "agent_id": "cloud-agent-1",
                "runtime_id": "runtime-1",
                "transport": "relay",
                "mcp_url": "/api/v1/agents/cloud-agent-1/mcp/",
            },
        ])
        agent = self._agent()

        @agent.capability("demo.echo")
        def echo(payload):
            return payload

        handle = agent.start(auto_renew=False, announce=False)
        try:
            status = handle.wait_for_cloud(timeout=1, poll_interval=0.001)
            self.assertTrue(status.ready)
            self.assertEqual(status.agent_id, "cloud-agent-1")
            self.assertEqual(status.transport, "relay")
        finally:
            handle.close()

    def test_start_requires_capability(self):
        agent = self._agent()
        try:
            with self.assertRaisesRegex(NexusAgentError, "at least one"):
                agent.start(auto_renew=False)
        finally:
            agent.server.server_close()

    def test_auto_discovery_has_explicit_environment_overrides(self):
        with mock.patch.dict(
            os.environ,
            {
                "NEXUS_ROUTER_URL": "http://[fd00::1]:7443",
                "NEXUS_AGENT_ADDRESS": "fd00::20",
            },
            clear=False,
        ):
            self.assertEqual(resolve_router_url(), "http://[fd00::1]:7443")
            self.assertEqual(resolve_advertise_address(), "fd00::20")

    def test_auto_agent_address_prefers_router_callable_lan_address(self):
        candidates = (
            ipaddress.ip_address("2606:4700:4700::20"),
            ipaddress.ip_address("192.168.10.20"),
            ipaddress.ip_address("fd42:10::20"),
        )
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "nexus_agent.agent._candidate_host_addresses",
            return_value=iter(candidates),
        ):
            self.assertEqual(resolve_advertise_address(), "fd42:10::20")

    def test_auto_agent_address_prefers_route_selected_source(self):
        candidates = (ipaddress.ip_address("fd42:10::20"),)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "nexus_agent.agent._router_selected_address",
            return_value=ipaddress.ip_address("192.168.250.164"),
        ), mock.patch(
            "nexus_agent.agent._candidate_host_addresses",
            return_value=iter(candidates),
        ):
            self.assertEqual(
                resolve_advertise_address(
                    router_url="http://192.168.250.1:7446"
                ),
                "192.168.250.164",
            )

    def test_router_auto_discovery_falls_back_to_default_gateway(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "nexus_agent.agent.socket.getaddrinfo", side_effect=OSError
        ), mock.patch(
            "nexus_agent.agent._default_ipv4_gateway", return_value="192.0.2.1"
        ):
            self.assertEqual(resolve_router_url(), "http://192.0.2.1:7446")

    def test_router_auto_discovery_uses_lan_sdk_port_for_mdns(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "nexus_agent.agent.socket.getaddrinfo", return_value=[object()]
        ) as getaddrinfo:
            self.assertEqual(
                resolve_router_url(),
                "http://nexus-router.local:7446",
            )
        getaddrinfo.assert_called_once_with(
            "nexus-router.local", 7446, 0, socket.SOCK_STREAM
        )

    def test_router_auto_discovery_finds_windows_on_link_router(self):
        route_table = """
IPv4 Route Table
===========================================================================
Active Routes:
Network Destination        Netmask          Gateway       Interface  Metric
          0.0.0.0          0.0.0.0      192.168.1.1    192.168.1.157     25
      192.168.1.0    255.255.255.0         On-link     192.168.1.157    281
    192.168.250.0    255.255.255.0         On-link   192.168.250.164   5256
===========================================================================
"""

        def connect(candidate, timeout):
            host, port = candidate
            self.assertEqual(port, 7446)
            if host == "192.168.250.1":
                return mock.Mock()
            raise OSError("closed")

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "nexus_agent.agent.os.name", "nt"
        ), mock.patch(
            "nexus_agent.agent.socket.getaddrinfo", side_effect=OSError
        ), mock.patch(
            "nexus_agent.agent._default_ipv4_gateway", return_value=None
        ), mock.patch(
            "nexus_agent.agent.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout=route_table),
        ) as run, mock.patch(
            "nexus_agent.agent.socket.create_connection", side_effect=connect
        ):
            self.assertEqual(resolve_router_url(), "http://192.168.250.1:7446")

        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
