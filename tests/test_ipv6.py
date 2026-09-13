import json
import pathlib
import socket
import sys
import unittest
import urllib.request

SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent import NexusAgentServer  # noqa: E402


class IPv6ServerRuntimeTest(unittest.TestCase):
    def setUp(self):
        try:
            self.server = NexusAgentServer(
                "::1",
                0,
                address_family="ipv6",
                dual_stack=False,
            )
        except OSError as exc:
            self.skipTest(f"IPv6 loopback is unavailable: {exc}")

        @self.server.handler("demo.ipv6")
        def echo(envelope):
            return {"ok": True, "payload": envelope.payload}

        self.thread = self.server.serve_in_thread()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        if not hasattr(self, "server"):
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_real_ipv6_http_invoke(self):
        envelope = {
            "version": "1.0",
            "intent": "demo.ipv6",
            "intent_version": 1,
            "task_id": "ipv6-runtime-1",
            "source_agent": "agent://demo/caller",
            "tenant": "demo",
            "hop_limit": 8,
            "constraints": {},
            "payload": {"transport": "ipv6"},
        }
        request = urllib.request.Request(
            f"http://[::1]:{self.server.port}/invoke",
            data=json.dumps(envelope).encode(),
            headers={
                "Content-Type": "application/vnd.nexus.agent-envelope+json",
                "Accept": "application/json",
                "X-Nexus-Route-Id": "0123456789abcdef0123456789abcdef",
            },
            method="POST",
        )
        with self.opener.open(request, timeout=2) as response:
            result = json.load(response)
        self.assertEqual(result, {"ok": True, "payload": {"transport": "ipv6"}})
        self.assertEqual(self.server.address_family, "ipv6")
        if hasattr(socket, "IPV6_V6ONLY"):
            value = self.server._httpd.socket.getsockopt(
                socket.IPPROTO_IPV6, socket.IPV6_V6ONLY
            )
            self.assertEqual(value, 1)

    def test_auto_family_selects_ipv6(self):
        server = NexusAgentServer("::1", 0)
        try:
            self.assertEqual(server.address_family, "ipv6")
        finally:
            server.server_close()

    def test_invalid_family_options_fail_before_bind(self):
        with self.assertRaisesRegex(ValueError, "address_family"):
            NexusAgentServer("127.0.0.1", 0, address_family="unix")
        with self.assertRaisesRegex(ValueError, "dual_stack"):
            NexusAgentServer(
                "127.0.0.1", 0, address_family="ipv4", dual_stack=True
            )


if __name__ == "__main__":
    unittest.main()
