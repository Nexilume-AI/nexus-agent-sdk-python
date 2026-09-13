import json
import hashlib
import pathlib
import ssl
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent import (  # noqa: E402
    AutoTokenProvider,
    CapabilityRegistration,
    NexusAgentClient,
    NexusAuthenticationError,
    NexusAuthorizationError,
    NexusTokenAcquisitionError,
    OIDCClientCredentialsProvider,
    RouterAuthMetadata,
    discover_router_auth,
)
from nexus_agent.auth import (  # noqa: E402
    CloudTrustOriginError,
    CloudTrustPolicy,
    RouterLanSessionProvider,
    resolve_environment_token,
)


TEST_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIBljCCATugAwIBAgIUAxioQnHAakCZ5ED/vYYYTle/Gj0wCgYIKoZIzj0EAwIw
JzElMCMGA1UEAwwcTmV4dXMgTG9jYWwgQ2xvdWQgV2Vic2l0ZSBDQTAeFw0yNjA4
MTQwNTA0NTFaFw0zNjA4MTEwNTE0NTFaMCcxJTAjBgNVBAMMHE5leHVzIExvY2Fs
IENsb3VkIFdlYnNpdGUgQ0EwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAAT6ODIA
1F/PMjmQIm7NfDnggdJY7pMtCkFWVWpow01ZW54aLtWcjTFsPrAINOVw8QjLYLwZ
8E3oc1L7oZfV6rmNo0UwQzASBgNVHRMBAf8ECDAGAQH/AgEAMB0GA1UdDgQWBBS4
YrX4y4v5Mc1vUR5Tldb87aiwDTAOBgNVHQ8BAf8EBAMCAYYwCgYIKoZIzj0EAwID
SQAwRgIhAIHPdBJMy6M3GMJTizywd5LxrbGQSiVxc102P4g6Wg54AiEAxONPUoW1
SNHTbh3+UfQsWNgCM5EBm3MUnIK3FFuY4N8=
-----END CERTIFICATE-----
"""


class AuthFixture(BaseHTTPRequestHandler):
    token_requests = []
    token_count = 0
    protected_count = 0
    bootstrap_count = 0
    bootstrap_requests = []
    reject_first_lan_session = False

    def log_message(self, *_args):
        pass

    def _json(self, status, value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/noauth/agent/v1/authentication":
            self._json(200, {
                "schema_version": 1,
                "authentication": {"required": False, "type": "none"},
                "required_scopes": {},
            })
        elif self.path == "/lan/agent/v1/authentication":
            self._json(200, {
                "schema_version": 2,
                "authentication": {
                    "required": True,
                    "type": "oauth2",
                    "issuer": "https://id.example.test",
                    "audience": "nexus-agent-router",
                },
                "required_scopes": {
                    "invoke": ["agent.route", "agent.invoke"],
                    "register": ["agent.register"],
                },
                "bootstrap": {
                    "type": "trusted-lan",
                    "endpoint": "/agent/v1/bootstrap",
                    "token_ttl_seconds": 300,
                },
                "cloud": {
                    "connector_enabled": True,
                    "enrolled": True,
                    "status_endpoint": "/agent/v1/cloud-registration",
                    "manifest_schema_version": 2,
                    "run_context_trust": {
                        "delivery": "bootstrap-response",
                    },
                    "transports": {
                        "direct_ipv6": True,
                        "relay": True,
                        "auto": True,
                    },
                },
            })
        elif self.path == "/agent/v1/authentication":
            self._json(200, {
                "schema_version": 2,
                "authentication": {
                    "required": True,
                    "type": "oauth2",
                    "issuer": "https://id.example.test",
                    "audience": "nexus-agent-router",
                },
                "required_scopes": {
                    "invoke": ["agent.route", "agent.invoke"],
                    "register": ["agent.register"],
                },
            })
        elif self.path == "/issuer/.well-known/openid-configuration":
            self._json(200, {
                "issuer": f"http://127.0.0.1:{self.server.server_port}/issuer",
                "token_endpoint": f"http://127.0.0.1:{self.server.server_port}/token",
            })
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if self.path == "/token":
            type(self).token_count += 1
            type(self).token_requests.append((
                urllib.parse.parse_qs(raw.decode()),
                dict(self.headers),
            ))
            self._json(200, {
                "access_token": f"access-{type(self).token_count}",
                "token_type": "Bearer",
                "expires_in": 120,
            })
        elif self.path == "/agent/v1/bootstrap":
            request = json.loads(raw)
            type(self).bootstrap_count += 1
            type(self).bootstrap_requests.append((request, dict(self.headers)))
            self._json(200, {
                "schema_version": 2,
                "access_token": f"lan-session-{type(self).bootstrap_count}",
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": "agent.register agent.route agent.invoke",
                "identity": {
                    "tenant": request["tenant"],
                    "origin": request["origin"],
                },
                "cloud_trust": {
                    "mode": "system",
                    "origin": "https://cloud.example.test",
                },
            })
        elif self.path == "/lan/agent/v1/register":
            if not self.headers.get("Authorization", "").startswith("Bearer lan-session-"):
                self._json(401, {"error": "missing LAN session"})
            else:
                self._json(200, {
                    "route_id": "route-lan-1",
                    "generation": 1,
                    "lease_seconds": 300,
                })
        elif self.path == "/lan/agent/v1/invoke":
            authorization = self.headers.get("Authorization")
            if type(self).reject_first_lan_session and authorization == "Bearer lan-session-1":
                self._json(401, {"error": {
                    "code": "AUTHENTICATION_REQUIRED",
                    "message": "LAN session expired",
                }})
            elif authorization and authorization.startswith("Bearer lan-session-"):
                self._json(200, {"ok": True, "authorization": authorization})
            else:
                self._json(401, {"error": "missing LAN session"})
        elif self.path == "/noauth/agent/v1/invoke":
            self._json(200, {
                "ok": True,
                "authorization_seen": "Authorization" in self.headers,
                "transaction_token_seen": "Txn-Token" in self.headers,
            })
        elif self.path == "/agent/v1/invoke":
            type(self).protected_count += 1
            if self.headers.get("Authorization") == "Bearer stale-token":
                self._json(401, {"error": {
                    "code": "AUTHENTICATION_REQUIRED",
                    "message": "token expired",
                }})
            elif self.headers.get("Authorization") == "Bearer fresh-token":
                self._json(200, {"ok": True})
            else:
                self._json(403, {"error": {
                    "code": "INSUFFICIENT_SCOPE",
                    "message": "scope denied",
                }})
        else:
            self._json(404, {"error": "not_found"})


class RefreshProvider:
    refreshable = True

    def __init__(self):
        self.refreshes = 0

    def get_token(self, *, force_refresh=False):
        if force_refresh:
            self.refreshes += 1
            return "fresh-token"
        return "stale-token"


class AuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AuthFixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        AuthFixture.token_requests.clear()
        AuthFixture.token_count = 0
        AuthFixture.protected_count = 0
        AuthFixture.bootstrap_count = 0
        AuthFixture.bootstrap_requests.clear()
        AuthFixture.reject_first_lan_session = False

    def test_canonical_environment_token_and_legacy_warning(self):
        self.assertEqual(
            resolve_environment_token({"NEXUS_AGENT_TOKEN": "canonical"}),
            "canonical",
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            token = resolve_environment_token({"NEXUS_JWT": "legacy"})
        self.assertEqual(token, "legacy")
        self.assertTrue(any(item.category is DeprecationWarning for item in caught))
        with self.assertRaises(NexusTokenAcquisitionError):
            resolve_environment_token({
                "NEXUS_AGENT_TOKEN": "one",
                "NEXUS_TOKEN": "two",
            })

    def test_router_auth_metadata_discovery(self):
        metadata = discover_router_auth(self.base_url)
        self.assertTrue(metadata.required)
        self.assertEqual(metadata.issuer, "https://id.example.test")
        self.assertEqual(metadata.audience, "nexus-agent-router")
        self.assertEqual(
            metadata.required_scopes["invoke"],
            ("agent.route", "agent.invoke"),
        )

    def test_router_auth_metadata_v2_discovers_trusted_lan_bootstrap(self):
        metadata = discover_router_auth(self.base_url + "/lan")
        self.assertEqual(metadata.schema_version, 2)
        self.assertIsNotNone(metadata.bootstrap)
        self.assertEqual(metadata.bootstrap.endpoint, "/agent/v1/bootstrap")
        self.assertEqual(metadata.bootstrap.token_ttl_seconds, 300)
        self.assertIsNotNone(metadata.cloud)
        self.assertTrue(metadata.cloud.connector_enabled)
        self.assertTrue(metadata.cloud.enrolled)
        self.assertEqual(
            metadata.cloud.status_endpoint, "/agent/v1/cloud-registration"
        )
        self.assertTrue(metadata.cloud.transports.direct_ipv6)
        self.assertTrue(metadata.cloud.transports.relay)
        self.assertEqual(metadata.cloud.manifest_schema_version, 2)
        self.assertEqual(
            metadata.cloud.run_context_trust_delivery,
            "bootstrap-response",
        )
        invalid = {
            "schema_version": 2,
            "authentication": {"required": False, "type": "none"},
            "required_scopes": {},
            "bootstrap": {
                "type": "trusted-lan",
                "endpoint": "https://evil.example/bootstrap",
                "token_ttl_seconds": 300,
            },
        }
        with self.assertRaises(Exception):
            RouterAuthMetadata.from_dict(invalid)

    def test_oidc_client_credentials_cache_and_force_refresh(self):
        provider = OIDCClientCredentialsProvider(
            issuer=self.base_url + "/issuer",
            client_id="caller-01",
            client_secret="secret",
            audience="nexus-agent-router",
            scopes=("agent.route", "agent.invoke"),
            allow_insecure_http=True,
        )
        self.assertEqual(provider.get_token(), "access-1")
        self.assertEqual(provider.get_token(), "access-1")
        self.assertEqual(provider.get_token(force_refresh=True), "access-2")
        self.assertEqual(AuthFixture.token_count, 2)
        fields, headers = AuthFixture.token_requests[0]
        self.assertEqual(fields["grant_type"], ["client_credentials"])
        self.assertEqual(fields["audience"], ["nexus-agent-router"])
        self.assertEqual(fields["scope"], ["agent.route agent.invoke"])
        self.assertTrue(headers["Authorization"].startswith("Basic "))

    def test_pinned_cloud_trust_is_validated_and_kept_in_memory(self):
        digest = hashlib.sha256(TEST_CA_PEM.encode("ascii")).hexdigest()
        policy = RouterLanSessionProvider._parse_cloud_trust({
            "mode": "pinned-pem",
            "origin": "https://cloud.example.test",
            "sha256": digest,
            "ca_pem": TEST_CA_PEM,
        })
        self.assertIsInstance(policy, CloudTrustPolicy)
        self.assertEqual(policy.mode, "pinned-pem")
        policy.validate_exchange_url(
            "https://cloud.example.test/api/v1/internal/run-context/exchange/"
        )
        with self.assertRaises(CloudTrustOriginError):
            policy.validate_exchange_url(
                "https://other.example.test/api/v1/internal/run-context/exchange/"
            )
        with self.assertRaises(NexusTokenAcquisitionError):
            RouterLanSessionProvider._parse_cloud_trust({
                "mode": "pinned-pem",
                "origin": "https://cloud.example.test",
                "sha256": "0" * 64,
                "ca_pem": TEST_CA_PEM,
            })
        with self.assertRaises(NexusTokenAcquisitionError):
            RouterLanSessionProvider._parse_cloud_trust({
                "mode": "pinned-pem",
                "origin": "https://cloud.example.test",
                "sha256": hashlib.sha256(
                    b"-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----\n"
                ).hexdigest(),
                "ca_pem": (
                    "-----BEGIN PRIVATE KEY-----\nsecret\n"
                    "-----END PRIVATE KEY-----\n"
                ),
            })
        oversized = TEST_CA_PEM + (" \n" * 32768)
        with self.assertRaises(NexusTokenAcquisitionError):
            RouterLanSessionProvider._parse_cloud_trust({
                "mode": "pinned-pem",
                "origin": "https://cloud.example.test",
                "sha256": hashlib.sha256(oversized.encode("ascii")).hexdigest(),
                "ca_pem": oversized,
            })
        with self.assertRaises(NexusTokenAcquisitionError):
            RouterLanSessionProvider._parse_cloud_trust({
                "mode": "pinned-pem",
                "origin": "https://cloud.example.test",
                "sha256": "0" * 64,
                "ca_pem": TEST_CA_PEM + "私钥",
            })
        with self.assertRaises(NexusTokenAcquisitionError):
            RouterLanSessionProvider._parse_cloud_trust({
                "mode": "system",
            })
        self.assertNotIn("SSL_CERT_FILE", vars(policy))

    def test_cloud_trust_refreshes_once_only_for_certificate_failure(self):
        provider = AutoTokenProvider("http://router.invalid", environ={})
        policy = CloudTrustPolicy(
            mode="system", origin="https://cloud.example.test:443"
        )
        request = urllib.request.Request(
            "https://cloud.example.test/api/v1/internal/run-context/exchange/"
        )
        response = object()
        certificate_error = urllib.error.URLError(
            ssl.SSLCertVerificationError(1, "certificate verify failed")
        )
        with mock.patch.object(
            provider, "_cloud_policy", side_effect=(policy, policy)
        ) as resolve, mock.patch(
            "nexus_agent.auth.urllib.request.urlopen",
            side_effect=(certificate_error, response),
        ) as urlopen:
            self.assertIs(
                provider.open_cloud_request(request, timeout=2, exchange=True),
                response,
            )
        self.assertEqual(resolve.call_args_list, [mock.call(), mock.call(force_refresh=True)])
        self.assertEqual(urlopen.call_count, 2)

    def test_cloud_trust_does_not_retry_http_errors(self):
        provider = AutoTokenProvider("http://router.invalid", environ={})
        policy = CloudTrustPolicy(
            mode="system", origin="https://cloud.example.test"
        )
        request = urllib.request.Request(
            "https://cloud.example.test/api/v1/internal/run-context/exchange/"
        )
        not_found = urllib.error.HTTPError(
            request.full_url, 404, "not found", {}, None
        )
        with mock.patch.object(
            provider, "_cloud_policy", return_value=policy
        ), mock.patch(
            "nexus_agent.auth.urllib.request.urlopen", side_effect=not_found
        ) as urlopen:
            with self.assertRaises(urllib.error.HTTPError):
                provider.open_cloud_request(request, timeout=2, exchange=True)
        not_found.close()
        urlopen.assert_called_once()

    def test_auto_provider_prefers_canonical_environment_token(self):
        provider = AutoTokenProvider(
            "http://router.invalid",
            environ={"NEXUS_AGENT_TOKEN": "already-issued"},
        )
        self.assertEqual(provider.get_token(), "already-issued")
        self.assertFalse(provider.refreshable)

    def test_auto_provider_discovers_no_jwt_and_sends_no_credential(self):
        router = self.base_url + "/noauth"
        provider = AutoTokenProvider(router, environ={})
        client = NexusAgentClient(router, token_provider=provider)
        result = client.invoke({"payload": {}})
        self.assertEqual(result, {
            "ok": True,
            "authorization_seen": False,
            "transaction_token_seen": False,
        })

    def test_client_refreshes_once_after_401(self):
        provider = RefreshProvider()
        client = NexusAgentClient(self.base_url, token_provider=provider)
        result = client.invoke({"payload": {}})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(provider.refreshes, 1)
        self.assertEqual(AuthFixture.protected_count, 2)

    @staticmethod
    def _registration(origin="agent://demo/echo-a"):
        return CapabilityRegistration(
            intent="demo.echo",
            origin=origin,
            endpoint="http://192.168.250.164:8080/invoke",
            tenant="demo",
            public_ipv6=None,
        )

    def test_default_client_bootstraps_without_authentication_environment(self):
        with mock.patch.dict("os.environ", {
            "NEXUS_AGENT_TOKEN": "",
            "NEXUS_JWT": "",
            "NEXUS_AGENT_JWT": "",
            "NEXUS_TOKEN": "",
            "NEXUS_AGENT_CLIENT_ID": "",
            "NEXUS_AGENT_CLIENT_SECRET": "",
        }):
            client = NexusAgentClient(self.base_url + "/lan")
            lease = client.register(self._registration())
        self.assertEqual(lease.route_id, "route-lan-1")
        self.assertEqual(AuthFixture.bootstrap_count, 1)
        request, headers = AuthFixture.bootstrap_requests[0]
        self.assertEqual(request["tenant"], "demo")
        self.assertEqual(request["origin"], "agent://demo/echo-a")
        self.assertEqual(set(request["scopes"]), {
            "agent.register", "agent.route", "agent.invoke",
        })
        self.assertNotIn("Authorization", headers)

    def test_lan_session_401_reboot_refreshes_and_retries_once(self):
        AuthFixture.reject_first_lan_session = True
        client = NexusAgentClient(self.base_url + "/lan", auth="auto")
        client.register(self._registration())
        result = client.invoke({
            "tenant": "demo",
            "source_agent": "echo-a",
            "payload": {"value": "hello"},
        })
        self.assertEqual(result["authorization"], "Bearer lan-session-2")
        self.assertEqual(AuthFixture.bootstrap_count, 2)

    def test_lan_session_refresh_window_and_mixed_identity_rejection(self):
        clock = [100.0]
        with mock.patch("nexus_agent.auth.time.monotonic", side_effect=lambda: clock[0]):
            client = NexusAgentClient(self.base_url + "/lan", auth="auto")
            client.register(self._registration())
            clock[0] = 371.0
            result = client.invoke({
                "tenant": "demo",
                "source_agent": "echo-a",
                "payload": {},
            })
        self.assertEqual(result["authorization"], "Bearer lan-session-2")
        self.assertEqual(AuthFixture.bootstrap_count, 2)
        with self.assertRaises(NexusTokenAcquisitionError):
            client.register(self._registration("agent://demo/echo-b"))

    def test_lan_session_token_never_uses_filesystem_persistence(self):
        client = NexusAgentClient(self.base_url + "/lan", auth="auto")
        with mock.patch("builtins.open", side_effect=AssertionError(
            "LAN session tokens must not use file persistence"
        )), mock.patch("pathlib.Path.open", side_effect=AssertionError(
            "LAN session tokens must not use pathlib persistence"
        )):
            lease = client.register(self._registration())
            result = client.invoke({
                "tenant": "demo",
                "source_agent": "echo-a",
                "payload": {},
            })
        self.assertEqual(lease.route_id, "route-lan-1")
        self.assertTrue(result["ok"])

    def test_structured_authentication_and_authorization_errors(self):
        client = NexusAgentClient(self.base_url, token="stale-token")
        with self.assertRaises(NexusAuthenticationError):
            client.invoke({"payload": {}})
        client = NexusAgentClient(self.base_url, token="unknown-token")
        with self.assertRaises(NexusAuthorizationError):
            client.invoke({"payload": {}})


if __name__ == "__main__":
    unittest.main()
