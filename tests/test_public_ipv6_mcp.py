"""Real IPv6 loopback MCP integration; not a public Internet reachability test."""

import asyncio
import ipaddress
import json
import pathlib
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

try:
    import httpx
    from fastmcp import Client, Context, FastMCP
    from fastmcp.client.elicitation import ElicitResult
    from fastmcp.client.transports import StreamableHttpTransport
    from pydantic import BaseModel
except ImportError:
    FastMCP = None

from nexus_agent import DirectIPv6Agent, HmacJwtServerAuth, NexusAgent, NoServerAuth
from nexus_agent.public_ipv6_mcp import NativeMCPListener, _ResetSafeSocket


def http_factory(**kwargs):
    return httpx.AsyncClient(trust_env=False, **kwargs)


def client(url, token=None, **kwargs):
    return Client(StreamableHttpTransport(url, auth=token, httpx_client_factory=http_factory),
                  timeout=5, **kwargs)


def free_port():
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
        probe.bind(("::1", 0))
        return probe.getsockname()[1]


async def initialize_http(http, url):
    headers = {"Accept": "application/json, text/event-stream"}
    response = await http.post(url, headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "shutdown-test", "version": "1"}}})
    response.raise_for_status()
    headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
    headers["MCP-Protocol-Version"] = "2025-11-25"
    response = await http.post(url, headers=headers, json={
        "jsonrpc": "2.0", "method": "notifications/initialized"})
    response.raise_for_status()
    return headers


