import pathlib
import sys
import unittest
from unittest import mock


SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent import (  # noqa: E402
    DirectIPv6Agent,
    HmacJwtServerAuth,
    NexusAgent,
    NexusAgentServer,
    NexusHttpError,
    NoServerAuth,
    PublicIPv6Agent,
    SseEvent,
)


class DirectServerRuntimeTest(unittest.TestCase):
    def _server(self, auth):
        try:
            server = NexusAgentServer(
                "::1",
                0,
                path="/agent/v1/invoke",
                stream_path="/agent/v1/invoke-stream",
                address_family="ipv6",
                auth=auth,
            )
        except OSError as exc:
            self.skipTest(f"IPv6 loopback is unavailable: {exc}")
        return server

    def _run(self, server):
        thread = server.serve_in_thread()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return thread

    def test_no_jwt_direct_sync_and_stream_use_public_api_paths(self):
        server = self._server(NoServerAuth())

        @server.handler("demo.echo")
        def echo(envelope):
            return {"payload": envelope.payload, "subject": envelope.authenticated_subject}

        @server.stream_handler("demo.progress")
        def progress(_envelope):
            yield SseEvent(data="one", event="progress")
            yield SseEvent(data='{"ok":true}', event="result")

        self._run(server)
        target = DirectIPv6Agent.plain_http("::1", port=server.port)
        result = target.invoke(
            "demo.echo",
            {"value": 7},
            tenant="demo",
            source_agent="agent://demo/caller",
        )
        self.assertEqual(result, {"payload": {"value": 7}, "subject": None})
        events = list(target.invoke_stream(
            "demo.progress",
            {},
            tenant="demo",
            source_agent="agent://demo/caller",
        ))
        self.assertEqual([event.event for event in events], ["progress", "result"])

    def test_hs256_jwt_is_verified_and_bound_to_envelope_identity(self):
        auth = HmacJwtServerAuth(
            "0123456789abcdef0123456789abcdef",
            issuer="https://issuer.example",
            audience="public-agent",
        )
        server = self._server(auth)

        @server.handler("demo.secure")
        def secure(envelope):
            return {
                "subject": envelope.authenticated_subject,
                "scopes": list(envelope.authenticated_scopes),
            }

        self._run(server)
        token = auth.issue(
            subject="user-42",
            tenant="demo",
            source_agent="agent://demo/caller",
        )
        target = DirectIPv6Agent.plain_http("::1", port=server.port, token=token)
        result = target.invoke(
            "demo.secure",
            {},
            tenant="demo",
            source_agent="agent://demo/caller",
        )
        self.assertEqual(result["subject"], "user-42")
        self.assertEqual(result["scopes"], ["agent.invoke"])

        wrong_identity = DirectIPv6Agent.plain_http(
            "::1", port=server.port, token=token
        )
        with self.assertRaises(NexusHttpError) as captured:
            wrong_identity.invoke(
                "demo.secure",
                {},
                tenant="other",
                source_agent="agent://demo/caller",
            )
        self.assertEqual(captured.exception.status, 403)

    def test_missing_jwt_is_rejected(self):
        auth = HmacJwtServerAuth(
            "0123456789abcdef0123456789abcdef",
            issuer="https://issuer.example",
            audience="public-agent",
        )
        server = self._server(auth)

        @server.handler("demo.secure")
        def secure(_envelope):
            return {"ok": True}

        self._run(server)
        target = DirectIPv6Agent.plain_http("::1", port=server.port)
        with self.assertRaises(NexusHttpError) as captured:
            target.invoke(
                "demo.secure",
                {},
                tenant="demo",
                source_agent="agent://demo/caller",
            )
        self.assertEqual(captured.exception.status, 401)


class PublicIPv6FacadeTest(unittest.TestCase):
    def test_requires_global_address_and_explicit_auth(self):
        with self.assertRaisesRegex(ValueError, "global IPv6"):
            PublicIPv6Agent("fd00::20", auth="none")
        with self.assertRaises(TypeError):
            PublicIPv6Agent("2606:4700:4700::20")

    def test_nexus_agent_factory_builds_fixed_endpoint_without_router(self):
        fake_server = mock.Mock()
        fake_server.port = 9443
        with mock.patch(
            "nexus_agent.public_ipv6_agent.NexusAgentServer",
            return_value=fake_server,
        ) as server_class:
            agent = NexusAgent.public_ipv6(
                "2606:4700:4700::20",
                port=9443,
                auth="none",
                tenant="demo",
                agent_id="echo-1",
            )
        self.assertEqual(agent.origin, "agent://demo/echo-1")
        self.assertEqual(agent.endpoint.url, "http://[2606:4700:4700::20]:9443")
        options = server_class.call_args.kwargs
        self.assertEqual(options["path"], "/agent/v1/invoke")
        self.assertEqual(options["stream_path"], "/agent/v1/invoke-stream")
        self.assertEqual(options["address_family"], "ipv6")
        self.assertIsInstance(options["auth"], NoServerAuth)


if __name__ == "__main__":
    unittest.main()
