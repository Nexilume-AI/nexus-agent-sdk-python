import ipaddress
import os
import pathlib
import socket
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock


SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent import (  # noqa: E402
    HostAliasAllocator,
    HostAliasError,
    LocalAddressdClient,
    MemoryAddressBackend,
    NexusAgent,
    NoServerAuth,
    UnixAddressdTransport,
    WindowsAddressBackend,
    WindowsNamedPipeTransport,
    default_addressd_transport,
)
from nexus_agent.addressd import (  # noqa: E402
    AddressdApplication,
    AddressdStateStore,
    AddressdUnixServer,
)
from nexus_agent.windows_pipe import AddressdNamedPipeServer  # noqa: E402


PREFIX = "2606:4700:4700:1200::/64"
SECRET = b"p8.24-host-alias-allocation-secret"


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


class HostAliasAllocatorTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.backend = MemoryAddressBackend([("eth0", PREFIX)])
        self.allocator = HostAliasAllocator(
            backend=self.backend,
            interface="eth0",
            prefix=PREFIX,
            allocation_secret=SECRET,
            default_lease_seconds=300,
            reservation_seconds=15,
            now=self.clock,
        )

    def allocate(self, agent_id="agent-a", owner="uid:1000"):
        return self.allocator.allocate(
            owner=owner,
            tenant="demo",
            agent_id=agent_id,
        )

    def test_stable_distinct_addresses_share_one_interface(self):
        first = self.allocate("agent-a")
        second = self.allocate("agent-b")
        self.assertNotEqual(first.address, second.address)
        self.assertEqual(first.interface, "eth0")
        self.assertEqual(second.interface, "eth0")
        self.assertEqual(len(self.backend.addresses), 2)
        self.assertEqual(self.allocate("agent-a").lease_id, first.lease_id)

        self.allocator.release(first.lease_id, owner="uid:1000")
        replacement = self.allocate("agent-a")
        self.assertEqual(replacement.address, first.address)
        self.assertNotEqual(replacement.lease_id, first.lease_id)

    def test_two_phase_confirm_renew_and_release(self):
        reserved = self.allocate()
        self.assertEqual(reserved.state, "reserved")
        active = self.allocator.confirm(reserved.lease_id, owner="uid:1000")
        self.assertEqual(active.state, "active")
        self.assertEqual(active.expires_at, 1300.0)
        self.clock.value = 1100.0
        renewed = self.allocator.renew(reserved.lease_id, owner="uid:1000")
        self.assertEqual(renewed.expires_at, 1400.0)
        self.allocator.release(reserved.lease_id, owner="uid:1000")
        self.assertEqual(self.backend.addresses, set())

    def test_renew_refreshes_backend_address_when_supported(self):
        backend = mock.Mock(wraps=self.backend)
        backend.refresh_address = mock.Mock()
        allocator = HostAliasAllocator(
            backend=backend,
            interface="eth0",
            prefix=PREFIX,
            allocation_secret=SECRET,
            now=self.clock,
        )
        lease = allocator.allocate(
            owner="uid:1000", tenant="demo", agent_id="agent-a"
        )
        allocator.confirm(lease.lease_id, owner="uid:1000")
        allocator.renew(lease.lease_id, owner="uid:1000")
        backend.refresh_address.assert_called_once_with(
            "eth0", ipaddress.IPv6Address(lease.address)
        )

    def test_unconfirmed_and_crashed_leases_are_reclaimed(self):
        reserved = self.allocate()
        self.clock.value = reserved.expires_at + 0.1
        self.assertEqual(self.allocator.sweep(), 1)
        self.assertEqual(self.backend.addresses, set())
        self.assertEqual(self.allocator.list(), ())

    def test_owner_and_allowed_network_are_enforced(self):
        reserved = self.allocate()
        with self.assertRaisesRegex(HostAliasError, "another local user"):
            self.allocator.confirm(reserved.lease_id, owner="uid:2000")
        with self.assertRaisesRegex(HostAliasError, "interface"):
            self.allocator.allocate(
                owner="uid:1000",
                tenant="demo",
                agent_id="agent-c",
                interface="wan0",
            )
        with self.assertRaisesRegex(HostAliasError, "prefix"):
            self.allocator.allocate(
                owner="uid:1000",
                tenant="demo",
                agent_id="agent-c",
                prefix="2606:4700:4700:1300::/64",
            )

    def test_active_state_restores_without_duplicate_address(self):
        lease = self.allocate()
        active = self.allocator.confirm(lease.lease_id, owner="uid:1000")
        snapshot = self.allocator.snapshot()
        restored = HostAliasAllocator(
            backend=self.backend,
            interface="eth0",
            prefix=PREFIX,
            allocation_secret=SECRET,
            now=self.clock,
        )
        self.assertEqual(restored.restore(snapshot), 1)
        self.assertEqual(restored.list()[0], active)
        self.assertEqual(len(self.backend.addresses), 1)

    def test_restart_withdraws_active_address_when_backend_refresh_fails(self):
        lease = self.allocate()
        self.allocator.confirm(lease.lease_id, owner="uid:1000")
        snapshot = self.allocator.snapshot()
        backend = mock.Mock(wraps=self.backend)
        backend.refresh_address = mock.Mock(
            side_effect=HostAliasError("upstream RA prefix was withdrawn")
        )
        restarted = HostAliasAllocator(
            backend=backend,
            interface="eth0",
            prefix=PREFIX,
            allocation_secret=SECRET,
            now=self.clock,
        )
        self.assertEqual(restarted.restore(snapshot), 0)
        self.assertEqual(self.backend.addresses, set())
        self.assertEqual(restarted.list(), ())

    def test_restart_cleans_journaled_unconfirmed_address(self):
        reserved = self.allocate()
        snapshot = self.allocator.snapshot()
        restarted = HostAliasAllocator(
            backend=self.backend,
            interface="eth0",
            prefix=PREFIX,
            allocation_secret=SECRET,
            now=self.clock,
        )
        self.assertEqual(restarted.restore(snapshot), 0)
        self.assertNotIn(("eth0", reserved.address), self.backend.addresses)


