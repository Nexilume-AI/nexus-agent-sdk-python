import json
import os
import pathlib
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent import (  # noqa: E402
    DirectIPv6Agent,
    NexusAgent,
    NexusAgentClient,
    NexusAgentError,
)


class _IPv6HttpServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


class _InvokeHandler(BaseHTTPRequestHandler):
    peer_certificate = None

    def log_message(self, *_args):
        return

    def do_POST(self):
        if self.path != "/agent/v1/invoke":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        envelope = json.loads(self.rfile.read(length))
        type(self).peer_certificate = (
            self.connection.getpeercert()
            if hasattr(self.connection, "getpeercert") else None
        )
        raw = json.dumps({
            "ok": True,
            "intent": envelope["intent"],
            "payload": envelope["payload"],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class DirectIPv6TlsRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        candidates = []
        if sys.platform == "win32":
            candidates.append(pathlib.Path(
                "C:/Program Files/Git/usr/bin/openssl.exe"
            ))
        discovered = shutil.which("openssl")
        if discovered:
            candidates.append(pathlib.Path(discovered))
        openssl = next((str(path) for path in candidates if path.is_file()), None)
        if openssl is None:
            raise unittest.SkipTest("OpenSSL CLI is unavailable")
        cls.temp = tempfile.TemporaryDirectory(prefix="nexus-direct-ipv6-")
        cls.root = pathlib.Path(cls.temp.name)

        def run(*arguments):
            subprocess.run(
                [openssl, *arguments],
                cwd=cls.root,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        (cls.root / "ca.cnf").write_text(
            "[req]\n"
            "distinguished_name=dn\n"
            "prompt=no\n"
            "x509_extensions=v3_ca\n"
            "[dn]\n"
            "CN=Nexus Direct IPv6 Test CA\n"
            "[v3_ca]\n"
            "basicConstraints=critical,CA:TRUE\n"
            "keyUsage=critical,keyCertSign,cRLSign\n"
            "subjectKeyIdentifier=hash\n"
            "authorityKeyIdentifier=keyid:always\n",
            encoding="ascii",
        )
        run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
            "-days", "1", "-config", "ca.cnf", "-keyout", "ca.key",
            "-out", "ca.crt")

        run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256",
            "-subj", "/CN=router-direct.test", "-keyout", "server.key",
            "-out", "server.csr")
        (cls.root / "server.ext").write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectKeyIdentifier=hash\n"
            "authorityKeyIdentifier=keyid,issuer\n"
            "subjectAltName=DNS:router-direct.test\n",
            encoding="ascii",
        )
        run("x509", "-req", "-sha256", "-days", "1", "-in", "server.csr",
            "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
            "-extfile", "server.ext", "-out", "server.crt")

        run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256",
            "-subj", "/CN=agent-direct-caller", "-keyout", "client.key",
            "-out", "client.csr")
        (cls.root / "client.ext").write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=clientAuth\n"
            "subjectKeyIdentifier=hash\n"
            "authorityKeyIdentifier=keyid,issuer\n",
            encoding="ascii",
        )
        run("x509", "-req", "-sha256", "-days", "1", "-in", "client.csr",
            "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
            "-extfile", "client.ext", "-out", "client.crt")

        try:
            cls.server = _IPv6HttpServer(("::1", 0), _InvokeHandler)
        except OSError as exc:
            cls.temp.cleanup()
            raise unittest.SkipTest(f"IPv6 loopback is unavailable: {exc}")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.root / "server.crt", cls.root / "server.key")
        context.load_verify_locations(cafile=cls.root / "ca.crt")
        context.verify_mode = ssl.CERT_REQUIRED
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join(timeout=2)
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    def test_literal_ipv6_with_stable_tls_identity_and_mtls(self):
        target = DirectIPv6Agent(
            "::1",
            port=self.server.server_port,
            server_identity="router-direct.test",
            token="test-jwt",
            ca_file=str(self.root / "ca.crt"),
            cert_file=str(self.root / "client.crt"),
            key_file=str(self.root / "client.key"),
        )
        result = target.invoke(
            "demo.direct-ipv6",
            {"transport": "ipv6", "discovery": False},
            tenant="demo",
            source_agent="agent://demo/direct-caller",
            task_id="direct-ipv6-tls-1",
        )
        self.assertEqual(result["intent"], "demo.direct-ipv6")
        self.assertEqual(result["payload"]["transport"], "ipv6")
        self.assertIsNotNone(_InvokeHandler.peer_certificate)

    def test_callable_agent_derives_registration_identity_from_certificate(self):
        agent = NexusAgent(
            router="http://127.0.0.1:7788",
            auth="none",
            tenant="demo",
            agent_id="https-linter",
            listen_host="127.0.0.1",
            advertise_address="192.0.2.20",
            cert_file=str(self.root / "server.crt"),
            key_file=str(self.root / "server.key"),
            server_ca_bundle_id="direct-test-ca",
        )
        try:
            @agent.capability("demo.https-lint", public_ipv6=False)
            def lint(payload):
                return payload

            registration = agent.registrations()[0]
            self.assertEqual(
                registration.endpoint,
                f"https://router-direct.test:{agent.server.port}/invoke",
            )
            self.assertEqual(registration.backend_tls.address, "192.0.2.20")
            self.assertEqual(registration.backend_tls.port, agent.server.port)
            self.assertEqual(
                registration.backend_tls.tls_server_name,
                "router-direct.test",
            )
            self.assertEqual(
                registration.backend_tls.ca_bundle_id, "direct-test-ca"
            )
            self.assertEqual(len(registration.backend_tls.certificate_sha256), 64)
        finally:
            agent.server.server_close()

    def test_address_and_jwt_only_uses_installed_security_profile(self):
        profile_file = self.root / "security.json"
        profile_file.write_text(json.dumps({
            "version": 1,
            "caller_identity": {
                "cert_file": "client.crt",
                "key_file": "client.key",
            },
            "trust_bundles": {
                "direct-test-ca": {"ca_file": "ca.crt"},
            },
            "routes": [{
                "ipv6_prefix": "::1/128",
                "port": self.server.server_port,
                "tls_server_name": "router-direct.test",
                "ca_bundle_id": "direct-test-ca",
            }],
        }), encoding="utf-8")
        with mock.patch.dict(
            os.environ, {"NEXUS_AGENT_SECURITY_PROFILE": str(profile_file)}
        ):
            target = DirectIPv6Agent("::1", token="test-jwt")
            result = target.invoke(
                "demo.direct-ipv6",
                {"security_arguments": "profile"},
                tenant="demo",
                source_agent="agent://demo/direct-caller",
                task_id="direct-ipv6-profile-1",
            )
        self.assertEqual(result["payload"]["security_arguments"], "profile")
        self.assertEqual(target.server_identity, "router-direct.test")
        self.assertIsNotNone(_InvokeHandler.peer_certificate)

    def test_same_literal_ip_without_identity_override_fails_hostname_check(self):
        client = NexusAgentClient(
            f"https://[::1]:{self.server.server_port}",
            ca_file=str(self.root / "ca.crt"),
            cert_file=str(self.root / "client.crt"),
            key_file=str(self.root / "client.key"),
            use_environment_proxy=False,
        )
        with self.assertRaises(NexusAgentError):
            client.invoke_intent(
                "demo.direct-ipv6",
                {},
                tenant="demo",
                source_agent="agent://demo/direct-caller",
            )

    def test_missing_client_certificate_is_reported_as_sdk_error(self):
        target = DirectIPv6Agent(
            "::1",
            port=self.server.server_port,
            server_identity="router-direct.test",
            token="test-jwt",
            ca_file=str(self.root / "ca.crt"),
        )
        with self.assertRaises(NexusAgentError):
            target.invoke(
                "demo.direct-ipv6",
                {},
                tenant="demo",
                source_agent="agent://demo/direct-caller",
            )

    def test_explicit_plain_http_uses_real_ipv6_without_tls_profile(self):
        server = _IPv6HttpServer(("::1", 0), _InvokeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.dict(
                os.environ,
                {"NEXUS_AGENT_SECURITY_PROFILE": str(self.root / "missing.json")},
            ):
                target = DirectIPv6Agent.plain_http(
                    "::1", port=server.server_port, token="test-jwt"
                )
                result = target.invoke(
                    "demo.direct-ipv6",
                    {"transport": "plain-http"},
                    tenant="demo",
                    source_agent="agent://demo/direct-caller",
                )
            self.assertEqual(result["payload"]["transport"], "plain-http")
            self.assertTrue(target.base_url.startswith("http://[::1]:"))
            self.assertIsNone(target.client.ssl_context)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