@unittest.skipIf(FastMCP is None, "Install tests/requirements-native-mcp.txt")
class NativeMCPTest(unittest.IsolatedAsyncioTestCase):
    def listener(self, mcp, **options):
        listener = NativeMCPListener(mcp, address="::1", port=0,
                                     auth=options.pop("auth", NoServerAuth()),
                                     tenant="demo", **options)
        listener.start()
        self.addCleanup(listener.close)
        return listener, f"http://[::1]:{listener.port}/mcp"

    async def test_close_active_sse_is_local_and_idempotent(self):
        mcp, other = FastMCP("closing"), FastMCP("unaffected")
        listener, url = self.listener(mcp)
        survivor, other_url = self.listener(other)
        async with http_factory(timeout=5) as http, client(other_url) as peer:
            headers = await initialize_http(http, url)
            async with http.stream("GET", url, headers=headers) as stream:
                self.assertEqual(stream.status_code, 200)
                await asyncio.gather(asyncio.to_thread(listener.close),
                                     asyncio.to_thread(listener.close))
                # Graceful HTTP EOF, not a truncated chunked response or reset.
                await stream.aread()
            self.assertFalse(listener.thread.is_alive())
            self.assertTrue(survivor.is_healthy())
            self.assertTrue(await peer.ping())

    async def test_shutdown_cancels_active_tool(self):
        mcp = FastMCP("active shutdown")
        began, cancelled = threading.Event(), threading.Event()

        @mcp.tool
        async def slow() -> str:
            began.set()
            try:
                await asyncio.sleep(30)
                return "must not complete"
            finally:
                cancelled.set()

        listener, url = self.listener(mcp)
        async with http_factory(timeout=5) as http:
            headers = await initialize_http(http, url)
            task = asyncio.create_task(http.post(url, headers=headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "slow", "arguments": {}}}))
            try:
                self.assertTrue(await asyncio.to_thread(began.wait, 3))
                await asyncio.to_thread(listener.close)
                self.assertTrue(cancelled.is_set())
                response = await task
                self.assertNotIn('"result"', response.text)
                self.assertFalse(listener.thread.is_alive())
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_handler_retains_async_subprocess_support(self):
        mcp = FastMCP("async subprocess")

        @mcp.tool
        async def process() -> str:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "print('native-mcp-subprocess')",
                stdout=asyncio.subprocess.PIPE)
            output, _ = await proc.communicate()
            return output.decode().strip()

        _, url = self.listener(mcp)
        async with client(url) as c:
            self.assertEqual((await c.call_tool("process")).data, "native-mcp-subprocess")

    def test_old_mcp_stack_rejected_before_socket_allocation(self):
        with mock.patch("importlib.metadata.version", return_value="1.25.0"), \
                mock.patch("nexus_agent.public_ipv6_mcp.socket.socket") as sock:
            with self.assertRaisesRegex(Exception, "mcp>=1.30,<2"):
                NativeMCPListener(FastMCP("old"), address="::1", port=0,
                                  auth=NoServerAuth(), tenant="demo")
            sock.assert_not_called()

    def test_shutdown_handles_reset_but_preserves_other_errors(self):
        with _ResetSafeSocket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            with mock.patch.object(socket.socket, "shutdown", side_effect=ConnectionResetError):
                sock.shutdown(socket.SHUT_RDWR)
                with self.assertRaises(ConnectionResetError):
                    sock.shutdown(socket.SHUT_WR)
            with mock.patch.object(socket.socket, "shutdown", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    sock.shutdown(socket.SHUT_RDWR)
        self.assertEqual(sock.fileno(), -1)

    async def test_teardown_subprocess_has_no_dependency_warnings(self):
        # A subprocess captures cross-thread warnings, loop exception handlers,
        # and final GC warnings that ordinary unittest assertions cannot catch.
        script = '''
import gc, sys, unittest, warnings
warnings.simplefilter("always", ResourceWarning)
from test_public_ipv6_mcp import NativeMCPTest
names = [
    "test_native_tools_resources_templates_and_prompts",
    "test_two_agents_call_each_other_concurrently_and_reconnect",
    "test_close_active_sse_is_local_and_idempotent",
    "test_shutdown_cancels_active_tool",
    "test_cancel_does_not_break_next_call",
    "test_handler_retains_async_subprocess_support",
]
suite = unittest.TestSuite(NativeMCPTest(name) for _ in range(3) for name in names)
result = unittest.TextTestRunner().run(suite)
gc.collect()
sys.exit(not result.wasSuccessful())
'''
        result = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-W", "always", "-c", script],
            cwd=pathlib.Path(__file__).parent, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=120)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        for unwanted in ("ResourceWarning", "DeprecationWarning", "Exception in callback",
                         "timeout graceful shutdown exceeded", "Task was destroyed",
                         "ASGI callable returned without completing", "Traceback"):
            self.assertNotIn(unwanted, output)

    async def test_native_tools_resources_templates_and_prompts(self):
        mcp = FastMCP("native components")

        @mcp.tool
        def add(a: int, b: int) -> dict:
            return {"total": a + b}

        @mcp.resource("data://hello")
        def hello() -> str:
            return "hello resource"

        @mcp.resource("data://items/{name}")
        def item(name: str) -> str:
            return "item:" + name

        @mcp.prompt
        def greet(name: str) -> str:
            return "Hello " + name

        _, url = self.listener(mcp)
        async with client(url) as c:
            self.assertIsNotNone(c.initialize_result)
            self.assertEqual([t.name for t in await c.list_tools()], ["add"])
            self.assertEqual((await c.call_tool("add", {"a": 20, "b": 22})).data, {"total": 42})
            self.assertEqual(len(await c.list_resources()), 1)
            self.assertEqual(len(await c.list_resource_templates()), 1)
            self.assertEqual((await c.read_resource("data://hello"))[0].text, "hello resource")
            self.assertEqual((await c.read_resource("data://items/book"))[0].text, "item:book")
            self.assertEqual([p.name for p in await c.list_prompts()], ["greet"])
            self.assertEqual((await c.get_prompt("greet", {"name": "MCP"})).messages[0].content.text,
                             "Hello MCP")

    async def test_server_callbacks_and_progress_reach_native_caller(self):
        mcp = FastMCP("callbacks")

        class Answer(BaseModel):
            name: str

        @mcp.tool
        async def interact(ctx: Context) -> dict:
            roots = await ctx.list_roots()
            sample = await ctx.sample("Reply with sample", max_tokens=30)
            answer = await ctx.elicit("Your name?", Answer)
            await ctx.report_progress(1, 1, "done")
            return {"root": str(roots[0].uri), "sample": sample.text,
                    "answer": answer.data.name}

        async def sample_handler(messages, params, context):
            return "sample response"

        async def elicit_handler(message, schema, params, context):
            return ElicitResult(action="accept", content={"name": "caller"})

        progress = []

        async def on_progress(current, total, message):
            progress.append(message)

        _, url = self.listener(mcp)
        async with client(url, roots=["file:///authorized-workspace"],
                          sampling_handler=sample_handler, elicitation_handler=elicit_handler,
                          progress_handler=on_progress) as c:
            result = await c.call_tool("interact")
            self.assertEqual(result.data, {"root": "file:///authorized-workspace",
                                          "sample": "sample response", "answer": "caller"})
            self.assertIn("done", progress)

    async def test_two_agents_call_each_other_concurrently_and_reconnect(self):
        a, b = FastMCP("Agent A"), FastMCP("Agent B")
        urls = {}

        @a.tool
        async def echo_a(marker: str) -> dict:
            await asyncio.sleep(0.05)
            return {"agent": "A", "marker": marker}

        @b.tool
        async def echo_b(marker: str) -> dict:
            await asyncio.sleep(0.05)
            return {"agent": "B", "marker": marker}

        @a.tool
        async def call_b(marker: str) -> dict:
            async with client(urls["b"]) as peer:
                return (await peer.call_tool("echo_b", {"marker": marker})).data

        @b.tool
        async def call_a(marker: str) -> dict:
            async with client(urls["a"]) as peer:
                return (await peer.call_tool("echo_a", {"marker": marker})).data

        _, urls["a"] = self.listener(a)
        _, urls["b"] = self.listener(b)
        async with client(urls["a"]) as ca, client(urls["b"]) as cb:
            ra, rb = await asyncio.gather(ca.call_tool("call_b", {"marker": "from-A"}),
                                          cb.call_tool("call_a", {"marker": "from-B"}))
            self.assertEqual(ra.data, {"agent": "B", "marker": "from-A"})
            self.assertEqual(rb.data, {"agent": "A", "marker": "from-B"})
        async with client(urls["a"]) as reconnected:
            self.assertEqual((await reconnected.call_tool("echo_a", {"marker": "again"})).data["marker"], "again")

    async def test_jwt_rejections_and_cross_caller_session_isolation(self):
        auth = HmacJwtServerAuth("native-mcp-test-key-32-characters!", issuer="test", audience="agent-a")
        mcp = FastMCP("protected")

        @mcp.tool
        def echo(value: int) -> int:
            return value

        _, url = self.listener(mcp, auth=auth)

        def issue(subject="a", **kwargs):
            return auth.issue(subject=subject, tenant=kwargs.pop("tenant", "demo"),
                              source_agent="agent://demo/" + subject, **kwargs)

        valid = issue()
        other_key = HmacJwtServerAuth("different-test-key-32-characters!", issuer="test", audience="agent-a")
        other_audience = HmacJwtServerAuth("native-mcp-test-key-32-characters!", issuer="test", audience="agent-b")
        bad_tokens = [None, "invalid-test-sentinel", issue(tenant="other"), issue(scopes=()), issue(now=1),
                      other_key.issue(subject="a", tenant="demo", source_agent="agent://demo/a"),
                      other_audience.issue(subject="a", tenant="demo", source_agent="agent://demo/a")]
        async with httpx.AsyncClient(trust_env=False) as http:
            for token in bad_tokens:
                with self.subTest(token_kind=bad_tokens.index(token)):
                    response = await http.post(url, json={}, headers={"Authorization": "Bearer " + token} if token else {})
                    self.assertEqual(response.status_code, 401)
                    self.assertNotIn(token or "not-a-secret", response.text)
            async with client(url, valid) as c:
                self.assertEqual((await c.call_tool("echo", {"value": 7})).data, 7)
                session_id = c.transport.get_session_id()
                for method in ("POST", "GET", "DELETE"):
                    response = await http.request(method, url, headers={
                        "Authorization": "Bearer " + issue("b"),
                        "Mcp-Session-Id": session_id,
                        "Accept": "application/json, text/event-stream",
                    }, json={"jsonrpc": "2.0", "id": 55, "method": "tools/list"})
                    self.assertEqual(response.status_code, 404)
                # Rejected cross-caller DELETE did not destroy the owner's session.
                self.assertEqual((await c.call_tool("echo", {"value": 8})).data, 8)

    async def test_body_and_host_origin_bounds(self):
        _, url = self.listener(FastMCP("bounds"), max_request_bytes=128)
        async with httpx.AsyncClient(trust_env=False) as http:
            response = await http.post(url, content=b"x" * 129)
            self.assertEqual(response.status_code, 413)
            response = await http.post(url, json={}, headers={"Host": "untrusted.example"})
            self.assertEqual(response.status_code, 421)
            response = await http.post(url, json={}, headers={"Origin": "https://untrusted.example"})
            self.assertEqual(response.status_code, 403)

    async def test_tls_mtls_and_certificate_verification(self):
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
        except ImportError:
            self.skipTest("cryptography is required for real TLS acceptance")
        temp = tempfile.TemporaryDirectory(prefix="nexus-mcp-tls-")
        self.addCleanup(temp.cleanup)
        root = pathlib.Path(temp.name)
        now = datetime.now(timezone.utc)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Native MCP Test CA")])
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
              .public_key(key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
              .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
              .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
              .sign(key, hashes.SHA256()))
        leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Native MCP Test Server")])
        leaf = (x509.CertificateBuilder().subject_name(leaf_name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False)
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv6Address("::1"))]), False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,
                                                     ExtendedKeyUsageOID.CLIENT_AUTH]), False)
                .sign(key, hashes.SHA256()))
        ca_path, cert_path, key_path = root / "ca.pem", root / "cert.pem", root / "key.pem"
        ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                               serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        mcp = FastMCP("TLS")

        @mcp.tool
        def hello() -> str:
            return "verified"

        _, http_url = self.listener(mcp, cert_file=str(cert_path), key_file=str(key_path),
                                    client_ca_file=str(ca_path))
        url = http_url.replace("http:", "https:")
        context = ssl.create_default_context(cafile=str(ca_path))
        context.load_cert_chain(str(cert_path), str(key_path))

        def secure_factory(**kwargs):
            return httpx.AsyncClient(trust_env=False, verify=context, **kwargs)

        async with Client(StreamableHttpTransport(url, httpx_client_factory=secure_factory), timeout=5) as c:
            self.assertEqual((await c.call_tool("hello")).data, "verified")
        for label, verify, endpoint in (
            ("untrusted CA", True, url),
            ("missing client certificate", ssl.create_default_context(cafile=str(ca_path)), url),
            ("wrong certificate hostname", context, url.replace("[::1]", "localhost")),
        ):
            with self.subTest(label=label):
                async with httpx.AsyncClient(trust_env=False, verify=verify, timeout=3) as http:
                    with self.assertRaises(httpx.TransportError):
                        await http.post(endpoint, json={})

    async def test_cancel_does_not_break_next_call(self):
        mcp = FastMCP("cancel")
        began, cancelled = threading.Event(), threading.Event()

        @mcp.tool
        async def slow() -> str:
            began.set()
            try:
                await asyncio.sleep(30)
                return "unexpected"
            finally:
                cancelled.set()

        @mcp.tool
        def ping_tool() -> str:
            return "ok"

        _, url = self.listener(mcp)
        async with client(url) as c:
            async with httpx.AsyncClient(trust_env=False) as http:
                headers = {"Mcp-Session-Id": c.transport.get_session_id(),
                           "Accept": "application/json, text/event-stream"}
                task = asyncio.create_task(http.post(url, headers=headers, json={
                    "jsonrpc": "2.0", "id": 7700, "method": "tools/call",
                    "params": {"name": "slow", "arguments": {}},
                }))
                try:
                    self.assertTrue(await asyncio.to_thread(began.wait, 3))
                    response = await http.post(url, headers=headers, json={
                        "jsonrpc": "2.0", "method": "notifications/cancelled",
                        "params": {"requestId": 7700, "reason": "test cancellation"},
                    })
                    self.assertEqual(response.status_code, 202)
                    self.assertTrue(await asyncio.to_thread(cancelled.wait, 3))
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertEqual((await c.call_tool("ping_tool")).data, "ok")


