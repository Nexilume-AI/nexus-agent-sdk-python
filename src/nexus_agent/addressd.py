"""Privileged local service for bounded Agent IPv6 host-alias leases."""

import argparse
import json
import os
import signal
import socket
import socketserver
import struct
import threading
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .host_alias import (
    HostAliasAllocator,
    HostAliasError,
    MemoryAddressBackend,
    system_address_backend,
)


class AddressdStateStore:
    def __init__(self, path: str, *, protect_windows: bool = False) -> None:
        self.path = Path(path)
        self.protect_windows = protect_windows

    def load(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    def save(self, values) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(list(values), sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        if os.name == "nt" and self.protect_windows:
            from .windows_pipe import protect_admin_file

            protect_admin_file(str(self.path))


class AddressdApplication:
    """Validate and dispatch the small versioned local IPC contract."""

    def __init__(
        self,
        allocator: HostAliasAllocator,
        *,
        state_store: Optional[AddressdStateStore] = None,
    ) -> None:
        self.allocator = allocator
        self.state_store = state_store
        if state_store is not None:
            allocator.restore(state_store.load())

    def _persist(self) -> None:
        if self.state_store is not None:
            self.state_store.save(self.allocator.snapshot())

    def sweep(self) -> int:
        removed = self.allocator.sweep()
        if removed:
            self._persist()
        return removed

    def dispatch(
        self,
        request: Mapping[str, Any],
        *,
        peer_owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        if request.get("version") != 1:
            raise HostAliasError("addressd protocol version must be 1")
        method = request.get("method")
        parameters = request.get("params")
        if not isinstance(method, str) or not isinstance(parameters, dict):
            raise HostAliasError("addressd request is invalid")
        supplied_owner = parameters.get("owner")
        owner = peer_owner or (
            str(supplied_owner) if supplied_owner is not None else ""
        )
        if not owner:
            raise HostAliasError("addressd could not identify the local caller")

        if method == "allocate":
            info = self.allocator.allocate(
                owner=owner,
                tenant=str(parameters.get("tenant", "")),
                agent_id=str(parameters.get("agent_id", "")),
                interface=str(parameters.get("interface", "auto")),
                prefix=str(parameters.get("prefix", "auto")),
                lease_seconds=int(parameters.get("lease_seconds", 300)),
            )
            self._persist()
            return info.to_dict()
        lease_id = str(parameters.get("lease_id", ""))
        if method == "confirm_bound":
            result = self.allocator.confirm(lease_id, owner=owner)
            self._persist()
            return result.to_dict()
        if method == "renew":
            try:
                result = self.allocator.renew(lease_id, owner=owner)
            finally:
                self._persist()
            return result.to_dict()
        if method == "release":
            self.allocator.release(lease_id, owner=owner)
            self._persist()
            return {"released": True}
        if method == "list":
            return {
                "leases": [item.to_dict() for item in self.allocator.list(owner=owner)]
            }
        if method == "status":
            result = {
                "status": "ok",
                "mode": getattr(self.allocator.backend, "mode", "routed-prefix"),
                "interface": self.allocator.interface,
                "prefix": str(self.allocator.prefix) if self.allocator.prefix else "dynamic",
                "leases": len(self.allocator.list()),
                "max_addresses": self.allocator.max_addresses,
            }
            relay_status = getattr(self.allocator.backend, "relay_status", None)
            if callable(relay_status):
                result["upstream_relay"] = relay_status()
            backend_status = getattr(self.allocator.backend, "backend_status", None)
            if callable(backend_status):
                result["address_backend"] = backend_status(probe=bool(parameters.get("probe", False)))
            return result
        raise HostAliasError("addressd method is not allowed")


def _peer_owner(connection: socket.socket, request: Mapping[str, Any]) -> str:
    if hasattr(socket, "SO_PEERCRED"):
        try:
            credentials = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _pid, uid, _gid = struct.unpack("3i", credentials)
            return f"uid:{uid}"
        except OSError:
            pass
    parameters = request.get("params")
    if isinstance(parameters, dict) and parameters.get("owner") is not None:
        return str(parameters["owner"])
    return ""


class _AddressdHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(16385)
        if not raw or len(raw) > 16384 or not raw.endswith(b"\n"):
            self._reply(False, error="request is empty or too large")
            return
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            result = self.server.application.dispatch(  # type: ignore[attr-defined]
                request,
                peer_owner=_peer_owner(self.connection, request),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, HostAliasError) as exc:
            self._reply(False, error=str(exc)[:512])
            return
        except Exception:
            self._reply(False, error="internal addressd failure")
            return
        self._reply(True, result=result)

    def _reply(
        self,
        ok: bool,
        *,
        result: Optional[Mapping[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        payload: Dict[str, Any] = {"version": 1, "ok": ok}
        if ok:
            payload["result"] = dict(result or {})
        else:
            payload["error"] = error or "request failed"
        self.wfile.write(json.dumps(
            payload, separators=(",", ":")
        ).encode("utf-8") + b"\n")


class _PortableUnixStreamServer(socketserver.TCPServer):
    """AF_UNIX stream server for Python builds lacking UnixStreamServer."""

    address_family = getattr(socket, "AF_UNIX", -1)


class AddressdUnixServer(socketserver.ThreadingMixIn, _PortableUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        socket_path: str,
        application: AddressdApplication,
        *,
        sweep_interval: float = 5.0,
        socket_group: Optional[str] = None,
    ) -> None:
        if not hasattr(socket, "AF_UNIX"):
            raise HostAliasError("this Python runtime does not support AF_UNIX sockets")
        if sweep_interval <= 0:
            raise ValueError("sweep_interval must be positive")
        self.application = application
        self.socket_path = socket_path
        self._sweep_interval = float(sweep_interval)
        self._sweep_stop = threading.Event()
        self._sweep_thread: Optional[threading.Thread] = None
        parent = os.path.dirname(socket_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.lexists(socket_path):
            if not os.path.isfile(socket_path) and not os.path.exists(socket_path):
                os.unlink(socket_path)
            else:
                try:
                    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    probe.settimeout(0.2)
                    probe.connect(socket_path)
                except OSError:
                    os.unlink(socket_path)
                else:
                    probe.close()
                    raise HostAliasError("another addressd instance is already running")
        super().__init__(socket_path, _AddressdHandler)
        if os.name != "nt":
            os.chmod(socket_path, 0o660)
            if socket_group:
                try:
                    import grp

                    group_id = grp.getgrnam(socket_group).gr_gid
                except (ImportError, KeyError) as exc:
                    super().server_close()
                    os.unlink(socket_path)
                    raise HostAliasError(
                        f"local socket group does not exist: {socket_group}"
                    ) from exc
                try:
                    os.chown(socket_path, -1, group_id)
                except OSError as exc:
                    super().server_close()
                    os.unlink(socket_path)
                    raise HostAliasError(
                        f"cannot assign local socket to group: {socket_group}"
                    ) from exc
        self._sweep_thread = threading.Thread(
            target=self._sweep_worker,
            name="nexus-addressd-sweeper",
            daemon=True,
        )
        self._sweep_thread.start()

    def _sweep_worker(self) -> None:
        while not self._sweep_stop.wait(self._sweep_interval):
            try:
                self.application.sweep()
            except Exception:
                # Keep serving allocations; a later sweep or request can retry.
                pass

    def server_close(self) -> None:
        self._sweep_stop.set()
        if self._sweep_thread is not None:
            self._sweep_thread.join(timeout=max(1.0, self._sweep_interval * 2))
            self._sweep_thread = None
        super().server_close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass


def _secret(path: str, *, protect_windows: bool = False) -> bytes:
    target = Path(path)
    try:
        value = target.read_bytes()
    except FileNotFoundError:
        target.parent.mkdir(parents=True, exist_ok=True)
        value = os.urandom(32)
        target.write_bytes(value)
        if os.name != "nt":
            os.chmod(target, 0o600)
    if len(value) < 32:
        raise HostAliasError("addressd allocation secret must contain at least 32 bytes")
    if os.name == "nt" and protect_windows:
        from .windows_pipe import protect_admin_file

        protect_admin_file(str(target))
    return value


def _defaults():
    if os.name == "nt":
        from .windows_pipe import DEFAULT_PIPE_NAME

        root = r"C:\ProgramData\Nexus"
        return (
            DEFAULT_PIPE_NAME,
            root + r"\addressd.secret",
            root + r"\addressd-state.json",
        )
    return (
        "/run/nexus-agent/addressd.sock",
        "/etc/nexus-agent/addressd.secret",
        "/var/lib/nexus-agent/addressd-state.json",
    )


def main(argv=None) -> int:
    socket_default, secret_default, state_default = _defaults()
    parser = argparse.ArgumentParser(
        description="Allocate one host IPv6 /128 per local Nexus Agent"
    )
    parser.add_argument("--interface", required=True)
    parser.add_argument("--prefix", default="dynamic")
    parser.add_argument("--socket", default=socket_default)
    parser.add_argument(
        "--pipe",
        help=r"Windows pipe name, default: \\.\pipe\nexus-agent-addressd",
    )
    parser.add_argument("--secret-file", default=secret_default)
    parser.add_argument("--state-file", default=state_default)
    parser.add_argument("--max-addresses", type=int, default=256)
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--reservation-seconds", type=int, default=15)
    parser.add_argument(
        "--socket-group",
        help="Unix group allowed to request Agent addresses",
    )
    parser.add_argument(
        "--pipe-group",
        default="Nexus Agent Users",
        help="Windows local group allowed to request Agent addresses",
    )
    parser.add_argument("--memory-backend", action="store_true")
    parser.add_argument(
        "--upstream-relay",
        action="store_true",
        help="accept /128 aliases only from the exact /64 advertised by the upstream router",
    )
    parser.add_argument(
        "--dhcpv6-ia-na",
        action="store_true",
        help="request one server-assigned DHCPv6 IA_NA /128 per Agent",
    )
    parser.add_argument(
        "--dhcpv6-duid-file",
        default="/var/lib/nexus-agent/dhcpv6-duid",
    )
    arguments = parser.parse_args(argv)

    selected_backends = sum(bool(value) for value in (
        arguments.memory_backend,
        arguments.upstream_relay,
        arguments.dhcpv6_ia_na,
    ))
    if selected_backends > 1:
        parser.error("address backend mode flags are mutually exclusive")
    if not arguments.dhcpv6_ia_na and arguments.prefix == "dynamic":
        parser.error("--prefix is required unless --dhcpv6-ia-na is enabled")
    if arguments.memory_backend:
        backend = MemoryAddressBackend([(arguments.interface, arguments.prefix)])
    elif arguments.upstream_relay:
        if os.name == "nt":
            parser.error("--upstream-relay is available only on Linux")
        from .upstream_relay import UpstreamRelayLinuxBackend

        backend = UpstreamRelayLinuxBackend(
            interface=arguments.interface,
            prefix=arguments.prefix,
        )
    elif arguments.dhcpv6_ia_na:
        if os.name == "nt":
            parser.error("--dhcpv6-ia-na is available only on Linux")
        from .dhcpv6_iana import Dhcpv6IaNaLinuxBackend, load_or_create_duid

        backend = Dhcpv6IaNaLinuxBackend(
            interface=arguments.interface,
            client_id=load_or_create_duid(arguments.dhcpv6_duid_file),
        )
    else:
        backend = system_address_backend()
    allocator = HostAliasAllocator(
        backend=backend,
        interface=arguments.interface,
        prefix=arguments.prefix,
        allocation_secret=_secret(
            arguments.secret_file, protect_windows=os.name == "nt"
        ),
        max_addresses=arguments.max_addresses,
        default_lease_seconds=arguments.lease_seconds,
        reservation_seconds=arguments.reservation_seconds,
    )
    application = AddressdApplication(
        allocator,
        state_store=AddressdStateStore(
            arguments.state_file, protect_windows=os.name == "nt"
        ),
    )
    if os.name == "nt":
        from .windows_pipe import AddressdNamedPipeServer

        server = AddressdNamedPipeServer(
            arguments.pipe or arguments.socket,
            application,
            allowed_group=arguments.pipe_group,
        )
    else:
        server = AddressdUnixServer(
            arguments.socket,
            application,
            socket_group=arguments.socket_group,
        )
    stopped = threading.Event()

    def stop(_signum, _frame) -> None:
        if not stopped.is_set():
            stopped.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
