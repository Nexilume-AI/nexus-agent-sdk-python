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
from nexus_agent.host_alias import HostAliasError  # noqa: E402
from nexus_agent.linux_ipv6 import (  # noqa: E402
    LinuxIPv6Candidate,
    LinuxInstallPaths,
    _service_text,
    discover_linux_candidates,
    discover_upstream_relay_candidates,
    doctor_linux,
    install_linux_service,
    run_elevated_linux,
    select_linux_candidate,
)


PREFIX = "2606:4700:4700:1200::/64"
CANDIDATE = LinuxIPv6Candidate(
    interface="eth0",
    interface_index=7,
    prefix=PREFIX,
    current_address="2606:4700:4700:1200::20",
)


class LinuxIPv6DiscoveryTest(unittest.TestCase):
    def test_discovers_global_64_from_iproute2_json(self):
        backend = mock.Mock()
        backend.json.side_effect = [
            [
                {"dst": PREFIX, "dev": "eth0", "protocol": "static"},
                {"dst": "fe80::/64", "dev": "eth0"},
                {"dst": "default", "dev": "eth0"},
            ],
            [{
                "ifname": "eth0",
                "addr_info": [
                    {"family": "inet6", "local": "2606:4700:4700:1200::20"},
                    {"family": "inet6", "local": "fe80::1"},
                ],
            }],
        ]
        with mock.patch("nexus_agent.linux_ipv6._linux_only"), mock.patch(
            "nexus_agent.linux_ipv6.socket.if_nametoindex", return_value=7
        ):
            result = discover_linux_candidates(backend)
        self.assertEqual(result, (CANDIDATE,))
        backend.json.assert_has_calls([
            mock.call(["ip", "-j", "-6", "route", "show"]),
            mock.call([
                "ip", "-j", "-6", "address", "show", "dev", "eth0",
                "scope", "global",
            ]),
        ])

    def test_ra_route_is_not_misclassified_as_owned_routed_prefix(self):
        backend = mock.Mock()
        backend.json.return_value = [
            {"dst": PREFIX, "dev": "eth0", "protocol": "ra"},
        ]
        with mock.patch("nexus_agent.linux_ipv6._linux_only"):
            self.assertEqual(discover_linux_candidates(backend), ())
        backend.json.assert_called_once_with(["ip", "-j", "-6", "route", "show"])

    def test_discovers_upstream_ra_prefix_from_host_with_only_a_128(self):
        backend = mock.Mock()
        backend.json.return_value = [{
            "ifname": "eth0",
            "addr_info": [{
                "family": "inet6",
                "local": "240d:c000:f020:900:c4e8:85d4:7644:0",
                "prefixlen": 128,
            }],
        }]
        advertisement = mock.Mock(
            prefix=PREFIX,
            interface_index=7,
            router="fe80::1",
            autonomous=True,
            valid_lifetime=1800,
            preferred_lifetime=900,
        )
        discovery = mock.Mock()
        discovery.discover.return_value = [advertisement]
        with mock.patch("nexus_agent.linux_ipv6._linux_only"):
            result = discover_upstream_relay_candidates(
                backend, discovery=discovery, interface="eth0"
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].mode, "upstream-relay")
        self.assertEqual(result[0].prefix, PREFIX)
        self.assertEqual(result[0].current_address,
                         "240d:c000:f020:900:c4e8:85d4:7644:0")
        discovery.discover.assert_called_once_with("eth0")

    def test_explicit_selection_is_exact(self):
        selected = select_linux_candidate(
            [CANDIDATE], interface="eth0", prefix=PREFIX
        )
        self.assertIs(selected, CANDIDATE)
        with self.assertRaisesRegex(HostAliasError, "global IPv6 /64"):
            select_linux_candidate(
                [CANDIDATE], interface="eth0", prefix="fd00::/64"
            )


