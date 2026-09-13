import importlib.util
import asyncio
import os
import tempfile
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


EXAMPLE = Path(__file__).parents[1] / "examples" / "browser_session_agent.py"
sys.path.insert(0, str(EXAMPLE.parents[1] / "src"))

from nexus_agent import NexusBrowserActionFailed  # noqa: E402


class AttachedBrowserAgentExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("browser_session_agent", EXAMPLE)
        cls.module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.module)

    def test_private_display_chat_and_structured_tools_share_browser_contract(self):
        self.assertTrue(self.module.CHAT_TOOL.chat)
        self.assertTrue(self.module.CHAT_TOOL.task)
        self.assertTrue(self.module.CHAT_TOOL.interactive)
        self.assertTrue(self.module.CHAT_TOOL.continuable)
        self.assertEqual(self.module.CHAT_TOOL.input_schema["required"], ["message"])
        self.assertEqual(
            self.module.CHAT_TOOL.input_schema["properties"]["files"]["items"]["required"],
            ["file_id"],
        )
        self.assertEqual(self.module.STRUCTURED_TOOL.name, "browser_session")

        fake_agent = mock.Mock()
        fake_agent.capability.side_effect = lambda *_args, **_kwargs: lambda handler: handler
        with mock.patch.object(self.module, "NexusAgent", return_value=fake_agent) as agent_type:
            result = self.module.build_agent(
                router="http://router.test/",
                tenant="demo",
                agent_id="browser-agent",
                listen_host="127.0.0.1",
                port=9443,
            )

        self.assertIs(result, fake_agent)
        values = agent_type.call_args.kwargs
        self.assertEqual(values["computer_requirement"], "required")
        self.assertEqual(
            values["workspace_capabilities"],
            ("files.list", "files.read", "browser.control"),
        )
        self.assertNotIn("command.execute", values["workspace_capabilities"])
        tools = [call.kwargs["tool"] for call in fake_agent.capability.call_args_list]
        self.assertEqual([tool.name for tool in tools], ["browser_chat", "browser_session"])

    def test_handlers_only_request_the_attached_session(self):
        source = EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("ctx.browser.attached_session", source)
        self.assertNotIn("ctx.browser.session(", source)

    def test_import_exposes_hosted_agent_without_router_socket_or_arguments(self):
        with mock.patch.dict(os.environ, {"NEXUS_AGENT_RUNTIME_MODE": "openwrt"}), \
             mock.patch("nexus_agent.agent.resolve_router_url", side_effect=AssertionError("Router discovery on import")), \
             mock.patch("nexus_agent.agent.NexusAgentServer", side_effect=AssertionError("Edge listener on import")), \
             mock.patch("argparse.ArgumentParser.parse_args", side_effect=AssertionError("CLI parsing on import")):
            spec = importlib.util.spec_from_file_location("browser_upload_probe", EXAMPLE)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        self.assertEqual(module.agent.runtime_mode, "hosted")
        self.assertEqual(set(module.agent.server.handlers), {"browser.chat", "browser.session"})
        self.assertEqual(
            module.agent.computer.workspace_capabilities,
            ("files.list", "files.read", "browser.control"),
        )

    def test_edge_cli_preserves_tls_registration_and_ready_file(self):
        handle = mock.Mock()
        handle.wait_for_cloud.return_value = SimpleNamespace(agent_id="cloud-agent", runtime_id="runtime",
            transport="direct_ipv6", manifest_digest="digest")
        handle.published = [SimpleNamespace(route_id="route")]
        edge = SimpleNamespace(runtime_mode="openwrt", origin="agent://demo/browser", start=mock.Mock(return_value=handle))
        with tempfile.TemporaryDirectory() as folder:
            ready = Path(folder) / "ready.json"
            argv = [str(EXAMPLE), "--router-url", "http://router.test:7446", "--tenant", "demo",
                    "--agent-id", "browser", "--cloud-name", "Browser QA", "--listen", "::",
                    "--advertise-address", "fd00::20", "--port", "9443", "--cert", "server.pem",
                    "--key", "server.key", "--tls-server-name", "browser.test", "--ca-bundle-id", "qa-ca",
                    "--router-ca", "ca.pem", "--lease-seconds", "60", "--cloud-timeout", "180",
                    "--ready-file", str(ready)]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(self.module, "build_agent", return_value=edge) as factory, \
                 mock.patch.object(self.module.signal, "signal"):
                self.assertEqual(self.module.main(), 0)
            factory.assert_called_once_with(router="http://router.test:7446", tenant="demo", agent_id="browser",
                cloud_name="Browser QA", listen_host="::", advertise_address="fd00::20", port=9443,
                cert_file="server.pem", key_file="server.key", server_tls_name="browser.test",
                server_ca_bundle_id="qa-ca", router_ca_file="ca.pem", lease_seconds=60)
            self.assertEqual(self.module.json.loads(ready.read_text())["cloud_agent_id"], "cloud-agent")
        edge.start.assert_called_once_with(auto_renew=True, renew_fraction=0.2, announce=False)
        handle.wait_for_cloud.assert_called_once_with(timeout=180, poll_interval=1)
        handle.wait.assert_called_once()
        handle.close.assert_called_once()

    def test_hosted_cli_serves_mcp_without_edge_registration(self):
        hosted = SimpleNamespace(runtime_mode="hosted", run=mock.Mock(), start=mock.Mock())
        with mock.patch.object(sys, "argv", [str(EXAMPLE)]), \
             mock.patch.object(self.module, "build_agent", return_value=hosted):
            self.assertEqual(self.module.main(), 0)
        hosted.run.assert_called_once_with()
        hosted.start.assert_not_called()

    def test_trusted_upload_boot_and_real_mcp_browser_tools(self):
        try:
            from fastmcp import Client
        except ImportError:
            self.skipTest("Optional FastMCP dependency is not installed")
        from nexus_agent import NexusRunContext
        boot = Path(os.environ.get("NEXUS_SERVER_SOURCE", "__server_source_not_configured__")) / "apps" / "agents" / "python_profile" / "nexus_boot.py"
        if not boot.is_file():
            self.skipTest("Requires the separate Nexus Server source checkout")
        spec = importlib.util.spec_from_file_location("browser_upload_boot", boot)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        locate = importlib.util.spec_from_file_location
        def redirect(name, path, *args, **kwargs):
            if name == "uploaded_agent":
                self.assertEqual(os.environ["NEXUS_AGENT_RUNTIME_MODE"], "hosted")
                self.assertEqual(path, "/opt/nexus-python/agent.py")
                return locate(name, EXAMPLE)
            return locate(name, path, *args, **kwargs)
        with mock.patch.dict(os.environ, {"NEXUS_AGENT_RUNTIME_MODE": "openwrt"}), \
             mock.patch("importlib.util.spec_from_file_location", side_effect=redirect), \
             mock.patch("nexus_agent.agent.resolve_router_url", side_effect=AssertionError("Router on upload")):
            server = module.load_server("agent")
        sessions, events = [], []
        def context_factory(**_):
            context = NexusRunContext(run_id=f"browser-run-{len(sessions)}", _event_sink=lambda event: events.append(event) or True)
            session = mock.Mock()
            observation = SimpleNamespace(title="Example", url="https://example.com", observation_id="observation", revision=1)
            session.open.return_value = observation
            session.perform.return_value = observation
            context.browser.attached_session = mock.Mock(return_value=session)
            sessions.append(session)
            return context
        async def run():
            async with Client(server.fastmcp) as client:
                tools = {tool.name: tool for tool in await client.list_tools()}
                self.assertEqual(set(tools), {"browser_chat", "browser_session"})
                for tool in tools.values():
                    self.assertEqual(tool.meta["nexus"]["agent_contract"]["computer"], {
                        "requirement": "required",
                        "workspace_capabilities": ["files.list", "files.read", "browser.control"],
                    })
                self.assertEqual(tools["browser_chat"].inputSchema, self.module.CHAT_TOOL.input_schema)
                self.assertTrue(tools["browser_chat"].meta["nexus"]["continuable"])
                result = await client.call_tool("browser_chat", {"message": "打开 https://example.com"})
                self.assertEqual(result.data["status"], "completed")
                result = await client.call_tool("browser_session", {"url": "https://example.com",
                    "actions": [{"kind": "scroll", "parameters": {"delta_y": 300}}]})
                self.assertEqual(result.data["status"], "completed")
        with mock.patch("nexus_agent.hosted.NexusRunContext.from_env", side_effect=context_factory):
            asyncio.run(run())
        self.assertEqual(len(sessions), 2)
        sessions[0].open.assert_called_once_with("https://example.com")
        sessions[1].perform.assert_called_once()
        self.assertTrue(any(event.get("type") == "TEXT_MESSAGE_END" for event in events))

    def test_fill_prefers_the_editable_node_over_its_label(self):
        label = SimpleNamespace(ref="label-1", role="", tag="label", name="Name", text="Name")
        input_node = SimpleNamespace(ref="input-1", role="textbox", tag="input", name="Name", text="")
        before = SimpleNamespace(dom=SimpleNamespace(nodes=(label, input_node)))
        after = SimpleNamespace(
            observation_id="observation-2",
            revision=2,
            title="Nexus Attached Browser Fixture",
        )
        locator = mock.Mock()
        locator.fill.return_value = after
        session = mock.Mock()
        session.observe.return_value = before
        session.element.return_value = locator
        context = SimpleNamespace(
            browser=SimpleNamespace(attached_session=mock.Mock(return_value=session)),
            chat=SimpleNamespace(say=mock.Mock(), ask=mock.Mock()),
            turn_index=1,
        )

        result = self.module.browser_chat(
            {"message": "Fill Name with Nexus Browser"},
            context,
        )

        session.element.assert_called_once_with("input-1")
        locator.fill.assert_called_once_with("Nexus Browser")
        context.chat.ask.assert_not_called()
        self.assertEqual(result["revision"], 2)

    def test_chat_opens_a_url_from_a_verified_attached_file(self):
        observation = SimpleNamespace(
            observation_id="observation-from-file",
            revision=3,
            title="Attached URL",
            url="https://example.com/from-file",
        )
        session = mock.Mock()
        session.open.return_value = observation

        def download(_reference, destination):
            Path(destination).write_text(
                "Open this URL: https://example.com/from-file\n",
                encoding="utf-8",
            )

        context = SimpleNamespace(
            browser=SimpleNamespace(attached_session=mock.Mock(return_value=session)),
            files=SimpleNamespace(download=mock.Mock(side_effect=download)),
            chat=SimpleNamespace(say=mock.Mock(), ask=mock.Mock()),
            turn_index=1,
        )
        reference = {
            "file_id": "2af04193-91e4-41b8-8502-9bcf16a7785f",
            "name": "links.txt",
            "content_type": "text/plain",
            "size_bytes": 48,
        }

        result = self.module.browser_chat(
            {"message": "打开 @links.txt 中的网址", "files": [reference]},
            context,
        )

        context.files.download.assert_called_once()
        session.open.assert_called_once_with("https://example.com/from-file")
        self.assertEqual(result["observation_id"], "observation-from-file")
        self.assertNotIn("from-file", context.chat.say.call_args.args[0].casefold())

    def test_recoverable_browser_action_failure_keeps_chat_available(self):
        session = mock.Mock()
        session.open.side_effect = NexusBrowserActionFailed(
            "Browser coordinates are outside the viewport"
        )
        context = SimpleNamespace(
            browser=SimpleNamespace(attached_session=mock.Mock(return_value=session)),
            chat=SimpleNamespace(say=mock.Mock(), ask=mock.Mock()),
            turn_index=1,
        )

        result = self.module.browser_chat(
            {"message": "Open https://example.com"},
            context,
        )

        self.assertEqual(result, {"status": "needs_input", "error_code": "BROWSER_ACTION_FAILED"})
        self.assertIn("Browser coordinates", context.chat.say.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
