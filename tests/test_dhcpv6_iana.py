import ipaddress
import pathlib
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock


SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent import (  # noqa: E402
    LocalAddressdClient,
    NexusAgent,
    UnixAddressdTransport,
)
from nexus_agent.addressd import (  # noqa: E402
    AddressdApplication,
    AddressdStateStore,
    AddressdUnixServer,
)
from nexus_agent.dhcpv6_iana import (  # noqa: E402
    ADVERTISE,
    OPTION_CLIENTID,
    OPTION_IAADDR,
    OPTION_IA_NA,
    OPTION_SERVERID,
    OPTION_STATUS_CODE,
    REBIND,
    RENEW,
    REPLY,
    STATUS_NO_ADDRS_AVAIL,
    STATUS_USE_MULTICAST,
    Dhcpv6Binding,
    Dhcpv6IaNaClient,
    Dhcpv6IaNaLinuxBackend,
    Dhcpv6LeaseError,
    Dhcpv6Reply,
    Dhcpv6ServerError,
    Dhcpv6TransientError,
    _normalize_lifetimes,
    _option,
    build_client_message,
    duid_uuid_from_machine_id,
    load_or_create_duid,
    parse_options,
    parse_server_ack,
    parse_server_message,
    stable_iaid,
)
from nexus_agent.host_alias import (  # noqa: E402
    HostAliasAllocator,
    LinuxAddressBackend,
)


CLIENT_ID = bytes.fromhex("0004") + bytes(range(16))
SERVER_ID = bytes.fromhex("0004") + bytes(reversed(range(16)))
ADDRESS = "2606:4700:4700:1200::100"
SECRET = b"dhcpv6-dynamic-host-alias-test-secret"


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


def server_message(
    message_type,
    transaction_id,
    iaid,
    *,
    address=ADDRESS,
    preferred=240,
    valid=300,
    t1=100,
    t2=200,
    status=None,
):
    options = [
        _option(OPTION_CLIENTID, CLIENT_ID),
        _option(OPTION_SERVERID, SERVER_ID),
    ]
    if status is not None:
        options.append(_option(OPTION_STATUS_CODE, struct.pack("!H", status)))
    else:
        iaaddr = ipaddress.IPv6Address(address).packed + struct.pack(
            "!II", preferred, valid
        )
        iana = struct.pack("!III", iaid, t1, t2) + _option(
            OPTION_IAADDR, iaaddr
        )
        options.append(_option(OPTION_IA_NA, iana))
    return bytes([message_type]) + transaction_id + b"".join(options)


