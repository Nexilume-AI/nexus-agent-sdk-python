import ipaddress
import pathlib
import sys
import types
import unittest
from unittest import mock


SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent import ipv6_cli  # noqa: E402
from nexus_agent.ipv6_cli import (  # noqa: E402
    IPv6Candidate,
    _parse_excluded_ports,
    _parse_global_64_routes,
    _parse_interfaces,
    _parse_ipv6_addresses,
    _select_candidate,
    _ensure_service_running,
    build_parser,
    choose_port,
    discover_windows_candidates,
    main,
)


PREFIX = "2606:4700:4700:1200::/64"


class IPv6CliParsingTest(unittest.TestCase):
    def test_parses_localized_independent_netsh_tables(self):
        interfaces = _parse_interfaces(
            "Idx Met MTU State Name\n"
            "96 20 1500 connected vEthernet (External WAN)\n"
        )
        self.assertEqual(interfaces[96], "vEthernet (External WAN)")
        routes = _parse_global_64_routes(
            "No Manual 16 2606:4700:4700:1200::/64 96 fe80::1\n"
            "No Manual 256 fe80::/64 96 Ethernet\n"
        )
        self.assertEqual(routes, [(96, ipaddress.IPv6Network(PREFIX))])
        addresses = _parse_ipv6_addresses(
            "Address 2606:4700:4700:1200::20 Parameters\n"
            "Address fe80::1%96 Parameters\n"
        )
        self.assertIn(ipaddress.IPv6Address("2606:4700:4700:1200::20"), addresses)

    def test_discovers_only_prefix_with_current_global_address(self):
        backend = mock.Mock()
        backend._run.side_effect = [
            "96 20 1500 connected vEthernet (External WAN)\n",
            "No Manual 16 2606:4700:4700:1200::/64 96 fe80::1\n",
            "Address 2606:4700:4700:1200::20 Parameters\n",
        ]
        with mock.patch("nexus_agent.ipv6_cli._windows_only"):
            candidates = discover_windows_candidates(backend)
        self.assertEqual(candidates, (
            IPv6Candidate(
                interface="vEthernet (External WAN)",
                interface_index=96,
                prefix=PREFIX,
                current_address="2606:4700:4700:1200::20",
            ),
        ))

    def test_port_selection_skips_excluded_and_unbindable_ports(self):
        ranges = _parse_excluded_ports("19423 19522\n50000 50059 *\n")
        self.assertEqual(ranges, ((19423, 19522), (50000, 50059)))
        with mock.patch(
            "nexus_agent.ipv6_cli._port_bindable",
            side_effect=lambda port: port == 20443,
        ):
            self.assertEqual(choose_port(None, ranges), 20443)
        with self.assertRaisesRegex(Exception, "reserved"):
            choose_port(19443, ranges)

    def test_candidate_selection_is_exact(self):
        candidate = IPv6Candidate("Ethernet", 7, PREFIX, "2606:4700:4700:1200::20")
        self.assertIs(
            _select_candidate([candidate], interface="Ethernet", prefix=PREFIX),
            candidate,
        )

    def test_cli_shape(self):
        arguments = build_parser().parse_args(["ipv6", "doctor", "--json"])
        self.assertEqual(arguments.command, "ipv6")
        self.assertEqual(arguments.ipv6_command, "doctor")
        self.assertTrue(arguments.json)

    def test_setup_relaunches_itself_through_uac(self):
        with mock.patch.object(
            ipv6_cli.os, "name", "nt"
        ), mock.patch.object(
            ipv6_cli.sys, "platform", "win32"
        ), mock.patch(
            "nexus_agent.ipv6_cli._is_administrator", return_value=False
        ), mock.patch(
            "nexus_agent.ipv6_cli._run_elevated", return_value=0
        ) as elevated:
            self.assertEqual(main(["ipv6", "setup", "--skip-self-test"]), 0)
        elevated.assert_called_once_with([
            "ipv6", "setup", "--skip-self-test",
        ])

    @unittest.skipUnless(sys.platform == "win32", "Windows Service test")
    def test_service_uses_isolated_machine_runtime(self):
        service_runtime = (
            r"C:\ProgramData\Nexus\addressd-runtimes\runtime-test"
            r"\Scripts\pythonservice.exe"
        )
        service_api = mock.Mock()
        service_api.SERVICE_AUTO_START = 2
        service_api.SERVICE_STOPPED = 1
        service_api.SERVICE_RUNNING = 4
        service_api.SERVICE_START_PENDING = 2
        service_api.SERVICE_STOP_PENDING = 3
        service_util = mock.Mock()
        service_util.QueryServiceStatus.side_effect = [
            (0, service_api.SERVICE_STOPPED),
            (0, service_api.SERVICE_RUNNING),
        ]
        modules = (
            types.SimpleNamespace(error=RuntimeError),
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            service_api,
            service_util,
            mock.Mock(),
        )
        with mock.patch(
            "nexus_agent.windows_service._require_windows_modules",
            return_value=modules,
        ), mock.patch(
            "nexus_agent.windows_service.install_service_runtime",
            return_value=service_runtime,
        ), mock.patch(
            "nexus_agent.windows_service.cleanup_service_runtimes"
        ) as cleanup:
            _ensure_service_running()

        self.assertEqual(
            service_util.ChangeServiceConfig.call_args.kwargs["exeName"],
            service_runtime,
        )
        service_util.StartService.assert_called_once_with("NexusAgentAddressd")
        cleanup.assert_called_once_with(service_runtime)

    @unittest.skipUnless(sys.platform == "win32", "Windows Service test")
    def test_manual_service_install_uses_isolated_machine_runtime(self):
        from nexus_agent import windows_service

        service_runtime = (
            r"C:\ProgramData\Nexus\addressd-runtimes\runtime-manual"
            r"\Scripts\pythonservice.exe"
        )
        observed = {}

        def handle_command(service_class, **_kwargs):
            observed["exe_name"] = service_class._exe_name_
            return 0

        with mock.patch.object(
            windows_service, "_require_windows_modules"
        ), mock.patch.object(
            windows_service,
            "install_service_runtime",
            return_value=service_runtime,
        ), mock.patch.object(
            windows_service.win32serviceutil,
            "HandleCommandLine",
            side_effect=handle_command,
        ) as command:
            self.assertEqual(windows_service.main(["install", "--startup", "auto"]), 0)

        service_class = command.call_args.args[0]
        self.assertEqual(observed["exe_name"], service_runtime)
        self.assertEqual(command.call_args.kwargs["argv"][1], "install")
        self.assertNotIn("_exe_name_", service_class.__dict__)


if __name__ == "__main__":
    unittest.main()
