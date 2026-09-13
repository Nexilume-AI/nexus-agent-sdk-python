"""Host-side IPv6 /128 lease allocation for one-address-per-Agent mode."""

import hashlib
import hmac
import ipaddress
import json
import locale
import os
import platform
import re
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple

from .errors import NexusAgentError

_IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._~-]{0,93}[A-Za-z0-9])?$")


class HostAliasError(NexusAgentError):
    """A bounded local IPv6 allocation error."""


@dataclass(frozen=True)
class HostAliasLeaseInfo:
    lease_id: str
    address: str
    prefix: str
    interface: str
    tenant: str
    agent_id: str
    state: str
    lease_seconds: int
    expires_at: float

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HostAliasLeaseInfo":
        try:
            result = cls(
                lease_id=str(value["lease_id"]),
                address=str(value["address"]),
                prefix=str(value["prefix"]),
                interface=str(value["interface"]),
                tenant=str(value["tenant"]),
                agent_id=str(value["agent_id"]),
                state=str(value["state"]),
                lease_seconds=int(value["lease_seconds"]),
                expires_at=float(value["expires_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HostAliasError("addressd returned an invalid lease") from exc
        if result.state not in ("reserved", "active"):
            raise HostAliasError("addressd returned an invalid lease state")
        try:
            address = ipaddress.IPv6Address(result.address)
            prefix = ipaddress.IPv6Network(result.prefix, strict=True)
        except ValueError as exc:
            raise HostAliasError("addressd returned an invalid IPv6 lease") from exc
        if address not in prefix or prefix.prefixlen not in (64, 128):
            raise HostAliasError("addressd returned an invalid static or dynamic prefix")
        return result

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AddressBackend(Protocol):
    def prefix_ready(self, interface: str, prefix: ipaddress.IPv6Network) -> bool:
        ...

    def add_address(self, interface: str, address: ipaddress.IPv6Address) -> None:
        ...

    def remove_address(self, interface: str, address: ipaddress.IPv6Address) -> None:
        ...

    def has_address(self, interface: str, address: ipaddress.IPv6Address) -> bool:
        ...


class MemoryAddressBackend:
    """Deterministic non-privileged backend for tests and dry runs."""

    def __init__(self, prefixes: Iterable[Tuple[str, str]]) -> None:
        self.prefixes = {
            (interface, str(ipaddress.IPv6Network(prefix, strict=True)))
            for interface, prefix in prefixes
        }
        self.addresses = set()
        self._lock = threading.RLock()

    def prefix_ready(self, interface: str, prefix: ipaddress.IPv6Network) -> bool:
        return (interface, str(prefix)) in self.prefixes

    def add_address(self, interface: str, address: ipaddress.IPv6Address) -> None:
        with self._lock:
            key = (interface, str(address))
            if key in self.addresses:
                raise HostAliasError("IPv6 address already exists on the interface")
            self.addresses.add(key)

    def remove_address(self, interface: str, address: ipaddress.IPv6Address) -> None:
        with self._lock:
            self.addresses.discard((interface, str(address)))

    def has_address(self, interface: str, address: ipaddress.IPv6Address) -> bool:
        with self._lock:
            return (interface, str(address)) in self.addresses


def _safe_interface(value: str) -> str:
    text = str(value)
    if not text or len(text) > 128 or text != text.strip() or any(
        ord(character) < 0x20 or character in "\r\n\0" for character in text
    ):
        raise ValueError("interface name is invalid")
    return text


class _CommandAddressBackend:
    def __init__(self, *, timeout: float = 5.0) -> None:
        self.timeout = timeout

    def _run(self, arguments: List[str]) -> str:
        try:
            completed = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                text=False,
                timeout=self.timeout,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostAliasError(f"address backend command failed: {arguments[0]}") from exc
        def decode(value: bytes) -> str:
            encodings = ("utf-8", locale.getpreferredencoding(False), "mbcs")
            for encoding in dict.fromkeys(encodings):
                try:
                    return value.decode(encoding)
                except (LookupError, UnicodeDecodeError):
                    continue
            return value.decode("utf-8", errors="replace")

        stdout = decode(completed.stdout)
        stderr = decode(completed.stderr)
        if completed.returncode != 0:
            message = (stderr or stdout).strip()
            raise HostAliasError(message[:512] or "address backend rejected the operation")
        return stdout


class LinuxAddressBackend(_CommandAddressBackend):
    """Bounded Linux backend using argument-vector iproute2 operations."""

    def prefix_ready(self, interface: str, prefix: ipaddress.IPv6Network) -> bool:
        interface = _safe_interface(interface)
        output = self._run(["ip", "-6", "route", "show", "dev", interface])
        for line in output.splitlines():
            token = line.strip().split(" ", 1)[0]
            try:
                candidate = ipaddress.IPv6Network(token, strict=False)
            except ValueError:
                continue
            if candidate == prefix:
                return True
        return False

    def add_address(self, interface: str, address: ipaddress.IPv6Address) -> None:
        interface = _safe_interface(interface)
        self._run(["ip", "-6", "addr", "add", f"{address}/128", "dev", interface])
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            output = self._run([
                "ip", "-6", "addr", "show", "dev", interface,
                "to", f"{address}/128",
            ])
            lowered = output.lower()
            if "dadfailed" in lowered:
                self.remove_address(interface, address)
                raise HostAliasError("IPv6 duplicate address detection failed")
            if str(address) in output and "tentative" not in lowered:
                return
            time.sleep(0.05)
        self.remove_address(interface, address)
        raise HostAliasError("IPv6 duplicate address detection timed out")

    def remove_address(self, interface: str, address: ipaddress.IPv6Address) -> None:
        interface = _safe_interface(interface)
        if self.has_address(interface, address):
            self._run(["ip", "-6", "addr", "del", f"{address}/128", "dev", interface])

    def has_address(self, interface: str, address: ipaddress.IPv6Address) -> bool:
        interface = _safe_interface(interface)
        output = self._run([
            "ip", "-6", "addr", "show", "dev", interface,
            "to", f"{address}/128",
        ])
        return str(address) in output


class WindowsAddressBackend(_CommandAddressBackend):
    """Windows backend using fixed netsh verbs without a command shell."""

    def interface_index(self, interface: str) -> int:
        interface = _safe_interface(interface)
        try:
            return socket.if_nametoindex(interface)
        except OSError:
            pass
        output = self._run(["netsh", "interface", "ipv6", "show", "interface"])
        for line in output.splitlines():
            columns = line.strip().split(None, 4)
            if (
                len(columns) == 5
                and columns[0].isdigit()
                and columns[4] == interface
            ):
                return int(columns[0])
        raise HostAliasError(f"Windows interface was not found: {interface}")

    def prefix_ready(self, interface: str, prefix: ipaddress.IPv6Network) -> bool:
        interface = _safe_interface(interface)
        interface_index = self.interface_index(interface)
        output = self._run(["netsh", "interface", "ipv6", "show", "route"])
        for line in output.splitlines():
            tokens = line.split()
            for position, token in enumerate(tokens[:-1]):
                if "/" not in token:
                    continue
                try:
                    candidate = ipaddress.IPv6Network(token, strict=False)
                except ValueError:
                    continue
                if candidate == prefix and tokens[position + 1] == str(interface_index):
                    return True
        return False

    def add_address(self, interface: str, address: ipaddress.IPv6Address) -> None:
        interface = _safe_interface(interface)
        self._run([
            "netsh", "interface", "ipv6", "add", "address",
            f"interface={interface}", f"address={address}/128",
            "type=unicast", "store=active",
        ])
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            output = self._run([
                "netsh", "interface", "ipv6", "show", "address",
                f"interface={interface}",
            ])
            lowered = output.lower()
            if "duplicate" in lowered or "重复" in output:
                self.remove_address(interface, address)
                raise HostAliasError("IPv6 duplicate address detection failed")
            if str(address).lower() in lowered:
                probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                try:
                    probe.bind((str(address), 0))
                except OSError:
                    pass
                else:
                    return
                finally:
                    probe.close()
            time.sleep(0.05)
        self.remove_address(interface, address)
        raise HostAliasError("IPv6 duplicate address detection timed out")

    def remove_address(self, interface: str, address: ipaddress.IPv6Address) -> None:
        interface = _safe_interface(interface)
        if self.has_address(interface, address):
            self._run([
                "netsh", "interface", "ipv6", "delete", "address",
                f"interface={interface}", f"address={address}", "store=active",
            ])

    def has_address(self, interface: str, address: ipaddress.IPv6Address) -> bool:
        interface = _safe_interface(interface)
        output = self._run([
            "netsh", "interface", "ipv6", "show", "address",
            f"interface={interface}",
        ])
        return str(address).lower() in output.lower()


def system_address_backend() -> AddressBackend:
    if platform.system() == "Linux":
        return LinuxAddressBackend()
    if platform.system() == "Windows":
        return WindowsAddressBackend()
    raise HostAliasError("host-alias allocation supports Linux and Windows")


@dataclass
class _LeaseRecord:
    info: HostAliasLeaseInfo
    owner: str


class HostAliasAllocator:
    """Thread-safe `/128` lease allocator used inside addressd."""

    def __init__(
        self,
        *,
        backend: AddressBackend,
        interface: str,
        prefix: Optional[str],
        allocation_secret: bytes,
        max_addresses: int = 256,
        default_lease_seconds: int = 300,
        reservation_seconds: int = 15,
        now: Any = time.time,
    ) -> None:
        self.backend = backend
        self.interface = _safe_interface(interface)
        self.dynamic_addressing = bool(
            getattr(backend, "dynamic_addressing", False)
        )
        if self.dynamic_addressing:
            if prefix not in (None, "dynamic"):
                raise ValueError("dynamic address backend requires prefix='dynamic'")
            self.prefix: Optional[ipaddress.IPv6Network] = None
        else:
            try:
                self.prefix = ipaddress.IPv6Network(str(prefix), strict=True)
            except ValueError as exc:
                raise ValueError("prefix must be a canonical IPv6 /64") from exc
            if self.prefix.prefixlen != 64 or not self.prefix.is_global:
                raise ValueError("prefix must be a global IPv6 /64")
        if not isinstance(allocation_secret, bytes) or len(allocation_secret) < 32:
            raise ValueError("allocation_secret must contain at least 32 bytes")
        if not 1 <= max_addresses <= 65536:
            raise ValueError("max_addresses must be between 1 and 65536")
        if not 30 <= default_lease_seconds <= 86400:
            raise ValueError("default_lease_seconds must be between 30 and 86400")
        if not 5 <= reservation_seconds <= 300:
            raise ValueError("reservation_seconds must be between 5 and 300")
        self._secret = allocation_secret
        self.max_addresses = max_addresses
        self.default_lease_seconds = default_lease_seconds
        self.reservation_seconds = reservation_seconds
        self._now = now
        self._records: Dict[str, _LeaseRecord] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _identity_valid(value: str) -> bool:
        return bool(_IDENTIFIER.fullmatch(value))

    @staticmethod
    def _owner_valid(owner: str) -> bool:
        return bool(
            owner
            and len(owner) <= 128
            and owner.isascii()
            and not any(
                character in "\r\n\0" or ord(character) < 0x20
                for character in owner
            )
        )

    def _address(self, owner: str, tenant: str, agent_id: str, counter: int):
        if self.prefix is None:
            raise HostAliasError("dynamic backend must select the IPv6 address")
        material = "\0".join((
            self.interface, str(self.prefix), owner, tenant, agent_id, str(counter)
        )).encode("utf-8")
        digest = hmac.new(self._secret, material, hashlib.sha256).digest()
        iid = int.from_bytes(digest[:8], "big")
        iid &= ~(1 << 57)  # locally administered IID
        if iid < 0x100 or iid >= (1 << 64) - 0x100:
            return None
        return ipaddress.IPv6Address(int(self.prefix.network_address) | iid)

    def _info(self, record: _LeaseRecord) -> HostAliasLeaseInfo:
        return record.info

    def _remove(self, lease_id: str) -> None:
        record = self._records.pop(lease_id, None)
        if record is None:
            return
        self.backend.remove_address(
            record.info.interface, ipaddress.IPv6Address(record.info.address)
        )

    def sweep(self) -> int:
        now = float(self._now())
        with self._lock:
            removed = 0
            tick = getattr(self.backend, "tick", None)
            if callable(tick):
                lost = {str(item) for item in tick()}
                for lease_id, record in list(self._records.items()):
                    if record.info.address in lost:
                        self._records.pop(lease_id, None)
                        removed += 1
            expired = [
                lease_id for lease_id, record in self._records.items()
                if record.info.expires_at <= now
            ]
            for lease_id in expired:
                self._remove(lease_id)
            return removed + len(expired)

    def allocate(
        self,
        *,
        owner: str,
        tenant: str,
        agent_id: str,
        interface: Optional[str] = None,
        prefix: Optional[str] = None,
        lease_seconds: Optional[int] = None,
    ) -> HostAliasLeaseInfo:
        if not self._identity_valid(tenant) or not self._identity_valid(agent_id):
            raise HostAliasError("tenant and agent_id must be safe identifiers")
        if not self._owner_valid(owner):
            raise HostAliasError("local lease owner is invalid")
        if interface not in (None, "auto", self.interface):
            raise HostAliasError("requested interface is not allowed")
        allowed_prefixes = (
            (None, "auto", "dynamic") if self.dynamic_addressing
            else (None, "auto", str(self.prefix))
        )
        if prefix not in allowed_prefixes:
            raise HostAliasError("requested prefix is not allowed")
        duration = self.default_lease_seconds if lease_seconds is None else lease_seconds
        if not 30 <= duration <= 86400:
            raise HostAliasError("lease_seconds must be between 30 and 86400")
        with self._lock:
            self.sweep()
            for record in self._records.values():
                info = record.info
                if (record.owner, info.tenant, info.agent_id) == (
                    owner, tenant, agent_id
                ):
                    return info
            if len(self._records) >= self.max_addresses:
                raise HostAliasError("host IPv6 address quota is full")
            if self.dynamic_addressing:
                allocate_address = getattr(self.backend, "allocate_address", None)
                if not callable(allocate_address):
                    raise HostAliasError("dynamic address backend cannot allocate addresses")
                address = ipaddress.IPv6Address(allocate_address(
                    self.interface,
                    owner=owner,
                    tenant=tenant,
                    agent_id=agent_id,
                    lease_seconds=duration,
                ))
                if not address.is_global:
                    try:
                        self.backend.remove_address(self.interface, address)
                    finally:
                        raise HostAliasError(
                            "dynamic address backend returned a non-global address"
                        )
                now = float(self._now())
                info = HostAliasLeaseInfo(
                    lease_id=secrets.token_hex(32),
                    address=address.compressed,
                    prefix=f"{address.compressed}/128",
                    interface=self.interface,
                    tenant=tenant,
                    agent_id=agent_id,
                    state="reserved",
                    lease_seconds=duration,
                    expires_at=now + self.reservation_seconds,
                )
                self._records[info.lease_id] = _LeaseRecord(info=info, owner=owner)
                return info
            if self.prefix is None or not self.backend.prefix_ready(
                self.interface, self.prefix
            ):
                raise HostAliasError("configured global /64 is not on-link")
            used = {record.info.address for record in self._records.values()}
            for counter in range(64):
                address = self._address(owner, tenant, agent_id, counter)
                if address is None or str(address) in used:
                    continue
                if self.backend.has_address(self.interface, address):
                    continue
                self.backend.add_address(self.interface, address)
                now = float(self._now())
                info = HostAliasLeaseInfo(
                    lease_id=secrets.token_hex(32),
                    address=address.compressed,
                    prefix=str(self.prefix),
                    interface=self.interface,
                    tenant=tenant,
                    agent_id=agent_id,
                    state="reserved",
                    lease_seconds=duration,
                    expires_at=now + self.reservation_seconds,
                )
                self._records[info.lease_id] = _LeaseRecord(info=info, owner=owner)
                return info
            raise HostAliasError("could not allocate a collision-free IPv6 address")

    def _owned(self, lease_id: str, owner: str) -> _LeaseRecord:
        if not self._owner_valid(owner):
            raise HostAliasError("local lease owner is invalid")
        record = self._records.get(lease_id)
        if record is None:
            raise HostAliasError("IPv6 lease was not found")
        if not hmac.compare_digest(record.owner, owner):
            raise HostAliasError("IPv6 lease belongs to another local user")
        return record

    def confirm(self, lease_id: str, *, owner: str) -> HostAliasLeaseInfo:
        with self._lock:
            self.sweep()
            record = self._owned(lease_id, owner)
            if record.info.state != "reserved":
                return record.info
            info = HostAliasLeaseInfo(**{
                **record.info.to_dict(),
                "state": "active",
                "expires_at": float(self._now()) + record.info.lease_seconds,
            })
            record.info = info
            return info

    def renew(self, lease_id: str, *, owner: str) -> HostAliasLeaseInfo:
        with self._lock:
            self.sweep()
            record = self._owned(lease_id, owner)
            if record.info.state != "active":
                raise HostAliasError("IPv6 lease must be confirmed before renewal")
            refresh = getattr(self.backend, "refresh_address", None)
            if callable(refresh):
                try:
                    refresh(
                        record.info.interface,
                        ipaddress.IPv6Address(record.info.address),
                    )
                except BaseException:
                    self._remove(lease_id)
                    raise
            info = HostAliasLeaseInfo(**{
                **record.info.to_dict(),
                "expires_at": float(self._now()) + record.info.lease_seconds,
            })
            record.info = info
            return info

    def release(self, lease_id: str, *, owner: str) -> None:
        with self._lock:
            self._owned(lease_id, owner)
            self._remove(lease_id)

    def list(self, *, owner: Optional[str] = None) -> Tuple[HostAliasLeaseInfo, ...]:
        if owner is not None and not self._owner_valid(owner):
            raise HostAliasError("local lease owner is invalid")
        with self._lock:
            self.sweep()
            return tuple(
                record.info for record in self._records.values()
                if owner is None or hmac.compare_digest(record.owner, owner)
            )

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            result: List[Dict[str, Any]] = []
            snapshot_address = getattr(self.backend, "snapshot_address", None)
            for record in self._records.values():
                value = {**record.info.to_dict(), "owner": record.owner}
                if callable(snapshot_address):
                    metadata = snapshot_address(
                        ipaddress.IPv6Address(record.info.address)
                    )
                    if metadata:
                        value["backend"] = dict(metadata)
                result.append(value)
            return result

    def restore(self, values: Iterable[Mapping[str, Any]]) -> int:
        restored = 0
        now = float(self._now())
        with self._lock:
            for value in values:
                try:
                    owner = str(value["owner"])
                    info = HostAliasLeaseInfo.from_dict(value)
                    address = ipaddress.IPv6Address(info.address)
                except (KeyError, ValueError, HostAliasError):
                    continue
                if not self._owner_valid(owner) or info.interface != self.interface:
                    continue
                if self.dynamic_addressing:
                    if info.prefix != f"{address.compressed}/128":
                        continue
                    restore_address = getattr(self.backend, "restore_address", None)
                    metadata = value.get("backend")
                    if not callable(restore_address) or not isinstance(metadata, Mapping):
                        continue
                    try:
                        restore_address(
                            self.interface,
                            address,
                            owner=owner,
                            tenant=info.tenant,
                            agent_id=info.agent_id,
                            metadata=metadata,
                        )
                    except BaseException:
                        continue
                    if (
                        info.state == "reserved"
                        or info.expires_at <= now
                        or len(self._records) >= self.max_addresses
                    ):
                        self.backend.remove_address(self.interface, address)
                        continue
                    self._records[info.lease_id] = _LeaseRecord(
                        info=info, owner=owner
                    )
                    restored += 1
                    continue
                if info.prefix != str(self.prefix):
                    continue
                if info.state == "reserved" or info.expires_at <= now:
                    if self.backend.has_address(self.interface, address):
                        self.backend.remove_address(self.interface, address)
                    continue
                if len(self._records) >= self.max_addresses:
                    continue
                present = self.backend.has_address(self.interface, address)
                refresh = getattr(self.backend, "refresh_address", None)
                if present and callable(refresh):
                    try:
                        refresh(self.interface, address)
                    except BaseException:
                        self.backend.remove_address(self.interface, address)
                        continue
                elif not present:
                    if self.prefix is None or not self.backend.prefix_ready(
                        self.interface, self.prefix
                    ):
                        continue
                    self.backend.add_address(self.interface, address)
                self._records[info.lease_id] = _LeaseRecord(info=info, owner=owner)
                restored += 1
        return restored


class AddressdTransport(Protocol):
    def call(self, method: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


class UnixAddressdTransport:
    def __init__(self, socket_path: Optional[str] = None, *, timeout: float = 5.0) -> None:
        if not hasattr(socket, "AF_UNIX"):
            raise HostAliasError("this Python runtime does not support local Unix sockets")
        self.socket_path = socket_path or os.environ.get(
            "NEXUS_AGENT_ADDRESSD_SOCKET",
            r"C:\ProgramData\Nexus\agent-addressd.sock"
            if os.name == "nt" else "/run/nexus-agent/addressd.sock",
        )
        self.timeout = timeout

    def call(self, method: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        request = json.dumps(
            {"version": 1, "method": method, "params": dict(parameters)},
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(request) > 16384:
            raise HostAliasError("addressd request is too large")
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(self.timeout)
            connection.connect(self.socket_path)
            with connection:
                connection.sendall(request)
                response = bytearray()
                while len(response) <= 65536:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
                    if b"\n" in chunk:
                        break
        except OSError as exc:
            raise HostAliasError(
                f"cannot connect to nexus-agent-addressd at {self.socket_path}"
            ) from exc
        try:
            payload = json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HostAliasError("addressd returned an invalid response") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise HostAliasError("addressd response version is invalid")
        if payload.get("ok") is not True:
            raise HostAliasError(str(payload.get("error", "addressd request failed")))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise HostAliasError("addressd response result is invalid")
        return result


def default_addressd_transport() -> AddressdTransport:
    if os.name == "nt":
        from .windows_pipe import WindowsNamedPipeTransport

        return WindowsNamedPipeTransport()
    return UnixAddressdTransport()


class LocalAddressdClient:
    def __init__(
        self,
        transport: Optional[AddressdTransport] = None,
        *,
        owner: Optional[str] = None,
    ) -> None:
        self.transport = transport or default_addressd_transport()
        self.owner = owner or str(os.getuid() if hasattr(os, "getuid") else os.getpid())

    def allocate(
        self,
        *,
        tenant: str,
        agent_id: str,
        interface: str = "auto",
        prefix: str = "auto",
        lease_seconds: int = 300,
    ) -> "HostAliasLease":
        result = self.transport.call("allocate", {
            "owner": self.owner,
            "tenant": tenant,
            "agent_id": agent_id,
            "interface": interface,
            "prefix": prefix,
            "lease_seconds": lease_seconds,
        })
        return HostAliasLease(self, HostAliasLeaseInfo.from_dict(result))

    def confirm(self, lease_id: str) -> HostAliasLeaseInfo:
        result = self.transport.call("confirm_bound", {
            "owner": self.owner, "lease_id": lease_id,
        })
        return HostAliasLeaseInfo.from_dict(result)

    def renew(self, lease_id: str) -> HostAliasLeaseInfo:
        result = self.transport.call("renew", {
            "owner": self.owner, "lease_id": lease_id,
        })
        return HostAliasLeaseInfo.from_dict(result)

    def release(self, lease_id: str) -> None:
        self.transport.call("release", {
            "owner": self.owner, "lease_id": lease_id,
        })


class HostAliasLease:
    def __init__(self, client: LocalAddressdClient, info: HostAliasLeaseInfo) -> None:
        self.client = client
        self.info = info
        self.last_error: Optional[BaseException] = None
        self._closed = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    @property
    def address(self) -> str:
        return self.info.address

    def confirm(self) -> HostAliasLeaseInfo:
        with self._lock:
            if self._closed:
                raise HostAliasError("IPv6 lease is closed")
            self.info = self.client.confirm(self.info.lease_id)
            return self.info

    def renew(self) -> HostAliasLeaseInfo:
        with self._lock:
            if self._closed:
                raise HostAliasError("IPv6 lease is closed")
            self.info = self.client.renew(self.info.lease_id)
            return self.info

    def start_auto_renew(self, *, fraction: float = 0.6) -> None:
        if not 0.1 <= fraction <= 0.9:
            raise ValueError("renew fraction must be between 0.1 and 0.9")
        with self._lock:
            if self.info.state != "active":
                raise HostAliasError("confirm the IPv6 lease before auto-renew")
            if self._thread is not None:
                return

            def worker() -> None:
                interval = max(1.0, self.info.lease_seconds * fraction)
                while not self._stop.wait(interval):
                    try:
                        self.renew()
                    except BaseException as exc:
                        self.last_error = exc
                        return

            self._thread = threading.Thread(
                target=worker, name="nexus-ipv6-lease", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        try:
            self.client.release(self.info.lease_id)
        except BaseException as exc:
            self.last_error = exc

    def __enter__(self) -> "HostAliasLease":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
