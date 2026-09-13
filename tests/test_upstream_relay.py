import ipaddress
import pathlib
import socket
import struct
import sys
import unittest
from unittest import mock


SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent.host_alias import HostAliasError, LinuxAddressBackend  # noqa: E402
from nexus_agent.upstream_relay import (  # noqa: E402
    PIO_AUTONOMOUS,
    PIO_ON_LINK,
    RouterAdvertisementDiscovery,
    RouterAdvertisementPrefix,
    UpstreamRelayLinuxBackend,
    parse_router_advertisement,
)


PREFIX = ipaddress.IPv6Network("2606:4700:4700:1200::/64")
ADDRESS = ipaddress.IPv6Address("2606:4700:4700:1200::a123")


def ra_packet(*, prefix_length=64, flags=PIO_ON_LINK | PIO_AUTONOMOUS,
              valid=1800, preferred=900, prefix=PREFIX):
    header = struct.pack("!BBHBBHII", 134, 0, 0, 64, 0, 1800, 0, 0)
    option = struct.pack(
        "!BBBBIII16s",
        3, 4, prefix_length, flags, valid, preferred, 0,
        prefix.network_address.packed,
    )
    return header + option


class RouterAdvertisementParserTest(unittest.TestCase):
    def test_accepts_exact_autonomous_global_64_from_link_local_router(self):
        result = parse_router_advertisement(
            ra_packet(),
            interface="eth0",
            interface_index=2,
            router="fe80::1",
            hop_limit=255,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].prefix, str(PREFIX))
        self.assertEqual(result[0].router, "fe80::1")
        self.assertEqual(result[0].valid_lifetime, 1800)
        self.assertTrue(result[0].autonomous)
        self.assertTrue(result[0].on_link)

    def test_rejects_spoofable_or_non_autonomous_advertisements(self):
        cases = [
            {"router": "2001:db8::1", "hop_limit": 255, "packet": ra_packet()},
            {"router": "fe80::1", "hop_limit": 254, "packet": ra_packet()},
            {
                "router": "fe80::1", "hop_limit": 255,
                "packet": ra_packet(flags=PIO_ON_LINK),
            },
            {
                "router": "fe80::1", "hop_limit": 255,
                "packet": ra_packet(prefix_length=56),
            },
            {
                "router": "fe80::1", "hop_limit": 255,
                "packet": ra_packet(valid=60, preferred=61),
            },
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(parse_router_advertisement(
                    case["packet"],
                    interface="eth0",
                    interface_index=2,
                    router=case["router"],
                    hop_limit=case["hop_limit"],
                ), ())

    def test_rejects_truncated_and_zero_length_options(self):
        self.assertEqual(parse_router_advertisement(
            ra_packet()[:-1], interface="eth0", interface_index=2,
            router="fe80::1", hop_limit=255,
        ), ())
        malformed = bytearray(ra_packet())
        malformed[17] = 0
        self.assertEqual(parse_router_advertisement(
            bytes(malformed), interface="eth0", interface_index=2,
            router="fe80::1", hop_limit=255,
        ), ())


class RouterAdvertisementDiscoveryTest(unittest.TestCase):
    def test_retries_solicitation_three_times_within_bounded_deadline(self):
        class Clock:
            value = 0.0

            def __call__(self):
                return self.value

        class Probe:
            def __init__(self, clock):
                self.clock = clock
                self.timeout = 0.0
                self.sent = []
                self.closed = False

            def setsockopt(self, *_arguments):
                return None

            def bind(self, *_arguments):
                return None

            def settimeout(self, value):
                self.timeout = value

            def sendto(self, packet, destination):
                self.sent.append((packet, destination))

            def recvmsg(self, *_arguments):
                self.clock.value += self.timeout
                raise socket.timeout()

            def close(self):
                self.closed = True

        clock = Clock()
        probe = Probe(clock)
        discovery = RouterAdvertisementDiscovery(
            socket_factory=lambda *_arguments: probe,
            now=clock,
        )
        with mock.patch(
            "nexus_agent.upstream_relay.socket.if_nametoindex", return_value=2
        ):
            self.assertEqual(discovery.discover("eth0", timeout=5.0), ())
        self.assertEqual(len(probe.sent), 3)
        self.assertTrue(probe.closed)


class UpstreamRelayBackendTest(unittest.TestCase):
    def advertisement(self):
        return RouterAdvertisementPrefix(
            interface="eth0",
            interface_index=2,
            router="fe80::1",
            prefix=str(PREFIX),
            valid_lifetime=1800,
            preferred_lifetime=900,
            on_link=True,
            autonomous=True,
        )

    def backend(self, values):
        discovery = mock.Mock()
        discovery.discover.return_value = values
        return UpstreamRelayLinuxBackend(
            interface="eth0", prefix=str(PREFIX), discovery=discovery
        )

    def test_prefix_ready_requires_matching_live_advertisement(self):
        backend = self.backend([self.advertisement()])
        self.assertTrue(backend.prefix_ready("eth0", PREFIX))
        self.assertFalse(backend.prefix_ready("eth1", PREFIX))
        missing = self.backend([])
        self.assertFalse(missing.prefix_ready("eth0", PREFIX))

    def test_deprecated_prefix_is_not_used_for_new_addresses(self):
        advertisement = self.advertisement()
        advertisement = RouterAdvertisementPrefix(
            **{**advertisement.__dict__, "preferred_lifetime": 0}
        )
        backend = self.backend([advertisement])
        self.assertFalse(backend.prefix_ready("eth0", PREFIX))
        self.assertFalse(backend.relay_status()["ready"])

    def test_status_exposes_router_and_ra_lifetimes(self):
        status = self.backend([self.advertisement()]).relay_status()
        self.assertEqual(status["mode"], "upstream-relay")
        self.assertTrue(status["ready"])
        self.assertEqual(status["router"], "fe80::1")
        self.assertEqual(status["valid_lifetime"], 1800)

    def test_add_and_refresh_apply_bounded_ra_lifetimes(self):
        backend = self.backend([self.advertisement()])
        with mock.patch.object(LinuxAddressBackend, "add_address") as add, mock.patch.object(
            LinuxAddressBackend, "has_address", return_value=True
        ), mock.patch.object(backend, "_run", return_value="") as run:
            backend.add_address("eth0", ADDRESS)
            backend.refresh_address("eth0", ADDRESS)
        add.assert_called_once_with("eth0", ADDRESS)
        expected = [
            "ip", "-6", "addr", "change", f"{ADDRESS}/128", "dev", "eth0",
            "valid_lft", "1800", "preferred_lft", "900", "noprefixroute",
        ]
        self.assertEqual(run.call_args_list, [mock.call(expected), mock.call(expected)])

    def test_refuses_address_outside_advertised_prefix(self):
        backend = self.backend([self.advertisement()])
        with self.assertRaisesRegex(HostAliasError, "outside"):
            backend.add_address("eth0", ipaddress.IPv6Address("2606:4700::1"))


if __name__ == "__main__":
    unittest.main()
