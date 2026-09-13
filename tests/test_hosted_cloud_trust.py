"""Real TLS callback and real FastMCP error-envelope regressions."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nexus_agent import NexusAgent, NexusRunContext
from nexus_agent.browser import NexusBrowserSessionLost
from nexus_agent.hosted_trust import TRUST_ENV, HostedCloudTrustError, hosted_cloud_opener


class HostedCloudTrustTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        cls.temp = tempfile.TemporaryDirectory(prefix="nexus-hosted-tls-")
        root = Path(cls.temp.name)
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Hosted test CA")])
        now = datetime.now(timezone.utc)
        def builder(subject, issuer, public_key):
            return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(public_key)
                    .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
                    .not_valid_after(now + timedelta(days=1)))
        ca = (builder(name, name, key.public_key()).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False).sign(key, hashes.SHA256()))
        cls.ca_pem = ca.public_bytes(serialization.Encoding.PEM).decode()
        leaf_key = ec.generate_private_key(ec.SECP256R1())
        leaf = (builder(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]), name, leaf_key.public_key())
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
                .sign(key, hashes.SHA256()))
        (root / "server.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        (root / "server.key").write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        cls.requests = []
        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.do_GET()
            def do_GET(self):
                cls.requests.append((self.command, self.path))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", cls.origin + "/target")
                    self.end_headers()
                    return
                self.send_response(404 if self.path == "/missing" else 200)
                self.send_header("Content-Type", "application/json")
                body = b'{"ok":true,"data":{"accepted":true}}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(root / "server.pem", root / "server.key")
        cls.server.socket = tls.wrap_socket(cls.server.socket, server_side=True)
        cls.origin = f"https://localhost:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def policy(self, **overrides):
        return {"schema_version": 1, "mode": "pinned-pem", "origins": [self.origin],
                "ca_pem": self.ca_pem, "sha256": hashlib.sha256(self.ca_pem.encode()).hexdigest(), **overrides}

    def context(self, **overrides):
        return NexusRunContext.from_env(environ={TRUST_ENV: json.dumps(self.policy(**overrides)),
                    "NEXUS_BROWSER_ENABLED": "true", "NEXUS_BROWSER_DELEGATE_URL": self.origin + "/browser",
                    "NEXUS_BROWSER_DELEGATE_TOKEN": "unit-test-token"})

    def test_private_ca_callback_fails_before_trust_and_passes_without_ca_files(self):
        # This is the deployed defect: a default context cannot verify this CA.
        with self.assertRaises(URLError):
            NexusRunContext.from_env(environ={})._open_cloud(Request(self.origin + "/probe"), timeout=2)
        with patch("builtins.open", side_effect=AssertionError("trust must stay in memory")):
            context = self.context()
            self.assertTrue(context._browser_request_raw("observe", {})["accepted"])
            for path in ("plan", "chat", "workspace", "files", "terminal", "mobile", "events"):
                with context._open_cloud(Request(self.origin + "/" + path), timeout=2) as response:
                    self.assertEqual(response.status, 200)
            context.close()

    def test_wrong_ca_and_hostname_remain_rejected(self):
        for context, target in ((self.context(mode="system", ca_pem="", sha256=""), self.origin),
                               (self.context(origins=[self.origin.replace("localhost", "127.0.0.1")]), self.origin.replace("localhost", "127.0.0.1"))):
            with self.assertRaises(HostedCloudTrustError) as failure:
                context._open_cloud(Request(target + "/probe"), timeout=2)
            self.assertEqual(failure.exception.code, "RUN_CONTEXT_TLS_FAILED")
            context.close()

    def test_origin_escape_http_and_redirect_never_send_delegate_to_target(self):
        context = self.context()
        before = len(self.requests)
        for target in ("http://localhost:1/", "https://other.invalid/", self.origin + "@evil.invalid/"):
            with self.assertRaises(HostedCloudTrustError) as failure:
                context._open_cloud(Request(target), timeout=2)
            self.assertEqual(failure.exception.code, "RUN_CONTEXT_ORIGIN_MISMATCH")
        self.assertEqual(len(self.requests), before)
        with self.assertRaises(HostedCloudTrustError):
            context._open_cloud(Request(self.origin + "/redirect", headers={"X-Nexus-Browser-Delegate-Token": "test"}), timeout=2)
        self.assertNotIn(("GET", "/target"), self.requests[before:])
        context.close()

    def test_missing_endpoint_is_not_retried(self):
        context = self.context()
        before = len(self.requests)
        with self.assertRaises(HTTPError):
            context._open_cloud(Request(self.origin + "/missing", data=b"{}"), timeout=2)
        self.assertEqual(self.requests[before:], [("POST", "/missing")])
        context.close()

    def test_untrusted_headers_cannot_install_ca_and_invalid_policy_fails_closed(self):
        self.assertIsNone(NexusRunContext.from_env(environ={}, headers={TRUST_ENV: json.dumps(self.policy())})._cloud_opener)
        for value in ("{", json.dumps(self.policy(sha256="0" * 64)), json.dumps(self.policy(origins=[])),
                      json.dumps(self.policy(ca_pem=self.ca_pem + "PRIVATE KEY")), " " * 24577):
            with self.assertRaises(HostedCloudTrustError) as failure:
                hosted_cloud_opener({TRUST_ENV: value})
            self.assertEqual(failure.exception.code, "RUN_CONTEXT_TRUST_UNAVAILABLE")

    def test_real_mcp_retains_typed_failure_without_secret_prose(self):
        from fastmcp import Client
        agent = NexusAgent(runtime="hosted")
        @agent.capability("fail")
        def fail(payload):
            if payload.get("browser"):
                raise NexusBrowserSessionLost("secret browser data")
            raise RuntimeError("Authorization: Bearer secret")
        async def run():
            async with Client(agent.as_mcp_server().fastmcp) as client:
                for arguments, code in (({"browser": True}, "BROWSER_SESSION_LOST"), ({}, "HANDLER_FAILED")):
                    result = await client.call_tool("fail", arguments, raise_on_error=False)
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.meta["nexus"]["failure"]["code"], code)
                    self.assertNotIn("secret", str(result))
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
