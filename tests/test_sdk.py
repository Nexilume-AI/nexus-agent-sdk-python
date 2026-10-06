import json
import pathlib
import socket
import sys
import ssl
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent import (  # noqa: E402
    AgentRequestError,
    BackendTlsIdentity,
    CapabilityRegistration,
    CloudRegistrationManifest,
    DirectIPv6Agent,
    NexusAgentClient,
    NexusAgentServer,
    NexusBrowserActionFailed,
    NexusComputerError,
    NexusHttpError,
    NexusMobileBusy,
    NexusMobileActionFailed,
    NexusMobilePermissionRequired,
    McpToolDescriptor,
    PublicAgentEndpoint,
    SseEvent,
)
from nexus_agent.client import _TlsServerNameHTTPSConnection  # noqa: E402
from nexus_agent.server import _managed_service_error  # noqa: E402


class ManagedServiceErrorTests(unittest.TestCase):
    def test_mobile_busy_keeps_specific_error_code(self):
        self.assertEqual(
            _managed_service_error(
                NexusMobileBusy("Mobile is being controlled by another Run")
            ),
            ("MOBILE_BUSY", "Mobile is being controlled by another Run"),
        )

    def test_mobile_permission_keeps_specific_error_code(self):
        self.assertEqual(
            _managed_service_error(
                NexusMobilePermissionRequired("Mobile capability is not authorized")
            ),
            (
                "MOBILE_PERMISSION_REQUIRED",
                "Mobile capability is not authorized",
            ),
        )

    def test_mobile_action_keeps_safe_terminal_error_code(self):
        self.assertEqual(
            _managed_service_error(
                NexusMobileActionFailed(
                    "Mobile action was rejected by the caller",
                    code="MOBILE_ACTION_REJECTED",
                )
            ),
            (
                "MOBILE_ACTION_REJECTED",
                "Mobile action was rejected by the caller",
            ),
        )
        forged = NexusMobileActionFailed("private", code="MOBILE_FAKE")
        self.assertEqual(
            _managed_service_error(forged),
            (
                "MOBILE_ACTION_FAILED",
                "Mobile action failed on the attached device",
            ),
        )


class FakeProxyHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        type(self).requests.append((self.path, body, dict(self.headers)))
        if self.path == "/agent/v1/register":
            response = {
                "route_id": "0123456789abcdef0123456789abcdef",
                "generation": 7,
                "lease_seconds": body["lease_seconds"],
                "removed": False,
            }
            if body.get("public_ipv6") == "auto":
                response["public_ipv6"] = "2001:db8:1234:5678::beef"
                response["public_port"] = 7443
                response["tls_server_name"] = "router-a.example.test"
                response["ca_bundle_id"] = "enterprise-agent-ca-v1"
                response["public_endpoint"] = {
                    "scheme": "https",
                    "address": response["public_ipv6"],
                    "port": response["public_port"],
                    "tls_server_name": response["tls_server_name"],
                    "ca_bundle_id": response["ca_bundle_id"],
                }
            self._json(201, response)
        elif self.path == "/agent/v1/renew":
            self._json(200, {
                "route_id": body["route_id"],
                "generation": 8,
                "lease_seconds": body.get("lease_seconds", 30),
                "removed": False,
            })
        elif self.path == "/agent/v1/unregister":
            self._json(200, {
                "route_id": body["route_id"],
                "generation": 9,
                "lease_seconds": 0,
                "removed": True,
            })
        elif self.path == "/agent/v1/invoke":
            self._json(200, {"echo": body["payload"]})
        elif self.path == "/nested-denied":
            self._json(401, {"error": {
                "code": "AUTHENTICATION_REQUIRED",
                "message": "access token required",
            }})
        else:
            self._json(403, {"code": "INSUFFICIENT_SCOPE", "message": "denied"})

    def _json(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ResumeProxyHandler(BaseHTTPRequestHandler):
    requests = []
    executions = 0

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        type(self).requests.append((body, dict(self.headers)))
        cursor = int(body.get("resume_from_event_id", 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if cursor == 0:
            type(self).executions += 1
            self.wfile.write(b"event: progress\nid: 1\ndata: accepted\n\n")
            self.wfile.flush()
            return
        self.wfile.write(b"event: progress\nid: 2\ndata: continued\n\n")
        self.wfile.write(b"event: result\nid: 3\ndata: {\"ok\":true}\n\n")
        self.wfile.write(b": nexus-stream-complete\n\n")
        self.wfile.flush()


class SelfHealingProxyHandler(BaseHTTPRequestHandler):
    requests = []
    register_count = 0
    renew_not_found_once = False

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        type(self).requests.append((self.path, body))
        if self.path == "/agent/v1/register":
            type(self).register_count += 1
            self._json(201, {
                "route_id": body.get("route_id") or f"{type(self).register_count:032x}",
                "generation": type(self).register_count,
                "lease_seconds": body["lease_seconds"],
                "removed": False,
            })
        elif self.path == "/agent/v1/renew":
            if type(self).renew_not_found_once:
                type(self).renew_not_found_once = False
                self._json(404, {
                    "code": "REGISTRATION_REJECTED",
                    "message": "local route was not found",
                })
            else:
                self._json(200, {
                    "route_id": body["route_id"],
                    "generation": 20,
                    "lease_seconds": body.get("lease_seconds", 1),
                    "removed": False,
                })
        elif self.path == "/agent/v1/unregister":
            self._json(200, {
                "route_id": body["route_id"],
                "generation": 21,
                "lease_seconds": 0,
                "removed": True,
            })
        else:
            self._json(404, {"code": "NOT_FOUND", "message": "not found"})

    def _json(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class SdkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProxyHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = NexusAgentClient(
            f"http://127.0.0.1:{cls.server.server_port}", token="test-token"
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        FakeProxyHandler.requests.clear()

    def test_registration_lifecycle(self):
        registration = CapabilityRegistration(
            intent="demo.echo",
            origin="agent://demo/echo",
            endpoint="http://127.0.0.1:9001/invoke",
            tenant="demo",
            lease_seconds=30,
        )
        lease = self.client.register(registration)
        self.assertEqual(lease.route_id, "0123456789abcdef0123456789abcdef")
        renewed = lease.renew(latency_ms=5, load_permille=10, healthy=True)
        self.assertEqual(renewed.generation, 8)
        lease.close()
        self.assertTrue(lease.info.removed)
        self.assertEqual(
            [request[0] for request in FakeProxyHandler.requests],
            ["/agent/v1/register", "/agent/v1/renew", "/agent/v1/unregister"],
        )
        self.assertEqual(
            FakeProxyHandler.requests[0][2]["Authorization"],
            "Bearer test-token",
        )

    def test_https_backend_identity_is_registered_and_renewed(self):
        identity = BackendTlsIdentity(
            address="192.168.10.20",
            port=9443,
            tls_server_name="linter.example.test",
            ca_bundle_id="system",
            certificate_sha256="ab" * 32,
        )
        registration = CapabilityRegistration(
            intent="chip.lint",
            origin="agent://demo/linter",
            endpoint="https://linter.example.test:9443/invoke",
            tenant="demo",
            lease_seconds=30,
            backend_tls=identity,
        )
        lease = self.client.register(registration)
        try:
            self.assertEqual(
                FakeProxyHandler.requests[0][1]["backend_tls"],
                identity.to_dict(),
            )
            lease.renew()
            self.assertEqual(
                FakeProxyHandler.requests[1][1]["backend_tls"],
                identity.to_dict(),
            )
            self.assertEqual(
                FakeProxyHandler.requests[1][1]["endpoint"],
                registration.endpoint,
            )
        finally:
            lease.close()

        with self.assertRaisesRegex(ValueError, "lowercase dotted DNS"):
            BackendTlsIdentity(
                address="192.168.10.20", port=9443,
                tls_server_name="NOT-A-DNS-NAME", certificate_sha256="ab" * 32,
            )

    def test_registration_can_request_router_managed_public_ipv6(self):
        registration = CapabilityRegistration(
            intent="demo.public-echo",
            origin="agent://demo/public-echo",
            endpoint="http://127.0.0.1:9002/invoke",
            tenant="demo",
            public_ipv6="auto",
        )
        lease = self.client.register(registration)
        self.assertEqual(lease.public_ipv6, "2001:db8:1234:5678::beef")
        self.assertIsInstance(lease.public_endpoint, PublicAgentEndpoint)
        self.assertEqual(lease.public_endpoint.url, "https://[2001:db8:1234:5678::beef]:7443")
        self.assertEqual(lease.public_endpoint.tls_server_name, "router-a.example.test")
        self.assertEqual(lease.public_endpoint.ca_bundle_id, "enterprise-agent-ca-v1")
        direct = lease.public_endpoint.connect(token="jwt", ca_file=None)
        self.assertEqual(direct.address, "2001:db8:1234:5678::beef")
        self.assertEqual(direct.server_identity, "router-a.example.test")
        self.assertEqual(FakeProxyHandler.requests[0][1]["public_ipv6"], "auto")
        lease.close()

        with self.assertRaisesRegex(ValueError, "public_ipv6"):
            CapabilityRegistration(
                intent="demo.bad",
                origin="agent://demo/bad",
                endpoint="http://127.0.0.1:9003/invoke",
                tenant="demo",
                public_ipv6="2001:db8::1",
            )

    def test_public_endpoint_round_trip_and_mismatch_rejection(self):
        endpoint = PublicAgentEndpoint.from_dict({
            "scheme": "https",
            "address": "2001:0db8::42",
            "port": 7443,
            "tls_server_name": "router-b.example.test",
            "ca_bundle_id": "enterprise_agent_ca_v2",
        })
        self.assertEqual(endpoint.address, "2001:db8::42")
        self.assertEqual(PublicAgentEndpoint.from_dict(endpoint.to_dict()), endpoint)
        plain = PublicAgentEndpoint.from_dict({
            "scheme": "http",
            "address": "2001:db8::43",
            "port": 7443,
        })
        self.assertEqual(plain.url, "http://[2001:db8::43]:7443")
        self.assertNotIn("tls_server_name", plain.to_dict())
        self.assertEqual(plain.connect(token="jwt").scheme, "http")
        with self.assertRaisesRegex(ValueError, "must not contain TLS"):
            PublicAgentEndpoint(
                scheme="http", address="2001:db8::44", port=7443,
                tls_server_name="router.example.test",
            )
        with self.assertRaisesRegex(ValueError, "invalid public Agent endpoint"):
            PublicAgentEndpoint.from_dict({"address": "192.0.2.1", "port": 7443})
        with self.assertRaisesRegex(Exception, "invalid lease response"):
            self.client._lease_info({
                "route_id": "0" * 32,
                "generation": 1,
                "lease_seconds": 30,
                "public_ipv6": "2001:db8::1",
                "public_endpoint": {
                    "address": "2001:db8::2",
                    "port": 7443,
                    "tls_server_name": "router.example.test",
                    "ca_bundle_id": "ca-v1",
                },
            })

    def test_invoke_intent_builds_envelope(self):
        response = self.client.invoke_intent(
            "demo.echo", {"value": 42}, tenant="demo",
            source_agent="agent://demo/caller",
            target_agent="agent://remote/echo-2",
            task_id="task-1"
        )
        self.assertEqual(response, {"echo": {"value": 42}})
        envelope = FakeProxyHandler.requests[0][1]
        self.assertEqual(envelope["intent"], "demo.echo")
        self.assertEqual(envelope["task_id"], "task-1")
        self.assertEqual(envelope["target_agent"], "agent://remote/echo-2")
        self.assertEqual(envelope["hop_limit"], 8)

    def test_invoke_rejects_empty_target_agent(self):
        with self.assertRaisesRegex(ValueError, "target_agent"):
            self.client.invoke_intent(
                "demo.echo", {}, tenant="demo",
                source_agent="agent://demo/caller", target_agent=""
            )
        self.assertEqual(FakeProxyHandler.requests, [])

    def test_structured_http_error(self):
        with self.assertRaises(NexusHttpError) as captured:
            self.client._post_json("/denied", {})
        self.assertEqual(captured.exception.status, 403)
        self.assertEqual(captured.exception.code, "INSUFFICIENT_SCOPE")

    def test_gateway_nested_structured_http_error(self):
        with self.assertRaises(NexusHttpError) as captured:
            self.client._post_json("/nested-denied", {})
        self.assertEqual(captured.exception.status, 401)
        self.assertEqual(captured.exception.code, "AUTHENTICATION_REQUIRED")

    def test_transaction_token_uses_separate_header(self):
        client = NexusAgentClient(
            f"http://127.0.0.1:{self.server.server_port}",
            transaction_token="one-task-token",
        )
        client.invoke_intent(
            "demo.echo", {"value": "one task"}, tenant="demo",
            source_agent="agent://demo/caller",
        )
        headers = FakeProxyHandler.requests[0][2]
        self.assertEqual(headers["Txn-Token"], "one-task-token")
        self.assertNotIn("Authorization", headers)

    def test_access_and_transaction_tokens_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            NexusAgentClient(
                "http://127.0.0.1:7788",
                token="access",
                transaction_token="transaction",
            )

    def test_tls_server_name_requires_https(self):
        with self.assertRaisesRegex(ValueError, "requires an https"):
            NexusAgentClient(
                "http://[2001:db8::1]:7443",
                tls_server_name="router.example.test",
            )

    def test_tls_connection_uses_separate_server_identity(self):
        class RecordingContext:
            check_hostname = True
            verify_mode = ssl.CERT_REQUIRED

            def __init__(self):
                self.server_hostname = None

            def wrap_socket(self, sock, *, server_hostname=None):
                self.server_hostname = server_hostname
                return sock

        context = RecordingContext()
        connection = _TlsServerNameHTTPSConnection(
            "[2001:db8::123]:7443",
            tls_server_name="router-b.example.test",
            context=context,
        )
        with mock.patch(
            "http.client.HTTPConnection.connect",
            lambda instance: setattr(instance, "sock", object()),
        ):
            connection.connect()
        self.assertEqual(context.server_hostname, "router-b.example.test")

    def test_direct_ipv6_target_builds_exact_proxy_free_invoke(self):
        target = DirectIPv6Agent(
            "[2001:db8:100::123]",
            server_identity="router-b.example.test",
            token="invoke-jwt",
        )
        target.client.invoke = mock.Mock(return_value={"ok": True})
        result = target.invoke(
            "demo.echo",
            {"value": 7},
            tenant="demo",
            source_agent="agent://demo/caller-a",
            task_id="direct-ipv6-1",
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(target.base_url, "https://[2001:db8:100::123]:7443")
        self.assertEqual(target.client.tls_server_name, "router-b.example.test")
        self.assertFalse(target.client.use_environment_proxy)
        envelope = target.client.invoke.call_args.args[0]
        self.assertEqual(envelope["intent"], "demo.echo")
        self.assertEqual(envelope["task_id"], "direct-ipv6-1")
        self.assertNotIn("target_agent", envelope)

    def test_direct_ipv6_target_rejects_non_ipv6_and_scoped_address(self):
        for address in ("192.0.2.10", "fe80::1%7", "not-an-address"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                DirectIPv6Agent(
                    address,
                    server_identity="router-b.example.test",
                )

    def test_direct_ipv6_plain_http_supports_no_jwt(self):
        target = DirectIPv6Agent.plain_http(
            "2001:db8:100::123"
        )
        target.client.invoke = mock.Mock(return_value={"ok": True})
        result = target.invoke(
            "demo.echo",
            {"cleartext": True},
            tenant="demo",
            source_agent="agent://demo/caller-a",
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(target.base_url, "http://[2001:db8:100::123]:7443")
        self.assertIsNone(target.server_identity)
        self.assertNotIn("Authorization", target.client._headers())
        self.assertNotIn("Txn-Token", target.client._headers())
        self.assertFalse(target.client.use_environment_proxy)

    def test_direct_ipv6_plain_http_rejects_tls_arguments(self):
        with self.assertRaisesRegex(ValueError, "does not accept TLS"):
            DirectIPv6Agent(
                "2001:db8::1",
                scheme="http",
                token="jwt",
                ca_file="ca.pem",
            )

    def test_client_automatically_resumes_after_clean_disconnect(self):
        ResumeProxyHandler.requests.clear()
        ResumeProxyHandler.executions = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), ResumeProxyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = NexusAgentClient(
                f"http://127.0.0.1:{server.server_port}", token="resume-jwt"
            )
            events = list(client.invoke_stream(
                {
                    "version": "1.0",
                    "intent": "demo.resume",
                    "intent_version": 1,
                    "task_id": "resume-client-1",
                    "source_agent": "agent://demo/caller",
                    "tenant": "demo",
                    "hop_limit": 8,
                    "payload": {"value": 9},
                },
                reconnect_delay=0,
            ))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual([event.event_id for event in events], ["1", "2", "3"])
        self.assertEqual(ResumeProxyHandler.executions, 1)
        self.assertEqual(len(ResumeProxyHandler.requests), 2)
        resumed_body, resumed_headers = ResumeProxyHandler.requests[1]
        self.assertEqual(resumed_body["resume_from_event_id"], 1)
        normalized_headers = {name.lower(): value for name, value in resumed_headers.items()}
        self.assertEqual(normalized_headers["last-event-id"], "1")

    def test_transaction_token_cannot_be_reused_for_stream_resume(self):
        client = NexusAgentClient(
            "http://127.0.0.1:7788", transaction_token="one-time"
        )
        with self.assertRaisesRegex(ValueError, "access JWT"):
            list(client.invoke_stream({"task_id": "task-transaction"}))

    def test_server_registration_context_closes_lease(self):
        server = NexusAgentServer("127.0.0.1", 0)
        registration = CapabilityRegistration(
            intent="demo.callable",
            origin="agent://demo/callable",
            endpoint=f"http://127.0.0.1:{server.port}/invoke",
            tenant="demo",
            lease_seconds=30,
        )
        with server.registered(self.client, [registration], auto_renew=False) as leases:
            self.assertEqual(len(leases), 1)
            self.assertFalse(leases[0].info.removed)
        self.assertTrue(leases[0].info.removed)
        server.server_close()
        self.assertEqual(
            [request[0] for request in FakeProxyHandler.requests],
            ["/agent/v1/register", "/agent/v1/unregister"],
        )


class LeaseSelfHealingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), SelfHealingProxyHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = NexusAgentClient(
            f"http://127.0.0.1:{cls.server.server_port}", token="lease-jwt"
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        SelfHealingProxyHandler.requests.clear()
        SelfHealingProxyHandler.register_count = 0
        SelfHealingProxyHandler.renew_not_found_once = False
        self.registration = CapabilityRegistration(
            intent="demo.self-heal",
            origin="agent://demo/self-heal",
            endpoint="http://127.0.0.1:9009/invoke",
            tenant="demo",
            lease_seconds=1,
        )

    @staticmethod
    def wait_until(predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_renew_404_reregisters_and_switches_route_id(self):
        lease = self.client.register(self.registration)
        old_route_id = lease.route_id
        SelfHealingProxyHandler.renew_not_found_once = True
        renewed = lease.renew(latency_ms=9, load_permille=17)
        self.assertNotEqual(renewed.route_id, old_route_id)
        self.assertEqual(lease.route_id, "00000000000000000000000000000002")
        self.assertEqual(lease.reregister_count, 1)
        self.assertEqual(
            [path for path, _body in SelfHealingProxyHandler.requests],
            ["/agent/v1/register", "/agent/v1/renew", "/agent/v1/register"],
        )
        replacement = SelfHealingProxyHandler.requests[-1][1]
        self.assertEqual(replacement["latency_ms"], 9)
        self.assertEqual(replacement["load_permille"], 17)
        lease.close()
        self.assertEqual(
            SelfHealingProxyHandler.requests[-1],
            ("/agent/v1/unregister", {"route_id": lease.route_id}),
        )

    def test_auto_renew_recovers_route_removed_by_router(self):
        SelfHealingProxyHandler.renew_not_found_once = True
        lease = self.client.register(
            self.registration,
            auto_renew=True,
            renew_fraction=0.2,
            health_check=lambda: True,
        )
        try:
            self.assertTrue(self.wait_until(lambda: lease.reregister_count == 1))
            self.assertEqual(lease.route_id, "00000000000000000000000000000002")
            self.assertIsNone(lease.last_error)
        finally:
            lease.close()

    def test_cloud_manifest_is_refreshed_on_every_lease_renewal(self):
        registration = CapabilityRegistration(
            intent="demo.cloud.repair",
            origin="agent://demo/cloud-repair",
            endpoint="http://127.0.0.1:9009/invoke",
            tenant="demo",
            lease_seconds=30,
            cloud=CloudRegistrationManifest(
                publish=True,
                agent_name="Cloud repair",
                tool=McpToolDescriptor(
                    name="repair",
                    description="Repair a Cloud manifest.",
                    input_schema={"type": "object"},
                ),
            ),
        )
        lease = self.client.register(registration)
        original_route_id = lease.route_id

        renewed = lease.renew(latency_ms=7)

        self.assertEqual(renewed.route_id, original_route_id)
        self.assertEqual(lease.manifest_refresh_count, 1)
        self.assertEqual(
            [path for path, _body in SelfHealingProxyHandler.requests],
            ["/agent/v1/register", "/agent/v1/register"],
        )
        refresh = SelfHealingProxyHandler.requests[-1][1]
        self.assertEqual(refresh["route_id"], original_route_id)
        self.assertEqual(refresh["latency_ms"], 7)
        self.assertEqual(refresh["cloud"]["tool"]["name"], "repair")
        lease.close()

    def test_unhealthy_route_is_not_reregistered(self):
        lease = self.client.register(self.registration)
        SelfHealingProxyHandler.renew_not_found_once = True
        try:
            with self.assertRaises(NexusHttpError) as captured:
                lease.renew(healthy=False)
            self.assertEqual(captured.exception.status, 404)
            self.assertEqual(lease.reregister_count, 0)
            self.assertEqual(SelfHealingProxyHandler.register_count, 1)
        finally:
            lease.close()

    def test_unhealthy_local_server_is_withdrawn_and_renewal_stops(self):
        lease = self.client.register(
            self.registration,
            auto_renew=True,
            renew_fraction=0.2,
            health_check=lambda: False,
        )
        try:
            self.assertTrue(self.wait_until(lambda: lease.health_check_failures == 1))
            unregisters = [
                body for path, body in SelfHealingProxyHandler.requests
                if path == "/agent/v1/unregister"
            ]
            self.assertEqual(len(unregisters), 1)
            self.assertTrue(lease.info.removed)
            request_count = len(SelfHealingProxyHandler.requests)
            time.sleep(1.3)
            self.assertEqual(request_count, len(SelfHealingProxyHandler.requests))
            self.assertIsNotNone(lease.last_error)
        finally:
            lease.close()
        self.assertEqual(request_count, len(SelfHealingProxyHandler.requests))


class AgentServerTest(unittest.TestCase):
    def setUp(self):
        self.server = NexusAgentServer("127.0.0.1", 0)
        self.resume_handler_calls = 0

        @self.server.handler("demo.echo")
        def echo(envelope):
            return {
                "payload": envelope.payload,
                "route_id": envelope.route_id,
                "protocol": envelope.protocol,
                "selector": envelope.selector,
                "target_agent": envelope.target_agent,
            }

        @self.server.handler("demo.reject")
        def reject(_envelope):
            raise AgentRequestError(422, "INPUT_REJECTED", "request was rejected")

        @self.server.handler("demo.workspace-failed")
        def workspace_failed(_envelope):
            raise NexusComputerError("Nexus run endpoint returned HTTP 404")

        @self.server.handler("demo.browser-failed")
        def browser_failed(_envelope):
            raise NexusBrowserActionFailed(
                "Browser navigation requires an HTTP or HTTPS URL"
            )

        @self.server.stream_handler("demo.stream")
        def stream(envelope):
            yield SseEvent(data="starting", event="progress", event_id="1")
            yield {"done": True, "payload": envelope.payload}

        @self.server.stream_handler("demo.resume")
        def resume_stream(_envelope):
            self.resume_handler_calls += 1
            yield SseEvent(data="one", event="progress")
            time.sleep(0.08)
            yield SseEvent(data="two", event="progress")
            time.sleep(0.08)
            yield SseEvent(data='{"ok":true}', event="result")

        self.thread = self.server.serve_in_thread()
        self.url = f"http://127.0.0.1:{self.server.port}/invoke"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @staticmethod
    def envelope(intent, payload, *, target_agent=None):
        result = {
            "version": "1.0",
            "intent": intent,
            "intent_version": 1,
            "task_id": "task-server-1",
            "source_agent": "agent://demo/caller",
            "tenant": "demo",
            "hop_limit": 7,
            "constraints": {"region": "local"},
            "payload": payload,
        }
        if target_agent is not None:
            result["target_agent"] = target_agent
        return result

    def post(self, intent, payload, *, accept="application/json", target_agent=None):
        request = urllib.request.Request(
            self.url,
            data=json.dumps(self.envelope(
                intent, payload, target_agent=target_agent
            )).encode("utf-8"),
            headers={
                "Content-Type": "application/vnd.nexus.agent-envelope+json",
                "Accept": accept,
                "X-Nexus-Route-Id": "0123456789abcdef0123456789abcdef",
            },
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=2)

    def test_sync_handler_and_protocol_payload(self):
        payload = {
            "protocol": "mcp",
            "selector": "echo",
            "request": {"jsonrpc": "2.0", "id": 1},
        }
        with self.post(
            "demo.echo", payload, target_agent="agent://demo/echo-1"
        ) as response:
            result = json.loads(response.read())
        self.assertEqual(result["payload"], payload)
        self.assertEqual(result["protocol"], "mcp")
        self.assertEqual(result["selector"], "echo")
        self.assertEqual(result["route_id"], "0123456789abcdef0123456789abcdef")
        self.assertEqual(result["target_agent"], "agent://demo/echo-1")

    def test_health_tracks_listener_lifecycle(self):
        self.assertTrue(self.server.is_healthy())
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.assertFalse(self.server.is_healthy())

    def test_sse_handler_flushes_protocol_events(self):
        with self.post("demo.stream", {"value": 42}, accept="text/event-stream") as response:
            self.assertEqual(response.headers.get_content_type(), "text/event-stream")
            body = response.read().decode("utf-8")
        self.assertIn("event: progress\nid: 1\ndata: starting\n\n", body)
        self.assertIn('data: {"done":true,"payload":{"value":42}}\n\n', body)
        self.assertIn(": nexus-stream-complete\n\n", body)

    def test_stream_disconnect_resumes_without_reexecuting_handler(self):
        first_request = urllib.request.Request(
            self.url,
            data=json.dumps(self.envelope("demo.resume", {"value": 7})).encode(),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            method="POST",
        )
        with urllib.request.urlopen(first_request, timeout=2) as response:
            first_event = b""
            while not first_event.endswith(b"\n\n"):
                first_event += response.readline()
        self.assertIn(b"id: 1\n", first_event)
        time.sleep(0.2)

        resumed = self.envelope("demo.resume", {"value": 7})
        resumed["resume_from_event_id"] = 1
        second_request = urllib.request.Request(
            self.url,
            data=json.dumps(resumed).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Last-Event-ID": "1",
            },
            method="POST",
        )
        with urllib.request.urlopen(second_request, timeout=2) as response:
            replay = response.read().decode()
        self.assertNotIn("id: 1\n", replay)
        self.assertIn("id: 2\ndata: two", replay)
        self.assertIn("id: 3\ndata: {\"ok\":true}", replay)
        self.assertIn(": nexus-stream-complete", replay)
        self.assertEqual(self.resume_handler_calls, 1)

    def test_resume_rejects_task_id_reuse_with_changed_payload(self):
        with self.post("demo.stream", {"value": 1}, accept="text/event-stream") as response:
            response.read()
        conflict = self.envelope("demo.stream", {"value": 2})
        conflict["resume_from_event_id"] = 1
        request = urllib.request.Request(
            self.url,
            data=json.dumps(conflict).encode(),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(captured.exception.code, 409)
        body = json.loads(captured.exception.read())
        captured.exception.close()
        self.assertEqual(body["code"], "STREAM_TASK_CONFLICT")

    def test_application_error_is_structured(self):
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.post("demo.reject", {}).read()
        self.assertEqual(captured.exception.code, 422)
        result = json.loads(captured.exception.read())
        captured.exception.close()
        self.assertEqual(result, {
            "code": "INPUT_REJECTED",
            "message": "request was rejected",
        })

    def test_managed_workspace_error_is_safe_and_actionable(self):
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.post("demo.workspace-failed", {}).read()
        self.assertEqual(captured.exception.code, 502)
        result = json.loads(captured.exception.read())
        captured.exception.close()
        self.assertEqual(result, {
            "code": "WORKSPACE_UNAVAILABLE",
            "message": "Nexus run endpoint returned HTTP 404",
        })

    def test_managed_browser_error_is_safe_and_actionable(self):
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.post("demo.browser-failed", {}).read()
        self.assertEqual(captured.exception.code, 502)
        result = json.loads(captured.exception.read())
        captured.exception.close()
        self.assertEqual(result, {
            "code": "BROWSER_ACTION_FAILED",
            "message": "Browser navigation requires an HTTP or HTTPS URL",
        })

    def test_unknown_intent_is_not_dispatched(self):
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.post("demo.missing", {}).read()
        self.assertEqual(captured.exception.code, 404)
        captured.exception.close()

    def test_empty_target_agent_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.post("demo.echo", {}, target_agent="").read()
        self.assertEqual(captured.exception.code, 400)
        result = json.loads(captured.exception.read())
        captured.exception.close()
        self.assertEqual(result["code"], "INVALID_ENVELOPE")


if __name__ == "__main__":
    unittest.main()