@unittest.skipIf(FastMCP is None, "Install tests/requirements-native-mcp.txt")
class PublicFacadeMCPTest(unittest.IsolatedAsyncioTestCase):
    def agent(self, mcp, **options):
        # Exercise production facade on loopback without adding public NIC aliases.
        with mock.patch.object(ipaddress.IPv6Address, "is_global", new_callable=mock.PropertyMock, return_value=True):
            return NexusAgent.public_ipv6("::1", mcp=mcp, auth="none",
                                          port=free_port(), mcp_port=free_port(), **options)

    async def test_native_and_legacy_listeners_share_lifecycle(self):
        from nexus_agent.fastmcp import NexusMCPServer
        mcp = NexusMCPServer("both protocols")

        @mcp.tool
        def echo(value: str) -> str:
            return value

        agent = self.agent(mcp)

        @agent.capability("demo.echo")
        def legacy(payload):
            return {"echo": echo(**payload)}

        handle = agent.start(announce=False)
        try:
            async with client(agent.mcp_url) as c:
                self.assertEqual((await c.call_tool("echo", {"value": "native"})).data, "native")
            direct = DirectIPv6Agent.plain_http("::1", port=agent.server.port)
            result = await asyncio.to_thread(direct.invoke, "demo.echo", {"value": "legacy"},
                                             tenant="demo", source_agent="agent://demo/caller")
            self.assertEqual(result, {"echo": "legacy"})
        finally:
            await asyncio.to_thread(handle.close)
        handle.close()
        self.assertFalse(handle.thread.is_alive())
        self.assertFalse(agent._mcp_listener.thread.is_alive())

    async def test_mcp_only_agent_and_confirm_failure_release_lease(self):
        lease = mock.Mock(address="::1", last_error=None)
        lease.confirm.side_effect = RuntimeError("confirm failed")
        allocator = mock.Mock()
        allocator.allocate.return_value = lease
        with mock.patch.object(ipaddress.IPv6Address, "is_global", new_callable=mock.PropertyMock, return_value=True):
            agent = NexusAgent.public_ipv6("auto", allocator=allocator, mcp=FastMCP("lease"),
                                          port=free_port(), mcp_port=free_port(), auth="none")
        with self.assertRaisesRegex(RuntimeError, "confirm failed"):
            agent.start(announce=False)
        lease.close.assert_called_once()
        lease.start_auto_renew.assert_not_called()
        self.assertFalse(agent._mcp_listener.thread.is_alive())
        self.assertEqual(agent.server._httpd.socket.fileno(), -1)

    async def test_mcp_lifespan_failure_does_not_confirm_lease(self):
        @asynccontextmanager
        async def broken(_server):
            raise RuntimeError("intentional startup failure")
            yield

        agent = self.agent(FastMCP("broken", lifespan=broken))
        with self.assertLogs("uvicorn.error", level="ERROR") as logs:
            with self.assertRaisesRegex(Exception, "did not start"):
                await asyncio.to_thread(agent.start, announce=False)
        self.assertIn("intentional startup failure", "\n".join(logs.output))
        self.assertFalse(agent._mcp_listener.thread.is_alive())

    async def test_mcp_only_success_and_shutdown_port_cleanup(self):
        agent = self.agent(FastMCP("mcp only"))
        handle = agent.start(announce=False)
        async with client(agent.mcp_url) as c:
            self.assertEqual(await c.list_tools(), [])
        await asyncio.to_thread(handle.close)
        self.assertEqual(agent._mcp_listener._socket.fileno(), -1)
        self.assertEqual(agent.server._httpd.socket.fileno(), -1)

    async def test_invalid_port_and_mcp_object_do_not_leak_address(self):
        allocator = mock.Mock()
        for ports in ({"port": 65535}, {"port": 9443, "mcp_port": 9443},
                      {"port": 9443, "mcp_port": True}):
            with self.subTest(ports=ports), self.assertRaises(ValueError):
                NexusAgent.public_ipv6("auto", allocator=allocator, mcp=FastMCP("invalid"),
                                      auth="none", **ports)
        allocator.allocate.assert_not_called()
        lease = mock.Mock(address="::1", last_error=None)
        allocator.allocate.return_value = lease
        with mock.patch.object(ipaddress.IPv6Address, "is_global", new_callable=mock.PropertyMock, return_value=True):
            with self.assertRaises(TypeError):
                NexusAgent.public_ipv6("auto", allocator=allocator, mcp=object(),
                                      port=free_port(), mcp_port=free_port(), auth="none")
        lease.close.assert_called_once()

    async def test_failed_mcp_port_bind_releases_invoke_port_and_lease(self):
        lease = mock.Mock(address="::1", last_error=None)
        allocator = mock.Mock()
        allocator.allocate.return_value = lease
        invoke_port = free_port()
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as occupied:
            occupied.bind(("::1", 0))
            occupied.listen()
            with mock.patch.object(ipaddress.IPv6Address, "is_global", new_callable=mock.PropertyMock, return_value=True):
                with self.assertRaisesRegex(Exception, "cannot initialize IPv6 listener"):
                    NexusAgent.public_ipv6("auto", allocator=allocator, mcp=FastMCP("busy"),
                                          port=invoke_port, mcp_port=occupied.getsockname()[1], auth="none")
        lease.close.assert_called_once()
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", invoke_port))


if __name__ == "__main__":
    unittest.main()
