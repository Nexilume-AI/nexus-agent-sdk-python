import argparse
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent import ipv6_cli  # noqa: E402
from nexus_agent.dhcpv6_iana import Dhcpv6Reply  # noqa: E402
from nexus_agent.linux_ipv6 import (  # noqa: E402
    LinuxIPv6Candidate,
    LinuxInstallPaths,
    _service_text,
    discover_dhcpv6_ia_na_candidates,
    doctor_linux,
    install_linux_service,
)


ADDRESS = "2606:4700:4700:1200::123"
CLIENT_ID = bytes.fromhex("0004") + bytes(range(16))


def candidate():
    return LinuxIPv6Candidate(
        interface="eth0",
        interface_index=7,
        prefix=ADDRESS + "/128",
        current_address=ADDRESS,
        mode="dhcpv6-ia-na",
        upstream_router="fe80::1",
        valid_lifetime=3600,
        preferred_lifetime=1800,
    )


class LinuxDhcpv6DiscoveryTest(unittest.TestCase):
    def test_probes_default_route_interface_without_committing_binding(self):
        backend = mock.Mock()
        backend.json.return_value = [{"dst": "default", "dev": "eth0"}]
        client = mock.Mock()
        client.probe.return_value = Dhcpv6Reply(
            server_id=b"server-id",
            server_address="fe80::1",
            iaid=99,
            address=ADDRESS,
            preferred_lifetime=1800,
            valid_lifetime=3600,
            t1=900,
            t2=1440,
        )
        with mock.patch("nexus_agent.linux_ipv6._linux_only"), mock.patch(
            "nexus_agent.linux_ipv6.socket.if_nametoindex", return_value=7
        ):
            result = discover_dhcpv6_ia_na_candidates(
                backend, client=client, client_id=CLIENT_ID
            )
        self.assertEqual(result, (candidate(),))
        client.probe.assert_called_once()
        self.assertEqual(client.probe.call_args.args[0], "eth0")
        backend.json.assert_called_once_with(
            ["ip", "-j", "-6", "route", "show", "default"]
        )

    def test_server_absence_is_a_clean_empty_candidate_set(self):
        backend = mock.Mock()
        backend.json.return_value = [{"dst": "default", "dev": "eth0"}]
        client = mock.Mock()
        from nexus_agent.host_alias import HostAliasError

        client.probe.side_effect = HostAliasError("no DHCPv6 server")
        errors = []
        with mock.patch("nexus_agent.linux_ipv6._linux_only"):
            self.assertEqual(
                discover_dhcpv6_ia_na_candidates(
                    backend, client=client, client_id=CLIENT_ID, errors=errors
                ),
                (),
            )
        self.assertEqual(errors, ["eth0: no DHCPv6 server"])


class LinuxDhcpv6InstallTest(unittest.TestCase):
    def test_service_uses_dynamic_backend_and_required_capabilities(self):
        paths = LinuxInstallPaths(
            config=pathlib.Path("/etc/nexus-agent/addressd.json"),
            service=pathlib.Path(
                "/etc/systemd/system/nexus-agent-addressd.service"
            ),
            runtime=pathlib.Path("/opt/nexus-agent/addressd-runtime"),
        )
        unit = _service_text(candidate(), "/usr/bin/python3", paths)
        self.assertIn("--prefix dynamic", unit)
        self.assertIn("--dhcpv6-ia-na", unit)
        self.assertIn(
            "CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW "
            "CAP_NET_BIND_SERVICE",
            unit,
        )
        self.assertIn(
            "ReadWritePaths=/etc/nexus-agent /var/lib/nexus-agent ", unit
        )

    def test_install_persists_dynamic_mode_and_stages_client_module(self):
        backend = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            paths = LinuxInstallPaths(
                config=root / "etc" / "addressd.json",
                service=root / "systemd" / "addressd.service",
                runtime=root / "opt" / "addressd-runtime",
            )
            with mock.patch("nexus_agent.linux_ipv6._linux_only"), mock.patch(
                "nexus_agent.linux_ipv6.os.geteuid", return_value=0, create=True
            ), mock.patch(
                "nexus_agent.linux_ipv6._existing_leases", return_value=0
            ), mock.patch(
                "nexus_agent.linux_ipv6._runtime_python",
                return_value="/usr/bin/python3",
            ):
                install_linux_service(
                    candidate(),
                    port=9443,
                    allowed_users=[],
                    backend=backend,
                    paths=paths,
                )
            configuration = json.loads(paths.config.read_text(encoding="utf-8"))
            self.assertEqual(configuration["mode"], "dhcpv6-ia-na")
            self.assertEqual(configuration["prefix"], "dynamic")
            self.assertEqual(configuration["offered_address"], ADDRESS)
            self.assertTrue(
                (paths.runtime / "nexus_agent" / "dhcpv6_iana.py").is_file()
            )


class LinuxDhcpv6DoctorTest(unittest.TestCase):
    def test_doctor_uses_privileged_daemon_probe(self):
        command = mock.Mock()
        command.run.return_value = "active\n"
        transport = mock.Mock()
        transport.call.return_value = {
            "leases": 2,
            "max_addresses": 256,
            "mode": "dhcpv6-ia-na",
            "address_backend": {
                "mode": "dhcpv6-ia-na",
                "ready": True,
                "bindings": 2,
                "servers": ["fe80::1"],
                "last_error": "",
            },
        }
        with mock.patch("nexus_agent.linux_ipv6._linux_only"), mock.patch(
            "nexus_agent.linux_ipv6._load_configuration",
            return_value={
                "mode": "dhcpv6-ia-na",
                "interface": "eth0",
                "prefix": "dynamic",
                "socket": "/run/nexus-agent/addressd.sock",
                "socket_group": "nexus-agent",
                "recommended_port": 9443,
            },
        ), mock.patch(
            "nexus_agent.linux_ipv6.LinuxCommandBackend", return_value=command
        ), mock.patch(
            "nexus_agent.linux_ipv6.UnixAddressdTransport",
            return_value=transport,
        ), mock.patch(
            "nexus_agent.linux_ipv6._current_user_in_group", return_value=True
        ), mock.patch(
            "nexus_agent.linux_ipv6.os.geteuid", return_value=1000, create=True
        ), mock.patch(
            "nexus_agent.linux_ipv6.LinuxAddressBackend"
        ) as static_backend:
            self.assertEqual(doctor_linux(argparse.Namespace(json=False)), 0)
        static_backend.assert_not_called()
        transport.call.assert_called_once_with(
            "status", {"owner": "doctor", "probe": True}
        )

    def test_cli_exposes_explicit_dhcpv6_mode(self):
        arguments = ipv6_cli.build_parser().parse_args(
            ["ipv6", "setup", "--mode", "dhcpv6-ia-na", "--skip-self-test"]
        )
        self.assertEqual(arguments.mode, "dhcpv6-ia-na")


if __name__ == "__main__":
    unittest.main()
