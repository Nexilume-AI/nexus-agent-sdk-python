"""Linux no-PD IPv6 upstream relay support for per-Agent /128 aliases.

The module accepts only a global /64 explicitly advertised in an ICMPv6
Router Advertisement Prefix Information Option.  It never derives a prefix
from an existing /128 address.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .host_alias import HostAliasError, LinuxAddressBackend, _safe_interface


ICMPV6_ROUTER_SOLICITATION = 133
ICMPV6_ROUTER_ADVERTISEMENT = 134
ND_OPT_PREFIX_INFORMATION = 3
ND_OPT_SOURCE_LINKADDR = 1
PIO_ON_LINK = 0x80
PIO_AUTONOMOUS = 0x40


@dataclass(frozen=True)
class RouterAdvertisementPrefix:
    interface: str
    interface_index: int
    router: str
    prefix: str
    valid_lifetime: int
    preferred_lifetime: int
    on_link: bool
    autonomous: bool


def parse_router_advertisement(
    packet: bytes,
    *,
    interface: str,
    interface_index: int,
    router: str,
    hop_limit: int,
) -> Tuple[RouterAdvertisementPrefix, ...]:
    """Parse bounded RA Prefix Information Options from one link-local router."""

    if (
        not isinstance(packet, bytes)
        or len(packet) < 16
        or len(packet) > 4096
        or packet[0] != ICMPV6_ROUTER_ADVERTISEMENT
        or packet[1] != 0
        or hop_limit != 255
    ):
        return ()
    interface = _safe_interface(interface)
    try:
        source = ipaddress.IPv6Address(str(router).split("%", 1)[0])
    except ValueError:
        return ()
    if not source.is_link_local:
        return ()
    result: List[RouterAdvertisementPrefix] = []
    offset = 16
    while offset < len(packet):
        if offset + 2 > len(packet):
            return ()
        option_type = packet[offset]
        option_units = packet[offset + 1]
        if option_units == 0:
            return ()
        option_length = option_units * 8
        if offset + option_length > len(packet):
            return ()
        if option_type == ND_OPT_PREFIX_INFORMATION and option_length == 32:
            prefix_length = packet[offset + 2]
            flags = packet[offset + 3]
            valid_lifetime = struct.unpack_from("!I", packet, offset + 4)[0]
            preferred_lifetime = struct.unpack_from("!I", packet, offset + 8)[0]
            raw_prefix = packet[offset + 16:offset + 32]
            try:
                prefix = ipaddress.IPv6Network(
                    (ipaddress.IPv6Address(raw_prefix), prefix_length), strict=True
                )
            except ValueError:
                offset += option_length
                continue
            autonomous = bool(flags & PIO_AUTONOMOUS)
            on_link = bool(flags & PIO_ON_LINK)
            if (
                prefix.prefixlen == 64
                and prefix.is_global
                and autonomous
                and valid_lifetime > 0
                and preferred_lifetime <= valid_lifetime
            ):
                result.append(RouterAdvertisementPrefix(
                    interface=interface,
                    interface_index=interface_index,
                    router=source.compressed,
                    prefix=str(prefix),
                    valid_lifetime=valid_lifetime,
                    preferred_lifetime=preferred_lifetime,
                    on_link=on_link,
                    autonomous=autonomous,
                ))
        offset += option_length
    return tuple(result)


class RouterAdvertisementDiscovery:
    """Actively solicit and validate upstream Router Advertisements on Linux."""

    def __init__(
        self,
        *,
        socket_factory: Callable[..., socket.socket] = socket.socket,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.socket_factory = socket_factory
        self.now = now

    @staticmethod
    def _hop_limit(ancillary: Iterable[Tuple[int, int, bytes]]) -> int:
        hop_type = getattr(socket, "IPV6_HOPLIMIT", 52)
        for level, option, value in ancillary:
            if level == socket.IPPROTO_IPV6 and option == hop_type and len(value) >= 4:
                return int(struct.unpack("i", value[:4])[0])
        return -1

    def discover(
        self,
        interface: str,
        *,
        timeout: float = 3.0,
        max_packets: int = 32,
    ) -> Tuple[RouterAdvertisementPrefix, ...]:
        interface = _safe_interface(interface)
        if not 0.1 <= timeout <= 15.0:
            raise ValueError("RA discovery timeout must be between 0.1 and 15 seconds")
        try:
            interface_index = socket.if_nametoindex(interface)
        except OSError as exc:
            raise HostAliasError(f"Linux interface was not found: {interface}") from exc
        try:
            probe = self.socket_factory(
                socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6
            )
        except OSError as exc:
            raise HostAliasError(
                "upstream-relay discovery requires CAP_NET_RAW or root"
            ) from exc
        discovered: Dict[Tuple[str, str], RouterAdvertisementPrefix] = {}
        try:
            probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, interface_index)
            probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
            recv_hop = getattr(socket, "IPV6_RECVHOPLIMIT", 51)
            probe.setsockopt(socket.IPPROTO_IPV6, recv_hop, 1)
            try:
                checksum = getattr(socket, "IPV6_CHECKSUM", 7)
                probe.setsockopt(socket.IPPROTO_IPV6, checksum, 2)
            except OSError:
                # Linux ICMPv6 raw sockets normally calculate this checksum.
                pass
            probe.bind(("::", 0, 0, interface_index))
            solicitation = struct.pack("!BBHI", ICMPV6_ROUTER_SOLICITATION, 0, 0, 0)
            deadline = self.now() + timeout
            next_solicitation = self.now()
            solicitations = 0
            packets = 0
            while packets < max_packets:
                current_time = self.now()
                remaining = deadline - current_time
                if remaining <= 0:
                    break
                if solicitations < 3 and current_time >= next_solicitation:
                    probe.sendto(solicitation, ("ff02::2", 0, 0, interface_index))
                    solicitations += 1
                    next_solicitation = current_time + (1.0 if solicitations == 1 else 2.0)
                receive_for = remaining
                if solicitations < 3:
                    receive_for = min(
                        receive_for, max(0.05, next_solicitation - self.now())
                    )
                probe.settimeout(receive_for)
                try:
                    packet, ancillary, _flags, source = probe.recvmsg(4096, 256)
                except socket.timeout:
                    continue
                except OSError as exc:
                    raise HostAliasError("failed while receiving Router Advertisement") from exc
                packets += 1
                router = source[0] if isinstance(source, tuple) and source else ""
                for candidate in parse_router_advertisement(
                    packet,
                    interface=interface,
                    interface_index=interface_index,
                    router=router,
                    hop_limit=self._hop_limit(ancillary),
                ):
                    key = (candidate.router, candidate.prefix)
                    current = discovered.get(key)
                    if current is None or candidate.valid_lifetime > current.valid_lifetime:
                        discovered[key] = candidate
        finally:
            probe.close()
        return tuple(sorted(
            discovered.values(),
            key=lambda item: (item.interface_index, item.prefix, item.router),
        ))


class UpstreamRelayLinuxBackend(LinuxAddressBackend):
    """Assign /128s only while an exact prefix remains advertised upstream."""

    mode = "upstream-relay"

    def __init__(
        self,
        *,
        interface: str,
        prefix: str,
        discovery: Optional[RouterAdvertisementDiscovery] = None,
        timeout: float = 5.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.interface = _safe_interface(interface)
        try:
            self.prefix = ipaddress.IPv6Network(prefix, strict=True)
        except ValueError as exc:
            raise ValueError("upstream relay prefix must be a canonical IPv6 /64") from exc
        if self.prefix.prefixlen != 64 or not self.prefix.is_global:
            raise ValueError("upstream relay prefix must be a global IPv6 /64")
        self.discovery = discovery or RouterAdvertisementDiscovery()
        self._advertisement: Optional[RouterAdvertisementPrefix] = None

    def _refresh_advertisement(self) -> RouterAdvertisementPrefix:
        candidates = self.discovery.discover(self.interface, timeout=min(self.timeout, 5.0))
        matches = [
            item for item in candidates
            if item.prefix == str(self.prefix) and item.preferred_lifetime > 0
        ]
        if not matches:
            raise HostAliasError(
                "upstream router no longer advertises the configured autonomous /64"
            )
        matches.sort(key=lambda item: (-item.valid_lifetime, item.router))
        self._advertisement = matches[0]
        return matches[0]

    def relay_status(self) -> dict:
        """Return daemon-owned RA health without requiring clients to open raw sockets."""
        try:
            advertisement = self._refresh_advertisement()
        except HostAliasError as exc:
            return {
                "mode": self.mode,
                "ready": False,
                "detail": str(exc)[:512],
            }
        return {
            "mode": self.mode,
            "ready": True,
            "router": advertisement.router,
            "valid_lifetime": advertisement.valid_lifetime,
            "preferred_lifetime": advertisement.preferred_lifetime,
            "on_link": advertisement.on_link,
            "autonomous": advertisement.autonomous,
        }

    def prefix_ready(self, interface: str, prefix: ipaddress.IPv6Network) -> bool:
        if _safe_interface(interface) != self.interface or prefix != self.prefix:
            return False
        try:
            self._refresh_advertisement()
            return True
        except HostAliasError:
            return False

    @staticmethod
    def _bounded_lifetimes(
        advertisement: RouterAdvertisementPrefix,
    ) -> Tuple[int, int]:
        valid = max(30, min(advertisement.valid_lifetime, 86400))
        preferred = max(0, min(advertisement.preferred_lifetime, valid))
        return valid, preferred

    def _set_lifetimes(
        self,
        operation: str,
        address: ipaddress.IPv6Address,
        advertisement: RouterAdvertisementPrefix,
    ) -> None:
        valid, preferred = self._bounded_lifetimes(advertisement)
        self._run([
            "ip", "-6", "addr", operation, f"{address}/128", "dev", self.interface,
            "valid_lft", str(valid), "preferred_lft", str(preferred), "noprefixroute",
        ])

    def add_address(self, interface: str, address: ipaddress.IPv6Address) -> None:
        if _safe_interface(interface) != self.interface or address not in self.prefix:
            raise HostAliasError("requested address is outside the upstream relay prefix")
        advertisement = self._refresh_advertisement()
        super().add_address(interface, address)
        try:
            self._set_lifetimes("change", address, advertisement)
        except BaseException:
            super().remove_address(interface, address)
            raise

    def refresh_address(self, interface: str, address: ipaddress.IPv6Address) -> None:
        if _safe_interface(interface) != self.interface or address not in self.prefix:
            raise HostAliasError("requested address is outside the upstream relay prefix")
        if not self.has_address(interface, address):
            raise HostAliasError("upstream relay address is missing from the interface")
        self._set_lifetimes("change", address, self._refresh_advertisement())