class LinuxIPv6InstallTest(unittest.TestCase):
    def test_service_is_machine_owned_and_hardened(self):
        paths = LinuxInstallPaths(
            config=pathlib.Path("/etc/nexus-agent/addressd.json"),
            service=pathlib.Path("/etc/systemd/system/nexus-agent-addressd.service"),
            runtime=pathlib.Path("/opt/nexus-agent/addressd-runtime"),
        )
        unit = _service_text(CANDIDATE, "/usr/bin/python3", paths)
        self.assertIn("Environment=PYTHONPATH=/opt/nexus-agent/addressd-runtime", unit)
        self.assertIn("--interface eth0 --prefix " + PREFIX, unit)
        self.assertIn("CapabilityBoundingSet=CAP_NET_ADMIN", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        # Without CAP_CHOWN, root can assign the socket only to its own group.
        self.assertIn("Group=nexus-agent\n", unit)
        self.assertNotIn("CAP_CHOWN", unit)
        self.assertIn("ProtectSystem=strict", unit)

    def test_upstream_relay_service_has_raw_socket_capability_and_flag(self):
        paths = LinuxInstallPaths(
            config=pathlib.Path("/etc/nexus-agent/addressd.json"),
            service=pathlib.Path("/etc/systemd/system/nexus-agent-addressd.service"),
            runtime=pathlib.Path("/opt/nexus-agent/addressd-runtime"),
        )
        candidate = LinuxIPv6Candidate(
            interface="eth0", interface_index=7, prefix=PREFIX,
            current_address="240d:c000:f020:900::1", mode="upstream-relay",
            upstream_router="fe80::1", valid_lifetime=1800,
            preferred_lifetime=900,
        )
        unit = _service_text(candidate, "/usr/bin/python3", paths)
        self.assertIn("--upstream-relay", unit)
        self.assertIn("CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW", unit)
        self.assertIn("AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW", unit)

    def test_install_writes_runtime_config_and_enables_service(self):
        backend = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            paths = LinuxInstallPaths(
                config=root / "etc" / "addressd.json",
                service=root / "systemd" / "nexus-agent-addressd.service",
                runtime=root / "opt" / "addressd-runtime",
            )
            with mock.patch("nexus_agent.linux_ipv6._linux_only"), mock.patch(
                "nexus_agent.linux_ipv6.os.geteuid", return_value=0, create=True
            ), mock.patch(
                "nexus_agent.linux_ipv6._existing_leases", return_value=0
            ), mock.patch(
                "nexus_agent.linux_ipv6._runtime_python", return_value="/usr/bin/python3"
            ):
                install_linux_service(
                    CANDIDATE,
                    port=9443,
                    allowed_users=[],
                    backend=backend,
                    paths=paths,
                )

            configuration = json.loads(paths.config.read_text(encoding="utf-8"))
            self.assertEqual(configuration["interface"], "eth0")
            self.assertEqual(configuration["prefix"], PREFIX)
            self.assertEqual(configuration["recommended_port"], 9443)
            self.assertTrue((paths.runtime / "nexus_agent" / "addressd.py").is_file())
            backend.run.assert_has_calls([
                mock.call(["systemctl", "stop", "nexus-agent-addressd.service"], check=False),
                mock.call(["groupadd", "--system", "--force", "nexus-agent"]),
                mock.call(["systemctl", "daemon-reload"]),
                mock.call([
                    "systemctl", "enable", "--now", "nexus-agent-addressd.service"
                ]),
            ])

    def test_active_leases_block_reconfiguration(self):
        with mock.patch("nexus_agent.linux_ipv6._linux_only"), mock.patch(
            "nexus_agent.linux_ipv6.os.geteuid", return_value=0, create=True
        ), mock.patch(
            "nexus_agent.linux_ipv6._existing_leases", return_value=2
        ):
            with self.assertRaisesRegex(HostAliasError, "active leases"):
                install_linux_service(
                    CANDIDATE, port=9443, allowed_users=[], backend=mock.Mock()
                )

    def test_non_root_relaunch_uses_current_python_and_sudo(self):
        completed = mock.Mock(returncode=0)
        with mock.patch("nexus_agent.linux_ipv6._linux_only"), mock.patch(
            "nexus_agent.linux_ipv6.shutil.which", return_value="/usr/bin/sudo"
        ), mock.patch(
            "nexus_agent.linux_ipv6.subprocess.run", return_value=completed
        ) as run:
            self.assertEqual(
                run_elevated_linux(["ipv6", "setup", "--skip-self-test"]), 0
            )
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/bin/sudo", sys.executable, "-c"])
        self.assertIn("sys.path.insert", command[3])
        self.assertIn("from nexus_agent.ipv6_cli import main", command[3])
        self.assertEqual(command[4:], ["ipv6", "setup", "--skip-self-test"])
        self.assertEqual(run.call_args.kwargs, {"check": False, "shell": False})


class LinuxIPv6DoctorTest(unittest.TestCase):
    def test_doctor_checks_systemd_prefix_socket_port_and_group(self):
        command = mock.Mock()
        command.run.return_value = "active\n"
        address_backend = mock.Mock()
        address_backend.prefix_ready.return_value = True
        transport = mock.Mock()
        transport.call.return_value = {"leases": 1, "max_addresses": 256}
        arguments = argparse.Namespace(json=False)
        with mock.patch("nexus_agent.linux_ipv6._linux_only"), mock.patch(
            "nexus_agent.linux_ipv6._load_configuration",
            return_value={
                "interface": "eth0",
                "prefix": PREFIX,
                "socket": "/run/nexus-agent/addressd.sock",
                "socket_group": "nexus-agent",
                "recommended_port": 9443,
            },
        ), mock.patch(
            "nexus_agent.linux_ipv6.LinuxCommandBackend", return_value=command
        ), mock.patch(
            "nexus_agent.linux_ipv6.LinuxAddressBackend", return_value=address_backend
        ), mock.patch(
            "nexus_agent.linux_ipv6.UnixAddressdTransport", return_value=transport
        ), mock.patch(
            "nexus_agent.linux_ipv6._current_user_in_group", return_value=True
        ), mock.patch(
            "nexus_agent.linux_ipv6.os.geteuid", return_value=1000, create=True
        ):
            self.assertEqual(doctor_linux(arguments), 0)

    def test_doctor_reads_upstream_relay_health_from_privileged_daemon(self):
        command = mock.Mock()
        command.run.return_value = "active\n"
        transport = mock.Mock()
        transport.call.return_value = {
            "leases": 2,
            "max_addresses": 256,
            "mode": "upstream-relay",
            "upstream_relay": {
                "ready": True,
                "router": "fe80::1",
                "valid_lifetime": 1800,
                "preferred_lifetime": 900,
            },
        }
        arguments = argparse.Namespace(json=False)
        with mock.patch("nexus_agent.linux_ipv6._linux_only"), mock.patch(
            "nexus_agent.linux_ipv6._load_configuration",
            return_value={
                "mode": "upstream-relay",
                "interface": "eth0",
                "prefix": PREFIX,
                "socket": "/run/nexus-agent/addressd.sock",
                "socket_group": "nexus-agent",
                "recommended_port": 9443,
            },
        ), mock.patch(
            "nexus_agent.linux_ipv6.LinuxCommandBackend", return_value=command
        ), mock.patch(
            "nexus_agent.linux_ipv6.LinuxAddressBackend"
        ) as address_backend, mock.patch(
            "nexus_agent.linux_ipv6.UnixAddressdTransport", return_value=transport
        ), mock.patch(
            "nexus_agent.linux_ipv6._current_user_in_group", return_value=True
        ), mock.patch(
            "nexus_agent.linux_ipv6.os.geteuid", return_value=1000, create=True
        ):
            self.assertEqual(doctor_linux(arguments), 0)
        address_backend.assert_not_called()

    def test_unified_cli_dispatches_linux_doctor(self):
        with mock.patch.object(ipv6_cli.os, "name", "posix"), mock.patch.object(
            ipv6_cli.sys, "platform", "linux"
        ), mock.patch(
            "nexus_agent.linux_ipv6.doctor_linux", return_value=0
        ) as doctor:
            self.assertEqual(ipv6_cli.main(["ipv6", "doctor", "--json"]), 0)
        doctor.assert_called_once()


if __name__ == "__main__":
    unittest.main()
