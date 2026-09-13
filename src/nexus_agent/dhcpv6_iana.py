"""Bounded DHCPv6 IA_NA client and Linux per-Agent address backend.

The implementation follows RFC 9915.  One machine DUID owns a stable IAID per
Agent.  The privileged address daemon is the only DHCPv6 client; application
processes never bind UDP/546 or manipulate interface addresses.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import secrets
import socket
import struct
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .host_alias import HostAliasError, LinuxAddressBackend, _safe_interface


SOLICIT = 1
ADVERTISE = 2
REQUEST = 3
RENEW = 5
REBIND = 6
REPLY = 7
RELEASE = 8
DECLINE = 9

OPTION_CLIENTID = 1
OPTION_SERVERID = 2
OPTION_IA_NA = 3
OPTION_IAADDR = 5
OPTION_ORO = 6
OPTION_PREFERENCE = 7
OPTION_ELAPSED_TIME = 8
OPTION_STATUS_CODE = 13
OPTION_SOL_MAX_RT = 82

STATUS_SUCCESS = 0
STATUS_UNSPEC_FAIL = 1
STATUS_NO_ADDRS_AVAIL = 2
STATUS_NO_BINDING = 3
STATUS_NOT_ON_LINK = 4
STATUS_USE_MULTICAST = 5

STATUS_NAMES = {
    STATUS_SUCCESS: "Success",
    STATUS_UNSPEC_FAIL: "UnspecFail",
    STATUS_NO_ADDRS_AVAIL: "NoAddrsAvail",
    STATUS_NO_BINDING: "NoBinding",
    STATUS_NOT_ON_LINK: "NotOnLink",
    STATUS_USE_MULTICAST: "UseMulticast",
}

DHCPV6_CLIENT_PORT = 546
DHCPV6_SERVER_PORT = 547
ALL_DHCP_RELAY_AGENTS_AND_SERVERS = "ff02::1:2"
MAX_PACKET = 65535
MAX_OPTIONS = 128
DEFAULT_DUID_PATH = "/var/lib/nexus-agent/dhcpv6-duid"


class Dhcpv6TransientError(HostAliasError):
    """A DHCPv6 exchange timed out while the current lease may remain valid."""


class Dhcpv6LeaseError(HostAliasError):
    """The server definitively rejected or withdrew an IA_NA binding."""


class Dhcpv6ServerError(Dhcpv6LeaseError):
    """A syntactically valid server response carried a non-success status."""

    def __init__(self, code: int, message: str = "") -> None:
        self.code = int(code)
        self.server_message = str(message)
        name = STATUS_NAMES.get(self.code, f"status-{self.code}")
        detail = f": {self.server_message}" if self.server_message else ""
        super().__init__(f"DHCPv6 server returned {name}{detail}")


@dataclass(frozen=True)
class Dhcpv6Reply:
    server_id: bytes
    server_address: str
    iaid: int
    address: str
    preferred_lifetime: int
    valid_lifetime: int
    t1: int
    t2: int
    preference: int = 0


@dataclass(frozen=True)
class Dhcpv6Binding:
    iaid: int
    address: str
    server_id_hex: str
    server_address: str
    preferred_lifetime: int
    valid_lifetime: int
    t1: int
    t2: int
    acquired_at: float
    renew_at: float
    rebind_at: float
    preferred_until: float
    valid_until: float
    retry_at: float = 0.0

    @classmethod
    def from_reply(cls, reply: Dhcpv6Reply, *, now: float) -> "Dhcpv6Binding":
        preferred, valid, t1, t2 = _normalize_lifetimes(
            reply.preferred_lifetime, reply.valid_lifetime, reply.t1, reply.t2
        )
        return cls(
            iaid=reply.iaid,
            address=ipaddress.IPv6Address(reply.address).compressed,
            server_id_hex=reply.server_id.hex(),
            server_address=ipaddress.IPv6Address(reply.server_address).compressed,
            preferred_lifetime=preferred,
            valid_lifetime=valid,
            t1=t1,
            t2=t2,
            acquired_at=now,
            renew_at=now + t1,
            rebind_at=now + t2,
            preferred_until=now + preferred,
            valid_until=now + valid,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Dhcpv6Binding":
        try:
            result = cls(
                iaid=int(value["iaid"]),
                address=str(value["address"]),
                server_id_hex=str(value["server_id_hex"]),
                server_address=str(value["server_address"]),
                preferred_lifetime=int(value["preferred_lifetime"]),
                valid_lifetime=int(value["valid_lifetime"]),
                t1=int(value["t1"]),
                t2=int(value["t2"]),
                acquired_at=float(value["acquired_at"]),
                renew_at=float(value["renew_at"]),
                rebind_at=float(value["rebind_at"]),
                preferred_until=float(value["preferred_until"]),
                valid_until=float(value["valid_until"]),
                retry_at=float(value.get("retry_at", 0.0)),
            )
            address = ipaddress.IPv6Address(result.address)
            server = ipaddress.IPv6Address(result.server_address)
            server_id = bytes.fromhex(result.server_id_hex)
        except (KeyError, TypeError, ValueError) as exc:
            raise Dhcpv6LeaseError("stored DHCPv6 IA_NA binding is invalid") from exc
        if (
            not 0 <= result.iaid <= 0xFFFFFFFF
            or not address.is_global
            or server.is_unspecified
            or not 2 <= len(server_id) <= 128
            or result.valid_until <= result.acquired_at
            or result.preferred_until > result.valid_until
        ):
            raise Dhcpv6LeaseError("stored DHCPv6 IA_NA binding is invalid")
        return result

    @property
    def server_id(self) -> bytes:
        return bytes.fromhex(self.server_id_hex)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _option(code: int, value: bytes) -> bytes:
    if not 0 <= code <= 0xFFFF or len(value) > 0xFFFF:
        raise ValueError("DHCPv6 option is out of range")
    return struct.pack("!HH", code, len(value)) + value


def parse_options(payload: bytes) -> Dict[int, List[bytes]]:
    if not isinstance(payload, bytes) or len(payload) > MAX_PACKET:
        raise Dhcpv6LeaseError("DHCPv6 option area is invalid")
    result: Dict[int, List[bytes]] = {}
    offset = 0
    count = 0
    while offset < len(payload):
        if offset + 4 > len(payload):
            raise Dhcpv6LeaseError("DHCPv6 option header is truncated")
        code, length = struct.unpack_from("!HH", payload, offset)
        offset += 4
        if offset + length > len(payload):
            raise Dhcpv6LeaseError("DHCPv6 option value is truncated")
        count += 1
        if count > MAX_OPTIONS:
            raise Dhcpv6LeaseError("DHCPv6 message contains too many options")
        result.setdefault(code, []).append(payload[offset:offset + length])
        offset += length
    return result


def _singleton(options: Mapping[int, List[bytes]], code: int, name: str) -> bytes:
    values = options.get(code, [])
    if len(values) != 1:
        raise Dhcpv6LeaseError(f"DHCPv6 {name} option must appear exactly once")
    return values[0]


def _status(options: Mapping[int, List[bytes]]) -> Tuple[int, str]:
    values = options.get(OPTION_STATUS_CODE, [])
    if not values:
        return STATUS_SUCCESS, ""
    if len(values) != 1 or len(values[0]) < 2:
        raise Dhcpv6LeaseError("DHCPv6 status option is invalid")
    code = struct.unpack_from("!H", values[0], 0)[0]
    message = values[0][2:].decode("utf-8", errors="replace")[:256]
    return code, message


def _raise_status(code: int, message: str) -> None:
    if code == STATUS_SUCCESS:
        return
    raise Dhcpv6ServerError(code, message)


def _parse_ia_na(value: bytes, expected_iaid: int) -> Tuple[int, int, str, int, int]:
    if len(value) < 12:
        raise Dhcpv6LeaseError("DHCPv6 IA_NA option is truncated")
    iaid, t1, t2 = struct.unpack_from("!III", value, 0)
    if iaid != expected_iaid:
        raise Dhcpv6LeaseError("DHCPv6 reply contains an unexpected IAID")
    if t1 and t2 and t1 > t2:
        raise Dhcpv6LeaseError("DHCPv6 IA_NA has T1 greater than T2")
    nested = parse_options(value[12:])
    code, message = _status(nested)
    _raise_status(code, message)
    addresses = nested.get(OPTION_IAADDR, [])
    accepted: List[Tuple[str, int, int]] = []
    for item in addresses:
        if len(item) < 24:
            raise Dhcpv6LeaseError("DHCPv6 IA Address option is truncated")
        address = ipaddress.IPv6Address(item[:16])
        preferred, valid = struct.unpack_from("!II", item, 16)
        address_options = parse_options(item[24:])
        item_status, item_message = _status(address_options)
        if item_status != STATUS_SUCCESS:
            continue
        if address.is_global and valid > 0 and preferred <= valid:
            accepted.append((address.compressed, preferred, valid))
    if not accepted:
        raise Dhcpv6LeaseError("DHCPv6 server returned no usable global IA_NA address")
    accepted.sort(key=lambda item: (-item[2], -item[1], item[0]))
    address, preferred, valid = accepted[0]
    return t1, t2, address, preferred, valid


def parse_server_message(
    packet: bytes,
    *,
    transaction_id: bytes,
    client_id: bytes,
    expected_type: int,
    expected_iaid: int,
    server_address: str,
) -> Dhcpv6Reply:
    if (
        not isinstance(packet, bytes)
        or len(packet) < 4
        or len(packet) > MAX_PACKET
        or packet[0] != expected_type
        or packet[1:4] != transaction_id
    ):
        raise Dhcpv6LeaseError("DHCPv6 reply header is invalid")
    options = parse_options(packet[4:])
    if _singleton(options, OPTION_CLIENTID, "Client Identifier") != client_id:
        raise Dhcpv6LeaseError("DHCPv6 reply Client Identifier does not match")
    server_id = _singleton(options, OPTION_SERVERID, "Server Identifier")
    if not 2 <= len(server_id) <= 128:
        raise Dhcpv6LeaseError("DHCPv6 Server Identifier is invalid")
    code, message = _status(options)
    _raise_status(code, message)
    ia_values = options.get(OPTION_IA_NA, [])
    if len(ia_values) != 1:
        raise Dhcpv6LeaseError("DHCPv6 reply must contain exactly one IA_NA")
    t1, t2, address, preferred, valid = _parse_ia_na(
        ia_values[0], expected_iaid
    )
    preference_values = options.get(OPTION_PREFERENCE, [])
    preference = 0
    if preference_values:
        if len(preference_values) != 1 or len(preference_values[0]) != 1:
            raise Dhcpv6LeaseError("DHCPv6 Preference option is invalid")
        preference = preference_values[0][0]
    source = ipaddress.IPv6Address(str(server_address).split("%", 1)[0])
    if source.is_unspecified or source.is_multicast:
        raise Dhcpv6LeaseError("DHCPv6 reply source address is invalid")
    return Dhcpv6Reply(
        server_id=server_id,
        server_address=source.compressed,
        iaid=expected_iaid,
        address=address,
        preferred_lifetime=preferred,
        valid_lifetime=valid,
        t1=t1,
        t2=t2,
        preference=preference,
    )


def parse_server_ack(
    packet: bytes,
    *,
    transaction_id: bytes,
    client_id: bytes,
    expected_iaid: int,
    expected_server_id: bytes,
) -> None:
    if (
        not isinstance(packet, bytes)
        or len(packet) < 4
        or len(packet) > MAX_PACKET
        or packet[0] != REPLY
        or packet[1:4] != transaction_id
    ):
        raise Dhcpv6LeaseError("DHCPv6 acknowledgement header is invalid")
    options = parse_options(packet[4:])
    if _singleton(options, OPTION_CLIENTID, "Client Identifier") != client_id:
        raise Dhcpv6LeaseError("DHCPv6 acknowledgement Client Identifier does not match")
    if _singleton(options, OPTION_SERVERID, "Server Identifier") != expected_server_id:
        raise Dhcpv6LeaseError("DHCPv6 acknowledgement Server Identifier does not match")
    code, message = _status(options)
    _raise_status(code, message)
    ia_values = options.get(OPTION_IA_NA, [])
    if len(ia_values) > 1:
        raise Dhcpv6LeaseError("DHCPv6 acknowledgement has duplicate IA_NA options")
    if ia_values:
        if len(ia_values[0]) < 12:
            raise Dhcpv6LeaseError("DHCPv6 acknowledgement IA_NA is truncated")
        iaid = struct.unpack_from("!I", ia_values[0], 0)[0]
        if iaid != expected_iaid:
            raise Dhcpv6LeaseError("DHCPv6 acknowledgement contains an unexpected IAID")
        nested = parse_options(ia_values[0][12:])
        ia_code, ia_message = _status(nested)
        _raise_status(ia_code, ia_message)


def build_client_message(
    message_type: int,
    transaction_id: bytes,
    *,
    client_id: bytes,
    iaid: int,
    elapsed_centiseconds: int = 0,
    server_id: Optional[bytes] = None,
    address: Optional[str] = None,
) -> bytes:
    if len(transaction_id) != 3 or not 2 <= len(client_id) <= 128:
        raise ValueError("DHCPv6 identity or transaction ID is invalid")
    if message_type in (REQUEST, RENEW, RELEASE, DECLINE) and not server_id:
        raise ValueError("DHCPv6 message requires a Server Identifier")
    if message_type in (SOLICIT, REBIND) and server_id is not None:
        raise ValueError("DHCPv6 multicast discovery message cannot include Server Identifier")
    nested = b""
    if address is not None:
        packed = ipaddress.IPv6Address(address).packed
        nested = _option(OPTION_IAADDR, packed + struct.pack("!II", 0, 0))
    ia_na = struct.pack("!III", iaid, 0, 0) + nested
    options = [_option(OPTION_CLIENTID, client_id)]
    if server_id is not None:
        options.append(_option(OPTION_SERVERID, server_id))
    options.extend([
        _option(OPTION_ELAPSED_TIME, struct.pack(
            "!H", max(0, min(int(elapsed_centiseconds), 0xFFFF))
        )),
        _option(OPTION_IA_NA, ia_na),
    ])
    if message_type in (SOLICIT, REQUEST, RENEW, REBIND):
        options.append(_option(OPTION_ORO, struct.pack("!H", OPTION_SOL_MAX_RT)))
    return bytes([message_type]) + transaction_id + b"".join(options)


def _normalize_lifetimes(
    preferred: int, valid: int, t1: int, t2: int
) -> Tuple[int, int, int, int]:
    if valid <= 0 or preferred <= 0 or preferred > valid:
        raise Dhcpv6LeaseError("DHCPv6 address lifetimes are unusable")
    # Keep kernel lifetimes bounded while retaining conservative renewal times.
    valid = min(valid, 7 * 24 * 3600)
    preferred = min(preferred, valid)
    if t1 <= 0 or t1 >= valid:
        t1 = max(30, min(valid - 2, preferred // 2 or 30))
    if t2 <= 0 or t2 <= t1 or t2 >= valid:
        t2 = max(t1 + 1, min(valid - 1, int(preferred * 0.8)))
    if not 0 < t1 < t2 < valid:
        raise Dhcpv6LeaseError("DHCPv6 T1/T2 values cannot be scheduled safely")
    return preferred, valid, t1, t2


def duid_uuid_from_machine_id(machine_id: str) -> bytes:
    text = str(machine_id).strip().lower()
    if not text or len(text) > 128 or any(character not in "0123456789abcdef-" for character in text):
        raise ValueError("machine ID is invalid")
    value = uuid.uuid5(uuid.NAMESPACE_OID, "nexus-agent-addressd:" + text)
    return struct.pack("!H", 4) + value.bytes


def load_or_create_duid(
    path: str = DEFAULT_DUID_PATH,
    *,
    machine_id_path: str = "/etc/machine-id",
) -> bytes:
    target = Path(path)
    try:
        value = target.read_bytes()
    except FileNotFoundError:
        try:
            machine_id = Path(machine_id_path).read_text(encoding="ascii")
            value = duid_uuid_from_machine_id(machine_id)
        except (OSError, UnicodeError, ValueError):
            value = struct.pack("!H", 4) + uuid.uuid4().bytes
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(value)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    if len(value) != 18 or value[:2] != struct.pack("!H", 4):
        raise HostAliasError("DHCPv6 DUID file is not a valid DUID-UUID")
    return value


def preview_duid(machine_id_path: str = "/etc/machine-id") -> bytes:
    try:
        return duid_uuid_from_machine_id(
            Path(machine_id_path).read_text(encoding="ascii")
        )
    except (OSError, UnicodeError, ValueError):
        # Probe-only identity; installation persists a fresh DUID if needed.
        return struct.pack("!H", 4) + uuid.uuid4().bytes


def stable_iaid(
    duid: bytes, interface: str, owner: str, tenant: str, agent_id: str
) -> int:
    material = b"\0".join((
        duid,
        _safe_interface(interface).encode("utf-8"),
        str(owner).encode("utf-8"),
        str(tenant).encode("utf-8"),
        str(agent_id).encode("utf-8"),
    ))
    value = int.from_bytes(hashlib.sha256(material).digest()[:4], "big")
    return value or 1


class Dhcpv6IaNaClient:
    """Small synchronous RFC 9915 client with bounded retransmission."""

    def __init__(
        self,
        *,
        socket_factory: Callable[..., socket.socket] = socket.socket,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.socket_factory = socket_factory
        self.now = now

    @staticmethod
    def _interface_index(interface: str) -> int:
        try:
            return socket.if_nametoindex(_safe_interface(interface))
        except OSError as exc:
            raise HostAliasError(f"Linux interface was not found: {interface}") from exc

    def _socket(self, interface: str, interface_index: int) -> socket.socket:
        try:
            connection = self.socket_factory(
                socket.AF_INET6, socket.SOCK_DGRAM, socket.IPPROTO_UDP
            )
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            bind_device = getattr(socket, "SO_BINDTODEVICE", 25)
            connection.setsockopt(
                socket.SOL_SOCKET, bind_device, interface.encode("ascii") + b"\0"
            )
            connection.setsockopt(
                socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, interface_index
            )
            recv_pktinfo = getattr(socket, "IPV6_RECVPKTINFO", 49)
            connection.setsockopt(socket.IPPROTO_IPV6, recv_pktinfo, 1)
            connection.bind(("::", DHCPV6_CLIENT_PORT, 0, interface_index))
            return connection
        except (OSError, UnicodeEncodeError) as exc:
            try:
                connection.close()  # type: ignore[has-type]
            except Exception:
                pass
            raise HostAliasError(
                "DHCPv6 IA_NA could not bind UDP/546 on the selected interface; "
                "CAP_NET_BIND_SERVICE/CAP_NET_RAW"
            ) from exc

    @staticmethod
    def _packet_interface(ancillary: Iterable[Tuple[int, int, bytes]]) -> int:
        pktinfo = getattr(socket, "IPV6_PKTINFO", 50)
        for level, option, value in ancillary:
            if level == socket.IPPROTO_IPV6 and option == pktinfo and len(value) >= 20:
                return struct.unpack_from("=I", value, 16)[0]
        return 0

    def _exchange(
        self,
        interface: str,
        *,
        message_type: int,
        expected_type: int,
        client_id: bytes,
        iaid: int,
        server_id: Optional[bytes] = None,
        address: Optional[str] = None,
        destination: Optional[str] = None,
        timeout: float = 6.0,
        acknowledgement_only: bool = False,
    ) -> Optional[Dhcpv6Reply]:
        if not 1.0 <= timeout <= 30.0:
            raise ValueError("DHCPv6 exchange timeout must be between 1 and 30 seconds")
        interface = _safe_interface(interface)
        interface_index = self._interface_index(interface)
        transaction_id = secrets.token_bytes(3)
        started = self.now()
        deadline = started + timeout
        retransmit_at = started
        interval = 1.0
        target = destination or ALL_DHCP_RELAY_AGENTS_AND_SERVERS
        target_address = ipaddress.IPv6Address(target)
        target_scope = interface_index if target_address.is_link_local or target_address.is_multicast else 0
        connection = self._socket(interface, interface_index)
        last_error: Optional[BaseException] = None
        try:
            while self.now() < deadline:
                current = self.now()
                if current >= retransmit_at:
                    elapsed = int(max(0.0, current - started) * 100)
                    packet = build_client_message(
                        message_type,
                        transaction_id,
                        client_id=client_id,
                        iaid=iaid,
                        elapsed_centiseconds=elapsed,
                        server_id=server_id,
                        address=address,
                    )
                    connection.sendto(
                        packet,
                        (target_address.compressed, DHCPV6_SERVER_PORT, 0, target_scope),
                    )
                    retransmit_at = current + interval
                    interval = min(interval * 2.0, 4.0)
                wait_for = min(deadline, retransmit_at) - self.now()
                if wait_for <= 0:
                    continue
                connection.settimeout(wait_for)
                try:
                    packet, ancillary, _flags, source = connection.recvmsg(
                        MAX_PACKET, 256
                    )
                except socket.timeout:
                    continue
                except OSError as exc:
                    last_error = exc
                    continue
                if (
                    not isinstance(source, tuple)
                    or len(source) < 2
                    or int(source[1]) != DHCPV6_SERVER_PORT
                ):
                    continue
                received_interface = self._packet_interface(ancillary)
                if received_interface not in (0, interface_index):
                    continue
                try:
                    if acknowledgement_only:
                        if server_id is None:
                            raise Dhcpv6LeaseError(
                                "DHCPv6 acknowledgement requires a selected server"
                            )
                        parse_server_ack(
                            packet,
                            transaction_id=transaction_id,
                            client_id=client_id,
                            expected_iaid=iaid,
                            expected_server_id=server_id,
                        )
                        return None
                    reply = parse_server_message(
                        packet,
                        transaction_id=transaction_id,
                        client_id=client_id,
                        expected_type=expected_type,
                        expected_iaid=iaid,
                        server_address=str(source[0]),
                    )
                    if server_id is not None and reply.server_id != server_id:
                        last_error = Dhcpv6LeaseError(
                            "DHCPv6 reply came from an unexpected server"
                        )
                        continue
                    return reply
                except Dhcpv6ServerError as exc:
                    if (
                        exc.code == STATUS_USE_MULTICAST
                        and not target_address.is_multicast
                    ):
                        target_address = ipaddress.IPv6Address(
                            ALL_DHCP_RELAY_AGENTS_AND_SERVERS
                        )
                        target_scope = interface_index
                        retransmit_at = self.now()
                        last_error = exc
                        continue
                    raise
                except Dhcpv6LeaseError as exc:
                    last_error = exc
                    continue
        finally:
            connection.close()
        raise Dhcpv6TransientError("DHCPv6 server did not answer the IA_NA exchange") from last_error

    def probe(
        self, interface: str, *, client_id: bytes, iaid: int, timeout: float = 6.0
    ) -> Dhcpv6Reply:
        reply = self._exchange(
            interface,
            message_type=SOLICIT,
            expected_type=ADVERTISE,
            client_id=client_id,
            iaid=iaid,
            timeout=timeout,
        )
        if reply is None:
            raise Dhcpv6LeaseError("DHCPv6 Solicit returned no offer")
        return reply

    def acquire(
        self, interface: str, *, client_id: bytes, iaid: int, timeout: float = 6.0
    ) -> Dhcpv6Reply:
        offer = self.probe(interface, client_id=client_id, iaid=iaid, timeout=timeout)
        reply = self._exchange(
            interface,
            message_type=REQUEST,
            expected_type=REPLY,
            client_id=client_id,
            iaid=iaid,
            server_id=offer.server_id,
            address=offer.address,
            timeout=timeout,
        )
        if reply is None:
            raise Dhcpv6LeaseError("DHCPv6 Request returned no binding")
        return reply

    def renew(
        self,
        interface: str,
        *,
        client_id: bytes,
        binding: Dhcpv6Binding,
        rebind: bool = False,
        timeout: float = 6.0,
    ) -> Dhcpv6Reply:
        reply = self._exchange(
            interface,
            message_type=REBIND if rebind else RENEW,
            expected_type=REPLY,
            client_id=client_id,
            iaid=binding.iaid,
            server_id=None if rebind else binding.server_id,
            address=binding.address,
            destination=None if rebind else binding.server_address,
            timeout=timeout,
        )
        if reply is None:
            raise Dhcpv6LeaseError("DHCPv6 Renew/Rebind returned no binding")
        return reply

    def release(
        self, interface: str, *, client_id: bytes, binding: Dhcpv6Binding
    ) -> None:
        self._exchange(
            interface,
            message_type=RELEASE,
            expected_type=REPLY,
            client_id=client_id,
            iaid=binding.iaid,
            server_id=binding.server_id,
            address=binding.address,
            destination=binding.server_address,
            timeout=3.0,
            acknowledgement_only=True,
        )

    def decline(
        self, interface: str, *, client_id: bytes, binding: Dhcpv6Binding
    ) -> None:
        self._exchange(
            interface,
            message_type=DECLINE,
            expected_type=REPLY,
            client_id=client_id,
            iaid=binding.iaid,
            server_id=binding.server_id,
            address=binding.address,
            destination=binding.server_address,
            timeout=3.0,
            acknowledgement_only=True,
        )


class Dhcpv6IaNaLinuxBackend(LinuxAddressBackend):
    """Acquire, install, renew, persist and release one IA_NA per Agent."""

    mode = "dhcpv6-ia-na"
    dynamic_addressing = True

    def __init__(
        self,
        *,
        interface: str,
        client_id: bytes,
        client: Optional[Dhcpv6IaNaClient] = None,
        now: Callable[[], float] = time.time,
        timeout: float = 6.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.interface = _safe_interface(interface)
        if not 2 <= len(client_id) <= 128:
            raise ValueError("DHCPv6 client DUID is invalid")
        self.client_id = bytes(client_id)
        self.client = client or Dhcpv6IaNaClient()
        self.now = now
        self._bindings: Dict[str, Dhcpv6Binding] = {}
        self._lock = threading.RLock()
        self._last_error = ""
        self._last_server = ""

    def prefix_ready(self, interface: str, prefix: ipaddress.IPv6Network) -> bool:
        return _safe_interface(interface) == self.interface and prefix.prefixlen == 128

    def _iaid(self, owner: str, tenant: str, agent_id: str) -> int:
        return stable_iaid(
            self.client_id, self.interface, owner, tenant, agent_id
        )

    def _set_lifetimes(self, binding: Dhcpv6Binding) -> None:
        now = self.now()
        valid = max(1, int(binding.valid_until - now))
        preferred = max(0, min(valid, int(binding.preferred_until - now)))
        self._run([
            "ip", "-6", "addr", "change", f"{binding.address}/128",
            "dev", self.interface, "valid_lft", str(valid),
            "preferred_lft", str(preferred), "noprefixroute",
        ])

    def allocate_address(
        self,
        interface: str,
        *,
        owner: str,
        tenant: str,
        agent_id: str,
        lease_seconds: int,
    ) -> ipaddress.IPv6Address:
        del lease_seconds  # The DHCP server owns the network lease lifetime.
        if _safe_interface(interface) != self.interface:
            raise HostAliasError("requested interface is not allowed")
        iaid = self._iaid(owner, tenant, agent_id)
        with self._lock:
            if any(binding.iaid == iaid for binding in self._bindings.values()):
                raise HostAliasError("DHCPv6 IAID is already active")
            reply = self.client.acquire(
                self.interface, client_id=self.client_id, iaid=iaid
            )
            binding = Dhcpv6Binding.from_reply(reply, now=self.now())
            address = ipaddress.IPv6Address(binding.address)
            if self.has_address(self.interface, address):
                try:
                    self.client.release(
                        self.interface, client_id=self.client_id, binding=binding
                    )
                except HostAliasError:
                    pass
                raise HostAliasError("DHCPv6 server assigned an address already in use")
            try:
                super().add_address(self.interface, address)
                self._set_lifetimes(binding)
            except BaseException:
                try:
                    self.client.decline(
                        self.interface, client_id=self.client_id, binding=binding
                    )
                except HostAliasError:
                    pass
                raise
            self._bindings[address.compressed] = binding
            self._last_error = ""
            self._last_server = binding.server_address
            return address

    def _renew_binding(self, binding: Dhcpv6Binding) -> Dhcpv6Binding:
        now = self.now()
        if now >= binding.valid_until:
            raise Dhcpv6LeaseError("DHCPv6 IA_NA lease expired")
        if now < max(binding.renew_at, binding.retry_at):
            self._set_lifetimes(binding)
            return binding
        try:
            reply = self.client.renew(
                self.interface,
                client_id=self.client_id,
                binding=binding,
                rebind=now >= binding.rebind_at,
            )
            updated = Dhcpv6Binding.from_reply(reply, now=now)
            if updated.address != binding.address or updated.iaid != binding.iaid:
                raise Dhcpv6LeaseError(
                    "DHCPv6 server changed the active Agent address"
                )
            self._last_error = ""
            self._last_server = updated.server_address
            return updated
        except Dhcpv6TransientError as exc:
            self._last_error = str(exc)[:512]
            if now >= binding.valid_until:
                raise Dhcpv6LeaseError("DHCPv6 IA_NA lease expired") from exc
            retry = min(binding.valid_until - 1, now + 30.0)
            return replace(binding, retry_at=max(now + 1.0, retry))

    def refresh_address(self, interface: str, address: ipaddress.IPv6Address) -> None:
        if _safe_interface(interface) != self.interface:
            raise HostAliasError("requested interface is not allowed")
        key = address.compressed
        with self._lock:
            binding = self._bindings.get(key)
            if binding is None:
                raise Dhcpv6LeaseError("DHCPv6 IA_NA binding is missing")
            updated = self._renew_binding(binding)
            self._bindings[key] = updated
            self._set_lifetimes(updated)

    def remove_address(self, interface: str, address: ipaddress.IPv6Address) -> None:
        if _safe_interface(interface) != self.interface:
            raise HostAliasError("requested interface is not allowed")
        with self._lock:
            binding = self._bindings.pop(address.compressed, None)
            if binding is not None:
                try:
                    self.client.release(
                        self.interface, client_id=self.client_id, binding=binding
                    )
                except HostAliasError as exc:
                    self._last_error = str(exc)[:512]
            super().remove_address(self.interface, address)

    def snapshot_address(self, address: ipaddress.IPv6Address) -> Dict[str, Any]:
        with self._lock:
            binding = self._bindings.get(address.compressed)
            return binding.to_dict() if binding is not None else {}

    def restore_address(
        self,
        interface: str,
        address: ipaddress.IPv6Address,
        *,
        owner: str,
        tenant: str,
        agent_id: str,
        metadata: Mapping[str, Any],
    ) -> None:
        if _safe_interface(interface) != self.interface:
            raise Dhcpv6LeaseError("stored DHCPv6 interface does not match")
        binding = Dhcpv6Binding.from_dict(metadata)
        if (
            binding.address != address.compressed
            or binding.iaid != self._iaid(owner, tenant, agent_id)
            or self.now() >= binding.valid_until
        ):
            raise Dhcpv6LeaseError("stored DHCPv6 IA_NA identity no longer matches")
        with self._lock:
            self._bindings[address.compressed] = binding
            try:
                if not self.has_address(self.interface, address):
                    super().add_address(self.interface, address)
                self.refresh_address(self.interface, address)
            except BaseException:
                self._bindings.pop(address.compressed, None)
                if self.has_address(self.interface, address):
                    super().remove_address(self.interface, address)
                raise

    def tick(self) -> Tuple[str, ...]:
        lost: List[str] = []
        with self._lock:
            for address, binding in list(self._bindings.items()):
                now = self.now()
                if now < max(binding.renew_at, binding.retry_at):
                    continue
                try:
                    self.refresh_address(
                        self.interface, ipaddress.IPv6Address(address)
                    )
                except HostAliasError as exc:
                    self._last_error = str(exc)[:512]
                    self._bindings.pop(address, None)
                    try:
                        super().remove_address(
                            self.interface, ipaddress.IPv6Address(address)
                        )
                    except HostAliasError:
                        pass
                    lost.append(address)
        return tuple(lost)

    def backend_status(self, *, probe: bool = False) -> Dict[str, Any]:
        with self._lock:
            ready = True
            if probe:
                iaid = stable_iaid(
                    self.client_id,
                    self.interface,
                    "doctor",
                    "nexus-doctor",
                    "capability-probe",
                )
                try:
                    offer = self.client.probe(
                        self.interface,
                        client_id=self.client_id,
                        iaid=iaid,
                        timeout=6.0,
                    )
                    self._last_server = offer.server_address
                    self._last_error = ""
                except HostAliasError as exc:
                    ready = False
                    self._last_error = str(exc)[:512]
            servers = sorted({
                binding.server_address for binding in self._bindings.values()
            })
            if self._last_server and self._last_server not in servers:
                servers.append(self._last_server)
            return {
                "mode": self.mode,
                "ready": ready,
                "interface": self.interface,
                "bindings": len(self._bindings),
                "servers": servers,
                "last_error": self._last_error,
            }