class Dhcpv6ProtocolTest(unittest.TestCase):
    def test_builds_and_parses_one_ia_na(self):
        transaction_id = b"\x01\x02\x03"
        packet = build_client_message(
            1, transaction_id, client_id=CLIENT_ID, iaid=77
        )
        self.assertEqual(packet[:4], b"\x01" + transaction_id)
        self.assertIn(OPTION_IA_NA, parse_options(packet[4:]))

        reply = parse_server_message(
            server_message(ADVERTISE, transaction_id, 77),
            transaction_id=transaction_id,
            client_id=CLIENT_ID,
            expected_type=ADVERTISE,
            expected_iaid=77,
            server_address="fe80::1",
        )
        self.assertEqual(reply.address, ADDRESS)
        self.assertEqual(reply.server_id, SERVER_ID)
        self.assertEqual((reply.t1, reply.t2), (100, 200))

    def test_rejects_truncated_excessive_and_duplicate_options(self):
        with self.assertRaisesRegex(Dhcpv6LeaseError, "truncated"):
            parse_options(b"\x00\x01\x00")
        with self.assertRaisesRegex(Dhcpv6LeaseError, "too many"):
            parse_options(_option(99, b"") * 129)

        transaction_id = b"\x04\x05\x06"
        packet = server_message(ADVERTISE, transaction_id, 88)
        packet += _option(OPTION_SERVERID, SERVER_ID)
        with self.assertRaisesRegex(Dhcpv6LeaseError, "exactly once"):
            parse_server_message(
                packet,
                transaction_id=transaction_id,
                client_id=CLIENT_ID,
                expected_type=ADVERTISE,
                expected_iaid=88,
                server_address="fe80::1",
            )

    def test_surfaces_no_addresses_available(self):
        transaction_id = b"\x07\x08\x09"
        with self.assertRaisesRegex(
            Dhcpv6ServerError, "NoAddrsAvail"
        ) as caught:
            parse_server_message(

                server_message(
                    ADVERTISE,
                    transaction_id,
                    89,
                    status=STATUS_NO_ADDRS_AVAIL,
                ),
                transaction_id=transaction_id,
                client_id=CLIENT_ID,
                expected_type=ADVERTISE,
                expected_iaid=89,

                server_address="fe80::1",
            )
        self.assertEqual(caught.exception.code, STATUS_NO_ADDRS_AVAIL)

    def test_use_multicast_status_retries_selected_server_on_multicast(self):
        class FakeSocket:
            def __init__(self):
                self.sent = []
                self.responses = 0

            def sendto(self, packet, destination):
                self.sent.append((packet, destination))

            def settimeout(self, _timeout):
                pass

            def recvmsg(self, _packet_size, _control_size):
                transaction_id = self.sent[-1][0][1:4]
                self.responses += 1
                if self.responses == 1:
                    packet = server_message(
                        REPLY, transaction_id, 91, status=STATUS_USE_MULTICAST
                    )
                else:
                    packet = server_message(REPLY, transaction_id, 91)
                return packet, [], 0, ("fe80::1", 547, 0, 7)

            def close(self):
                pass

        connection = FakeSocket()
        client = Dhcpv6IaNaClient()
        client._interface_index = mock.Mock(return_value=7)
        client._socket = mock.Mock(return_value=connection)
        binding = Dhcpv6Binding.from_reply(
            Dhcpv6Reply(
                server_id=SERVER_ID,
                server_address="fe80::1",
                iaid=91,
                address=ADDRESS,
                preferred_lifetime=240,
                valid_lifetime=300,
                t1=100,
                t2=200,
            ),
            now=1000.0,
        )
        reply = client.renew(
            "eth0", client_id=CLIENT_ID, binding=binding, timeout=2.0
        )
        self.assertEqual(reply.address, ADDRESS)
        self.assertEqual(connection.sent[0][0][0], RENEW)
        self.assertIn(
            OPTION_SERVERID, parse_options(connection.sent[0][0][4:])
        )
        self.assertEqual(connection.sent[0][1][0], "fe80::1")
        self.assertEqual(connection.sent[1][1][0], "ff02::1:2")

    def test_rebind_wire_message_is_multicast_without_server_id(self):
        class FakeSocket:
            def __init__(self):
                self.sent = []

            def sendto(self, packet, destination):
                self.sent.append((packet, destination))

            def settimeout(self, _timeout):
                pass

            def recvmsg(self, _packet_size, _control_size):
                transaction_id = self.sent[-1][0][1:4]
                packet = server_message(REPLY, transaction_id, 92)
                return packet, [], 0, ("fe80::2", 547, 0, 7)

            def close(self):
                pass

        connection = FakeSocket()
        client = Dhcpv6IaNaClient()
        client._interface_index = mock.Mock(return_value=7)
        client._socket = mock.Mock(return_value=connection)
        binding = Dhcpv6Binding.from_reply(
            Dhcpv6Reply(
                server_id=SERVER_ID,
                server_address="fe80::1",
                iaid=92,
                address=ADDRESS,
                preferred_lifetime=240,
                valid_lifetime=300,
                t1=100,
                t2=200,
            ),
            now=1000.0,
        )
        reply = client.renew(
            "eth0",
            client_id=CLIENT_ID,
            binding=binding,
            rebind=True,
            timeout=2.0,
        )
        packet, destination = connection.sent[0]
        self.assertEqual(reply.address, ADDRESS)
        self.assertEqual(packet[0], REBIND)
        self.assertNotIn(OPTION_SERVERID, parse_options(packet[4:]))
        self.assertEqual(destination[0], "ff02::1:2")

    def test_release_ack_may_omit_ia_address(self):
        transaction_id = b"\x0a\x0b\x0c"
        packet = bytes([REPLY]) + transaction_id + b"".join((
            _option(OPTION_CLIENTID, CLIENT_ID),
            _option(OPTION_SERVERID, SERVER_ID),
            _option(OPTION_IA_NA, struct.pack("!III", 90, 0, 0)),
        ))
        parse_server_ack(
            packet,
            transaction_id=transaction_id,
            client_id=CLIENT_ID,
            expected_iaid=90,
            expected_server_id=SERVER_ID,
        )

    def test_duid_and_agent_iaid_are_stable_and_distinct(self):
        first = duid_uuid_from_machine_id("0123456789abcdef0123456789abcdef")
        second = duid_uuid_from_machine_id("0123456789abcdef0123456789abcdef")
        self.assertEqual(first, second)
        self.assertEqual(first[:2], b"\x00\x04")
        self.assertNotEqual(
            stable_iaid(first, "eth0", "uid:1000", "demo", "agent-a"),
            stable_iaid(first, "eth0", "uid:1000", "demo", "agent-b"),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "duid"
            machine = pathlib.Path(directory) / "machine-id"
            machine.write_text("0123456789abcdef0123456789abcdef\n")
            self.assertEqual(
                load_or_create_duid(str(path), machine_id_path=str(machine)),
                load_or_create_duid(str(path), machine_id_path=str(machine)),
            )

    def test_lifetime_defaults_remain_ordered(self):
        preferred, valid, t1, t2 = _normalize_lifetimes(240, 300, 0, 0)
        self.assertEqual((preferred, valid), (240, 300))
        self.assertLess(t1, t2)
        self.assertLess(t2, valid)


class FakeClient:
    def __init__(self):
        self.acquired = []
        self.renewed = []
        self.released = []
        self.fail_renew = None

    @staticmethod
    def _reply(iaid, address):
        return Dhcpv6Reply(
            server_id=SERVER_ID,
            server_address="fe80::1",
            iaid=iaid,
            address=address,
            preferred_lifetime=240,
            valid_lifetime=300,
            t1=100,
            t2=200,
        )

    def acquire(self, interface, *, client_id, iaid):
        del client_id
        self.acquired.append((interface, iaid))
        address = f"2606:4700:4700:1200::{100 + len(self.acquired)}"
        return self._reply(iaid, address)

    def renew(self, interface, *, client_id, binding, rebind=False):
        del client_id
        self.renewed.append((interface, binding.iaid, rebind))
        if self.fail_renew:
            raise self.fail_renew
        return self._reply(binding.iaid, binding.address)

    def release(self, interface, *, client_id, binding):
        del client_id
        self.released.append((interface, binding.iaid))

    def decline(self, interface, *, client_id, binding):
        raise AssertionError((interface, client_id, binding))


class Dhcpv6BackendTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.client = FakeClient()
        self.backend = Dhcpv6IaNaLinuxBackend(
            interface="eth0",
            client_id=CLIENT_ID,
            client=self.client,
            now=self.clock,
        )
        self.has_address = mock.patch.object(
            LinuxAddressBackend, "has_address", return_value=False
        )
        self.add_address = mock.patch.object(LinuxAddressBackend, "add_address")
        self.remove_address = mock.patch.object(
            LinuxAddressBackend, "remove_address"
        )
        self.has_address_mock = self.has_address.start()
        self.add_address.start()
        self.remove_address.start()
        self.backend._set_lifetimes = mock.Mock()

    def tearDown(self):
        self.has_address.stop()
        self.add_address.stop()
        self.remove_address.stop()

    def allocate(self, agent_id):
        return self.backend.allocate_address(
            "eth0",
            owner="uid:1000",
            tenant="demo",
            agent_id=agent_id,
            lease_seconds=300,
        )

    def test_allocates_distinct_ia_na_per_agent_and_releases(self):
        first = self.allocate("agent-a")
        second = self.allocate("agent-b")
        self.assertNotEqual(first, second)
        self.assertNotEqual(self.client.acquired[0][1], self.client.acquired[1][1])
        self.assertEqual(len(self.backend.snapshot_address(first)), 14)
        self.backend.remove_address("eth0", first)
        self.assertEqual(self.client.released[0][1], self.client.acquired[0][1])

    def test_t1_renew_uses_selected_server(self):
        address = self.allocate("agent-a")
        self.clock.value = 1101.0
        self.backend.refresh_address("eth0", address)
        self.assertEqual(
            self.client.renewed[-1],
            ("eth0", self.client.acquired[0][1], False),
        )

    def test_t2_rebind_uses_multicast_discovery_path(self):
        address = self.allocate("agent-a")
        binding = self.backend._bindings[address.compressed]
        self.backend._bindings[address.compressed] = Dhcpv6Binding(
            **{**binding.to_dict(), "renew_at": 1000.0, "rebind_at": 1100.0}
        )
        self.clock.value = 1101.0
        self.backend.refresh_address("eth0", address)
        self.assertEqual(
            self.client.renewed[-1],
            ("eth0", self.client.acquired[0][1], True),
        )

    @unittest.skipUnless(
        hasattr(socket, "AF_UNIX"), "Unix Socket restart acceptance"
    )
    def test_addressd_restart_restores_binding_without_new_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            socket_path = str(root / "addressd.sock")
            state = AddressdStateStore(str(root / "addressd-state.json"))
            allocator = HostAliasAllocator(
                backend=self.backend,
                interface="eth0",
                prefix="dynamic",
                allocation_secret=SECRET,
                now=self.clock,
            )
            application = AddressdApplication(allocator, state_store=state)
            server = AddressdUnixServer(
                socket_path, application, sweep_interval=0.05
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            local_client = LocalAddressdClient(
                UnixAddressdTransport(socket_path, timeout=2.0),
                owner="uid:1000",
            )
            try:
                lease = local_client.allocate(
                    tenant="demo",
                    agent_id="agent-a",
                    interface="auto",
                    prefix="auto",
                    lease_seconds=300,
                )
                lease.confirm()
            finally:
                server.shutdown()
                thread.join(timeout=2.0)
                server.server_close()
            self.assertFalse(thread.is_alive())

            self.has_address_mock.return_value = True
            restarted_backend = Dhcpv6IaNaLinuxBackend(
                interface="eth0",
                client_id=CLIENT_ID,
                client=self.client,
                now=self.clock,
            )
            restarted_backend._set_lifetimes = mock.Mock()
            restarted_allocator = HostAliasAllocator(
                backend=restarted_backend,
                interface="eth0",
                prefix="dynamic",
                allocation_secret=SECRET,
                now=self.clock,
            )
            restarted_application = AddressdApplication(
                restarted_allocator, state_store=state
            )
            restarted_server = AddressdUnixServer(
                socket_path, restarted_application, sweep_interval=0.05
            )
            restarted_thread = threading.Thread(
                target=restarted_server.serve_forever, daemon=True
            )
            restarted_thread.start()
            restarted_client = LocalAddressdClient(
                UnixAddressdTransport(socket_path, timeout=2.0),
                owner="uid:1000",
            )
            try:
                status = restarted_client.transport.call(
                    "status", {"owner": "uid:1000"}
                )
                self.assertEqual(status["leases"], 1)
                self.assertEqual(len(restarted_backend._bindings), 1)
                self.assertEqual(len(self.client.acquired), 1)
                self.assertEqual(self.client.renewed, [])
                restarted_client.release(lease.info.lease_id)
            finally:
                restarted_server.shutdown()
                restarted_thread.join(timeout=2.0)
                restarted_server.server_close()
            self.assertFalse(restarted_thread.is_alive())

    def test_agent_exit_releases_dhcpv6_binding(self):
        allocator = HostAliasAllocator(
            backend=self.backend,
            interface="eth0",
            prefix="dynamic",
            allocation_secret=SECRET,
            now=self.clock,
        )
        application = AddressdApplication(allocator)

        class InProcessTransport:
            def call(_self, method, parameters):
                return application.dispatch(
                    {"version": 1, "method": method, "params": parameters},
                    peer_owner="uid:1000",
                )

        local_client = LocalAddressdClient(
            InProcessTransport(), owner="uid:1000"
        )
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
                allocator=local_client,
                interface="eth0",
                prefix="dynamic",
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

        self.assertEqual(len(self.client.acquired), 1)
        self.assertEqual(len(self.client.released), 1)
        self.assertEqual(
            self.client.released[0][1], self.client.acquired[0][1]
        )
        self.assertEqual(allocator.list(), ())

    def test_transient_failure_keeps_valid_binding_then_expiry_withdraws(self):
        address = self.allocate("agent-a")
        self.client.fail_renew = Dhcpv6TransientError("timeout")
        self.clock.value = 1101.0
        self.backend.refresh_address("eth0", address)
        binding = self.backend._bindings[address.compressed]
        self.assertGreater(binding.retry_at, self.clock.value)

        self.clock.value = binding.valid_until + 1
        self.assertEqual(self.backend.tick(), (address.compressed,))
        self.assertNotIn(address.compressed, self.backend._bindings)


class DynamicAllocatorTest(unittest.TestCase):
    def test_allocator_records_exact_128_and_backend_metadata(self):
        backend = mock.Mock()
        backend.dynamic_addressing = True
        backend.mode = "dhcpv6-ia-na"
        backend.tick.return_value = ()
        backend.allocate_address.return_value = ipaddress.IPv6Address(ADDRESS)
        backend.snapshot_address.return_value = {"iaid": 77}
        allocator = HostAliasAllocator(
            backend=backend,
            interface="eth0",
            prefix="dynamic",
            allocation_secret=SECRET,
        )
        lease = allocator.allocate(
            owner="uid:1000", tenant="demo", agent_id="agent-a"
        )
        self.assertEqual(lease.address, ADDRESS)
        self.assertEqual(lease.prefix, ADDRESS + "/128")
        self.assertEqual(allocator.snapshot()[0]["backend"], {"iaid": 77})


if __name__ == "__main__":
    unittest.main()