class AddressdProtocolTest(unittest.TestCase):
    def setUp(self):
        self.backend = MemoryAddressBackend([("eth0", PREFIX)])
        self.allocator = HostAliasAllocator(
            backend=self.backend,
            interface="eth0",
            prefix=PREFIX,
            allocation_secret=SECRET,
        )
        self.application = AddressdApplication(self.allocator)

    @staticmethod
    def request(method, **parameters):
        return {"version": 1, "method": method, "params": parameters}

    def test_status_exposes_backend_mode_and_relay_health(self):
        self.backend.mode = "upstream-relay"
        self.backend.relay_status = mock.Mock(return_value={
            "mode": "upstream-relay",
            "ready": True,
            "router": "fe80::1",
        })
        status = self.application.dispatch(
            self.request("status"), peer_owner="uid:1000"
        )
        self.assertEqual(status["mode"], "upstream-relay")
        self.assertTrue(status["upstream_relay"]["ready"])
        self.assertEqual(status["upstream_relay"]["router"], "fe80::1")

    def test_peer_identity_overrides_spoofed_owner(self):
        lease = self.application.dispatch(
            self.request(
                "allocate",
                owner="uid:attacker",
                tenant="demo",
                agent_id="agent-a",
                interface="auto",
                prefix="auto",
                lease_seconds=300,
            ),
            peer_owner="uid:1000",
        )
        with self.assertRaisesRegex(HostAliasError, "another local user"):
            self.application.dispatch(
                self.request(
                    "confirm_bound",
                    owner="uid:1000",
                    lease_id=lease["lease_id"],
                ),
                peer_owner="uid:2000",
            )
        confirmed = self.application.dispatch(
            self.request("confirm_bound", lease_id=lease["lease_id"]),
            peer_owner="uid:1000",
        )
        self.assertEqual(confirmed["state"], "active")

    def test_state_store_journals_reserved_and_active_leases(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AddressdStateStore(os.path.join(directory, "state.json"))
            application = AddressdApplication(self.allocator, state_store=store)
            lease = application.dispatch(
                self.request(
                    "allocate",
                    tenant="demo",
                    agent_id="agent-a",
                    interface="auto",
                    prefix="auto",
                    lease_seconds=300,
                ),
                peer_owner="uid:1000",
            )
            self.assertEqual(store.load()[0]["state"], "reserved")
            application.dispatch(
                self.request("confirm_bound", lease_id=lease["lease_id"]),
                peer_owner="uid:1000",
            )
            self.assertEqual(len(store.load()), 1)
            self.assertEqual(store.load()[0]["state"], "active")

    def test_failed_backend_refresh_withdraws_address_and_persisted_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AddressdStateStore(os.path.join(directory, "state.json"))
            application = AddressdApplication(self.allocator, state_store=store)
            lease = application.dispatch(
                self.request(
                    "allocate", tenant="demo", agent_id="agent-a",
                    interface="auto", prefix="auto", lease_seconds=300,
                ),
                peer_owner="uid:1000",
            )
            application.dispatch(
                self.request("confirm_bound", lease_id=lease["lease_id"]),
                peer_owner="uid:1000",
            )
            self.backend.refresh_address = mock.Mock(
                side_effect=HostAliasError("upstream RA prefix was withdrawn")
            )
            with self.assertRaisesRegex(HostAliasError, "withdrawn"):
                application.dispatch(
                    self.request("renew", lease_id=lease["lease_id"]),
                    peer_owner="uid:1000",
                )
            self.assertEqual(self.backend.addresses, set())
            self.assertEqual(self.allocator.list(), ())
            self.assertEqual(store.load(), [])

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "AF_UNIX unavailable")
    def test_real_local_ipc_allocate_confirm_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = os.path.join(directory, "addressd.sock")
            server = AddressdUnixServer(socket_path, self.application)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = LocalAddressdClient(
                    UnixAddressdTransport(socket_path), owner="test-client"
                )
                lease = client.allocate(
                    tenant="demo", agent_id="agent-a", lease_seconds=300
                )
                self.assertEqual(lease.info.state, "reserved")
                self.assertEqual(lease.confirm().state, "active")
                self.assertEqual(lease.renew().state, "active")
                lease.close()
                self.assertEqual(self.backend.addresses, set())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class PublicIPv6AutoAddressTest(unittest.TestCase):
    def test_invalid_static_options_do_not_allocate_an_address(self):
        fake_client = mock.Mock()
        with self.assertRaisesRegex(ValueError, "port"):
            NexusAgent.public_ipv6(
                "auto",
                allocator=fake_client,
                port=0,
                auth="none",
                tenant="demo",
                agent_id="agent-a",
            )
        fake_client.allocate.assert_not_called()

    def test_invalid_allocated_address_is_released_immediately(self):
        fake_lease = mock.Mock()
        fake_lease.address = "fd00::1"
        fake_client = mock.Mock()
        fake_client.allocate.return_value = fake_lease
        with self.assertRaisesRegex(ValueError, "global IPv6"):
            NexusAgent.public_ipv6(
                "auto",
                allocator=fake_client,
                auth="none",
                tenant="demo",
                agent_id="agent-a",
            )
        fake_lease.close.assert_called_once_with()

    def test_address_auto_allocates_confirms_and_releases_host_alias(self):
        fake_lease = mock.Mock()
        fake_lease.address = "2606:4700:4700:1200::1234"
        fake_client = mock.Mock()
        fake_client.allocate.return_value = fake_lease
        fake_server = mock.Mock()
        fake_server.port = 9443
        fake_server.is_healthy.return_value = True
        fake_thread = mock.Mock()
        fake_thread.is_alive.return_value = False
        fake_server.serve_in_thread.return_value = fake_thread

        with mock.patch(
            "nexus_agent.public_ipv6_agent.NexusAgentServer",
            return_value=fake_server,
        ):
            agent = NexusAgent.public_ipv6(
                "auto",
                allocator=fake_client,
                interface="eth0",
                prefix=PREFIX,
                port=9443,
                auth="none",
                tenant="demo",
                agent_id="agent-a",
            )

            @agent.capability("demo.echo")
            def echo(payload):
                return payload

            handle = agent.start(announce=False)
            handle.close()

        fake_client.allocate.assert_called_once_with(
            tenant="demo",
            agent_id="agent-a",
            interface="eth0",
            prefix=PREFIX,
            lease_seconds=300,
        )
        fake_lease.confirm.assert_called_once_with()
        fake_lease.start_auto_renew.assert_called_once_with()
        fake_lease.close.assert_called_once_with()
        self.assertEqual(
            agent.endpoint.url,
            "http://[2606:4700:4700:1200::1234]:9443",
        )


@unittest.skipUnless(os.name == "nt", "Windows-only addressd transport")
class WindowsAddressdTest(unittest.TestCase):
    def test_default_transport_is_named_pipe(self):
        self.assertIsInstance(
            default_addressd_transport(), WindowsNamedPipeTransport
        )

    def test_named_pipe_allocate_confirm_renew_and_release(self):
        backend = MemoryAddressBackend([("Ethernet", PREFIX)])
        allocator = HostAliasAllocator(
            backend=backend,
            interface="Ethernet",
            prefix=PREFIX,
            allocation_secret=SECRET,
        )
        application = AddressdApplication(allocator)
        pipe_name = r"\\.\pipe\nexus-addressd-test-" + uuid.uuid4().hex
        server = AddressdNamedPipeServer(
            pipe_name,
            application,
            allowed_group=None,
            sweep_interval=0.05,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = LocalAddressdClient(
                WindowsNamedPipeTransport(pipe_name, timeout=2.0),
                owner="spoofed-owner",
            )
            lease = client.allocate(
                tenant="demo", agent_id="windows-agent", lease_seconds=30
            )
            self.assertEqual(lease.info.state, "reserved")
            self.assertEqual(lease.confirm().state, "active")
            self.assertEqual(lease.renew().state, "active")
            stored = allocator.list()
            self.assertEqual(len(stored), 1)
            lease.close()
            self.assertEqual(backend.addresses, set())
        finally:
            server.shutdown()
            thread.join(timeout=2.0)
            server.server_close()
        self.assertFalse(thread.is_alive())

    def test_windows_route_parser_uses_interface_index(self):
        backend = WindowsAddressBackend()
        route_output = (
            "Publish  Type      Met  Prefix                    Idx  Gateway\n"
            "No       Manual    256  2606:4700:4700:1200::/64  27   Ethernet\n"
        )
        with mock.patch("socket.if_nametoindex", return_value=27), mock.patch.object(
            backend, "_run", return_value=route_output
        ):
            self.assertTrue(
                backend.prefix_ready("Ethernet", ipaddress.IPv6Network(PREFIX))
            )

    def test_hyperv_friendly_name_falls_back_to_netsh_interface_table(self):
        backend = WindowsAddressBackend()
        interface_output = (
            "Idx Met MTU State Name\n"
            "96 20 1500 connected vEthernet (Nexus-P817-External-WAN)\n"
        )
        route_output = (
            "Publish Type Met Prefix Idx Gateway\n"
            "No Manual 256 2606:4700:4700:1200::/64 96 Ethernet\n"
        )
        with mock.patch(
            "socket.if_nametoindex", side_effect=OSError("friendly name unavailable")
        ), mock.patch.object(
            backend, "_run", side_effect=[interface_output, route_output]
        ):
            self.assertTrue(
                backend.prefix_ready(
                    "vEthernet (Nexus-P817-External-WAN)",
                    ipaddress.IPv6Network(PREFIX),
                )
            )

    def test_windows_adds_exact_128_and_waits_until_bindable(self):
        backend = WindowsAddressBackend()
        address = ipaddress.IPv6Address("2606:4700:4700:1200::1234")
        probe = mock.Mock()
        with mock.patch.object(
            backend,
            "_run",
            side_effect=["", f"Address {address} Parameters\nDAD State: Preferred"],
        ) as run, mock.patch("socket.socket", return_value=probe):
            backend.add_address("Ethernet", address)
        self.assertIn(f"address={address}/128", run.call_args_list[0].args[0])
        probe.bind.assert_called_once_with((str(address), 0))
        probe.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
