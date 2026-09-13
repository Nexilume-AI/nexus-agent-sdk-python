"""One-command Linux setup and diagnostics for per-Agent IPv6 aliases."""

from __future__ import annotations

import getpass

import ipaddress
import json
import os
import pathlib

import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    import grp
    import pwd
except ImportError:  # Modules are unavailable on Windows; this module is lazy-loaded.
    grp = None  # type: ignore[assignment]
    pwd = None  # type: ignore[assignment]

from .direct_ipv6 import DirectIPv6Agent
from .host_alias import HostAliasError, LinuxAddressBackend, UnixAddressdTransport
from .public_ipv6_agent import PublicIPv6Agent
from .server_auth import HmacJwtServerAuth


SERVICE_NAME = "nexus-agent-addressd.service"
GROUP_NAME = "nexus-agent"
SOCKET_PATH = "/run/nexus-agent/addressd.sock"


@dataclass(frozen=True)
class LinuxIPv6Candidate:
    interface: str
    interface_index: int
    prefix: str
    current_address: str
    mode: str = "routed-prefix"
    upstream_router: str = ""
    valid_lifetime: int = 0
    preferred_lifetime: int = 0


@dataclass(frozen=True)
class LinuxInstallPaths:
    config: pathlib.Path = pathlib.Path("/etc/nexus-agent/addressd.json")
    service: pathlib.Path = pathlib.Path("/etc/systemd/system") / SERVICE_NAME
    runtime: pathlib.Path = pathlib.Path("/opt/nexus-agent/addressd-runtime")


class LinuxCommandBackend:
    """Argument-vector-only command runner used by setup and doctor."""

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def run(self, arguments: Sequence[str], *, check: bool = True) -> str:
        try:
            completed = subprocess.run(
                list(arguments),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostAliasError(f"could not run {arguments[0]}") from exc
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise HostAliasError(detail[:512] or f"{arguments[0]} failed")
        return completed.stdout

    def json(self, arguments: Sequence[str]) -> Any:
        output = self.run(arguments)
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise HostAliasError(f"{arguments[0]} returned invalid JSON") from exc


def _linux_only() -> None:
    if not sys.platform.startswith("linux"):
        raise HostAliasError("Linux IPv6 setup is available only on Linux")


def _global_64(value: str) -> Optional[ipaddress.IPv6Network]:
    try:
        prefix = ipaddress.IPv6Network(value, strict=False)
    except ValueError:
        return None
    if prefix.prefixlen != 64 or not prefix.is_global:
        return None
    return prefix


def discover_linux_candidates(
    backend: Optional[LinuxCommandBackend] = None,
) -> Tuple[LinuxIPv6Candidate, ...]:
    """Discover exact global /64 routes and their Linux interfaces."""

    _linux_only()
    backend = backend or LinuxCommandBackend()
    routes = backend.json(["ip", "-j", "-6", "route", "show"])
    if not isinstance(routes, list):
        raise HostAliasError("iproute2 returned an invalid IPv6 route table")
    found = set()
    candidates: List[LinuxIPv6Candidate] = []
    for route in routes:
        if not isinstance(route, Mapping):
            continue
        interface = str(route.get("dev", ""))
        prefix = _global_64(str(route.get("dst", "")))
        protocol = str(route.get("protocol", route.get("proto", ""))).lower()
        if (
            not interface
            or prefix is None
            or protocol == "ra"
            or (interface, str(prefix)) in found
        ):
            continue
        addresses = backend.json([
            "ip", "-j", "-6", "address", "show", "dev", interface,
            "scope", "global",
        ])
        current = ""
        if isinstance(addresses, list):
            for record in addresses:
                if not isinstance(record, Mapping):
                    continue
                for info in record.get("addr_info", []):
                    if not isinstance(info, Mapping) or info.get("family") != "inet6":
                        continue
                    try:
                        address = ipaddress.IPv6Address(str(info.get("local", "")))
                    except ValueError:
                        continue
                    if address.is_global and address in prefix:
                        current = address.compressed
                        break
                if current:
                    break
        found.add((interface, str(prefix)))
        try:
            interface_index = socket.if_nametoindex(interface)
        except OSError:
            interface_index = int(route.get("ifindex", 0) or 0)
        candidates.append(LinuxIPv6Candidate(
            interface=interface,
            interface_index=interface_index,
            prefix=str(prefix),
            current_address=current,
        ))
    return tuple(sorted(
        candidates,
        key=lambda item: (item.interface_index, item.interface, item.prefix),
    ))


def discover_upstream_relay_candidates(
    backend: Optional[LinuxCommandBackend] = None,
    *,
    discovery: Optional[Any] = None,
    interface: Optional[str] = None,
) -> Tuple[LinuxIPv6Candidate, ...]:
    """Discover autonomous global /64s explicitly advertised by an upstream router."""

    _linux_only()
    backend = backend or LinuxCommandBackend()
    if discovery is None:
        from .upstream_relay import RouterAdvertisementDiscovery

        discovery = RouterAdvertisementDiscovery()
    records = backend.json(["ip", "-j", "-6", "address", "show", "scope", "global"])
    if not isinstance(records, list):
        raise HostAliasError("iproute2 returned an invalid IPv6 address table")
    addresses: Dict[str, List[ipaddress.IPv6Address]] = {}
    interfaces = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        name = str(record.get("ifname", ""))
        if not name or (interface and name != interface):
            continue
        interfaces.add(name)
        for info in record.get("addr_info", []):
            if not isinstance(info, Mapping) or info.get("family") != "inet6":
                continue
            try:
                address = ipaddress.IPv6Address(str(info.get("local", "")))
            except ValueError:
                continue
            if address.is_global:
                addresses.setdefault(name, []).append(address)
    if interface:
        interfaces.add(interface)
    selected: Dict[Tuple[str, str], LinuxIPv6Candidate] = {}
    for name in sorted(interfaces):
        for advertisement in discovery.discover(name):
            try:
                prefix = ipaddress.IPv6Network(advertisement.prefix, strict=True)
            except ValueError:
                continue
            if (
                prefix.prefixlen != 64
                or not prefix.is_global
                or not advertisement.autonomous
                or int(advertisement.valid_lifetime) <= 0
                or int(advertisement.preferred_lifetime) <= 0
            ):
                continue
            current = next(
                (item.compressed for item in addresses.get(name, []) if item in prefix),
                (addresses.get(name, [None])[0].compressed if addresses.get(name) else ""),
            )
            candidate = LinuxIPv6Candidate(
                interface=name,
                interface_index=int(advertisement.interface_index),
                prefix=str(prefix),
                current_address=current,
                mode="upstream-relay",
                upstream_router=str(advertisement.router),
                valid_lifetime=int(advertisement.valid_lifetime),
                preferred_lifetime=int(advertisement.preferred_lifetime),
            )
            key = (name, str(prefix))
            existing = selected.get(key)
            if existing is None or candidate.valid_lifetime > existing.valid_lifetime:
                selected[key] = candidate
    return tuple(sorted(
        selected.values(),
        key=lambda item: (item.interface_index, item.interface, item.prefix),
    ))

def discover_dhcpv6_ia_na_candidates(
    backend: Optional[LinuxCommandBackend] = None,
    *,
    client: Optional[Any] = None,
    client_id: Optional[bytes] = None,
    interface: Optional[str] = None,
    errors: Optional[List[str]] = None,
) -> Tuple[LinuxIPv6Candidate, ...]:
    """Probe DHCPv6 IA_NA without committing an address binding."""

    _linux_only()
    backend = backend or LinuxCommandBackend()
    if client is None or client_id is None:
        from .dhcpv6_iana import Dhcpv6IaNaClient, preview_duid

        client = client or Dhcpv6IaNaClient()
        client_id = client_id or preview_duid()
    interfaces = set()
    if interface:
        interfaces.add(interface)
    else:
        routes = backend.json(["ip", "-j", "-6", "route", "show", "default"])
        if isinstance(routes, list):
            for route in routes:
                if isinstance(route, Mapping) and route.get("dev"):
                    interfaces.add(str(route["dev"]))
        if not interfaces:
            records = backend.json(["ip", "-j", "-6", "address", "show", "scope", "link"])
            if isinstance(records, list):
                for record in records:
                    if isinstance(record, Mapping):
                        name = str(record.get("ifname", ""))
                        if name and name != "lo":
                            interfaces.add(name)
    if not interfaces and errors is not None:
        errors.append("no IPv6 default or link-scope interface was found")
    from .dhcpv6_iana import stable_iaid

    result: List[LinuxIPv6Candidate] = []
    for name in sorted(interfaces):
        iaid = stable_iaid(client_id, name, "setup", "nexus-setup", "capability-probe")
        try:
            offer = client.probe(
                name, client_id=client_id, iaid=iaid, timeout=6.0
            )
        except HostAliasError as exc:
            if errors is not None:
                errors.append(f"{name}: {exc}")
            continue
        address = ipaddress.IPv6Address(offer.address)
        if not address.is_global or offer.valid_lifetime <= 0:
            continue
        try:
            interface_index = socket.if_nametoindex(name)
        except OSError:
            interface_index = 0
        result.append(LinuxIPv6Candidate(
            interface=name,
            interface_index=interface_index,
            prefix=f"{address.compressed}/128",
            current_address=address.compressed,
            mode="dhcpv6-ia-na",
            upstream_router=str(offer.server_address),
            valid_lifetime=int(offer.valid_lifetime),
            preferred_lifetime=int(offer.preferred_lifetime),
        ))
    return tuple(sorted(
        result,
        key=lambda item: (item.interface_index, item.interface, item.prefix),
    ))


def select_linux_candidate(
    candidates: Sequence[LinuxIPv6Candidate],
    *,
    interface: Optional[str],
    prefix: Optional[str],
) -> LinuxIPv6Candidate:
    selected = list(candidates)
    if interface:
        selected = [item for item in selected if item.interface == interface]
    if prefix:
        try:
            normalized = ipaddress.IPv6Network(prefix, strict=True)
        except ValueError as exc:
            raise HostAliasError("--prefix must be a canonical global IPv6 network") from exc
        if normalized.prefixlen not in (64, 128) or not normalized.is_global:
            raise HostAliasError("--prefix must be a global IPv6 /64 or DHCPv6 /128")
        selected = [item for item in selected if item.prefix == str(normalized)]
    if not selected:
        raise HostAliasError(
            "no usable IPv6 address source matched; verify an owned routed prefix, "
            "an upstream RA autonomous /64, or a DHCPv6 IA_NA server"
        )
    if len(selected) == 1:
        return selected[0]
    if not sys.stdin.isatty():
        raise HostAliasError("multiple IPv6 networks found; specify --interface and --prefix")
    print("Multiple usable IPv6 networks were found:")
    for number, item in enumerate(selected, 1):
        address = item.current_address or "routed prefix"
        print(f"  {number}. {item.interface}  {item.prefix}  ({address})")
    while True:
        answer = input("Select network [1]: ").strip() or "1"
        if answer.isdigit() and 1 <= int(answer) <= len(selected):
            return selected[int(answer) - 1]
        print("Enter one of the listed numbers.")


def _port_bindable(port: int) -> bool:
    probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        probe.bind(("::1", port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def choose_linux_port(requested: Optional[int]) -> int:
    choices = [requested] if requested is not None else [9443, 20043, 20443, 25443, 30043]
    for port in choices:
        if port is None or not 1 <= port <= 65535:
            continue
        if _port_bindable(port):
            return port
        if requested is not None:
            raise HostAliasError(f"TCP port {port} is already in use or unavailable")
    raise HostAliasError("could not find a usable Agent TCP port")


def _invoking_user() -> Optional[str]:
    if pwd is None:
        raise HostAliasError("Linux user database is unavailable")
    for value in (os.environ.get("SUDO_USER"), os.environ.get("PKEXEC_UID")):
        if value and value != "root":
            if value.isdigit():
                try:
                    return pwd.getpwuid(int(value)).pw_name
                except KeyError:
                    continue
            return value
    user = getpass.getuser()
    return user if user != "root" else None


def _validate_users(users: Iterable[str]) -> Tuple[str, ...]:
    users = tuple(users)
    if not users:
        return ()
    if pwd is None:
        raise HostAliasError("Linux user database is unavailable")
    result = []
    for user in users:
        if not user or user == "root" or user in result:
            continue
        try:
            pwd.getpwnam(user)
        except KeyError as exc:
            raise HostAliasError(f"Linux user does not exist: {user}") from exc
        result.append(user)
    return tuple(result)


def _runtime_python() -> str:
    executable = (
        "/usr/bin/python3"
        if pathlib.Path("/usr/bin/python3").is_file()
        else shutil.which("python3")
    )
    if not executable:
        raise HostAliasError("python3 is required to install nexus-agent-addressd")
    executable = os.path.realpath(executable)
    try:
        completed = subprocess.run(
            [executable, "-c", "import sys; raise SystemExit(sys.version_info < (3, 9))"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HostAliasError("could not validate the system python3 runtime") from exc
    if completed.returncode != 0:
        raise HostAliasError("system python3 3.9 or newer is required")
    return executable


def _stage_runtime(paths: LinuxInstallPaths) -> None:
    source = pathlib.Path(__file__).resolve().parent
    paths.runtime.parent.mkdir(parents=True, exist_ok=True)
    temporary = pathlib.Path(tempfile.mkdtemp(
        prefix="addressd-runtime-", dir=str(paths.runtime.parent)
    ))
    try:
        shutil.copytree(
            source,
            temporary / "nexus_agent",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        marker = temporary / "VERSION"
        marker.write_text("0.23.0\n", encoding="ascii")
        os.chmod(marker, 0o644)
        if paths.runtime.exists():
            shutil.rmtree(paths.runtime)
        os.replace(temporary, paths.runtime)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _service_text(candidate: LinuxIPv6Candidate, python: str, paths: LinuxInstallPaths) -> str:
    if candidate.mode == "upstream-relay":
        mode_flag = " --upstream-relay"
        prefix = candidate.prefix
        capabilities = "CAP_NET_ADMIN CAP_NET_RAW"
    elif candidate.mode == "dhcpv6-ia-na":
        mode_flag = " --dhcpv6-ia-na"
        prefix = "dynamic"
        capabilities = "CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE"
    else:
        mode_flag = ""
        prefix = candidate.prefix
        capabilities = "CAP_NET_ADMIN"
    return f"""[Unit]
Description=Nexus per-Agent IPv6 address allocator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PYTHONPATH={paths.runtime.as_posix()}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={python} -m nexus_agent.addressd --interface {candidate.interface} --prefix {prefix} --socket {SOCKET_PATH} --socket-group {GROUP_NAME}{mode_flag}
Restart=on-failure
RestartSec=2
RuntimeDirectory=nexus-agent
RuntimeDirectoryMode=0755
StateDirectory=nexus-agent
ConfigurationDirectory=nexus-agent
NoNewPrivileges=true
CapabilityBoundingSet={capabilities}
AmbientCapabilities={capabilities}
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/etc/nexus-agent /var/lib/nexus-agent /run/nexus-agent

[Install]
WantedBy=multi-user.target
"""


def _atomic_write(path: pathlib.Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _load_configuration(paths: LinuxInstallPaths = LinuxInstallPaths()) -> Dict[str, Any]:
    try:
        value = json.loads(paths.config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HostAliasError("Linux addressd configuration is missing or invalid") from exc
    if not isinstance(value, dict):
        raise HostAliasError("Linux addressd configuration is invalid")
    return value


def _existing_leases(paths: LinuxInstallPaths) -> int:
    try:
        configuration = _load_configuration(paths)
        status = UnixAddressdTransport(
            str(configuration.get("socket", SOCKET_PATH)), timeout=1.0
        ).call("status", {"owner": "setup"})
        return int(status.get("leases", 0))
    except HostAliasError:
        return 0


def install_linux_service(
    candidate: LinuxIPv6Candidate,
    *,
    port: int,
    allowed_users: Sequence[str],
    backend: Optional[LinuxCommandBackend] = None,
    paths: LinuxInstallPaths = LinuxInstallPaths(),
) -> None:
    _linux_only()
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise HostAliasError("Linux setup requires root privileges")
    backend = backend or LinuxCommandBackend()
    if _existing_leases(paths):
        raise HostAliasError("addressd has active leases; stop those Agents before reconfiguration")
    backend.run(["systemctl", "stop", SERVICE_NAME], check=False)
    backend.run(["groupadd", "--system", "--force", GROUP_NAME])
    users = _validate_users(allowed_users)
    for user in users:
        backend.run(["usermod", "--append", "--groups", GROUP_NAME, user])
    _stage_runtime(paths)
    python = _runtime_python()
    _atomic_write(paths.service, _service_text(candidate, python, paths), 0o644)
    configuration = {
        "version": 1,
        "mode": candidate.mode,
        "interface": candidate.interface,
        "prefix": "dynamic" if candidate.mode == "dhcpv6-ia-na" else candidate.prefix,
        "offered_address": candidate.current_address if candidate.mode == "dhcpv6-ia-na" else None,
        "upstream_router": candidate.upstream_router or None,
        "ra_valid_lifetime": candidate.valid_lifetime or None,
        "ra_preferred_lifetime": candidate.preferred_lifetime or None,
        "socket": SOCKET_PATH,
        "socket_group": GROUP_NAME,
        "recommended_port": port,
        "runtime": str(paths.runtime),
        "allowed_users": list(users),
    }
    _atomic_write(paths.config, json.dumps(configuration, indent=2) + "\n", 0o644)
    backend.run(["systemctl", "daemon-reload"])
    backend.run(["systemctl", "enable", "--now", SERVICE_NAME])


def _wait_for_socket(socket_path: str, timeout: float = 12.0) -> Dict[str, Any]:
    transport = UnixAddressdTransport(socket_path, timeout=1.0)
    deadline = time.monotonic() + timeout
    last_error: Optional[BaseException] = None
    while time.monotonic() < deadline:
        try:
            return dict(transport.call("status", {"owner": "setup"}))
        except HostAliasError as exc:
            last_error = exc
            time.sleep(0.25)
    raise HostAliasError("addressd Unix Socket did not become ready") from last_error


def _self_test(port: int) -> Dict[str, Any]:
    source = "agent://nexus-setup/linux-doctor"
    auth = HmacJwtServerAuth(
        os.urandom(32).hex(),
        issuer="urn:nexus:setup",
        audience="nexus-ipv6-self-test",
        max_lifetime_seconds=120,
    )
    token = auth.issue(
        subject="nexus-setup",
        tenant="nexus-setup",
        source_agent=source,
        expires_in=120,
    )
    agent = PublicIPv6Agent(
        "auto", auth=auth, tenant="nexus-setup", agent_id="ipv6-self-test", port=port
    )

    @agent.capability("nexus.self-test")
    def self_test(payload):
        return {"ok": True, "echo": payload}

    handle = agent.start(announce=False)
    try:
        target = DirectIPv6Agent.plain_http(
            agent.address, port=port, token=token, timeout=5.0
        )
        result = target.invoke(
            "nexus.self-test",
            {"probe": "linux-host-alias"},
            tenant="nexus-setup",
            source_agent=source,
        )
        return {"address": agent.address, "port": port, "result": result}
    finally:
        handle.close()


def setup_linux(arguments: Any) -> int:
    _linux_only()
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise HostAliasError("Linux setup did not receive root privileges")
    backend = LinuxCommandBackend(timeout=20.0)
    mode = str(getattr(arguments, "mode", "auto"))
    print("[1/6] Discovering usable global IPv6 networks...")
    candidates: Tuple[LinuxIPv6Candidate, ...] = ()
    if mode in ("auto", "routed-prefix"):
        candidates = discover_linux_candidates(backend)
    if mode == "upstream-relay" or (mode == "auto" and not candidates):
        candidates = discover_upstream_relay_candidates(
            backend,
            interface=arguments.interface,
        )
    dhcp_errors: List[str] = []
    if mode == "dhcpv6-ia-na" or (mode == "auto" and not candidates):
        candidates = discover_dhcpv6_ia_na_candidates(
            backend,
            interface=arguments.interface,
            errors=dhcp_errors,
        )
    if not candidates and dhcp_errors:
        detail = "; ".join(dhcp_errors[:4])
        raise HostAliasError(f"DHCPv6 IA_NA discovery failed: {detail}")
    candidate = select_linux_candidate(
        candidates,
        interface=arguments.interface,
        prefix=arguments.prefix,
    )
    detail = f"{candidate.interface}  {candidate.prefix}  mode={candidate.mode}"
    if candidate.upstream_router:
        role = "server" if candidate.mode == "dhcpv6-ia-na" else "router"
        detail += f"  {role}={candidate.upstream_router}"
    print(f"      {detail}")

    print("[2/6] Selecting an available TCP port...")
    port = choose_linux_port(arguments.port)
    print(f"      {port}")

    print("[3/6] Installing a machine-owned addressd runtime...")
    default_user = _invoking_user()
    users = arguments.allow_user or ([default_user] if default_user else [])
    install_linux_service(
        candidate, port=port, allowed_users=users, backend=backend
    )

    print("[4/6] Starting nexus-agent-addressd with systemd...")
    status = _wait_for_socket(SOCKET_PATH)
    print(f"      Active, leases={status.get('leases', 0)}")

    print("[5/6] Running a short-lived Agent /128 allocation and invoke self-test...")
    if arguments.skip_self_test:
        print("      Skipped by request")
    else:
        result = _self_test(port)
        print(f"      Passed on [{result['address']}]:{port}; address released")

    print("[6/6] Setup complete")
    print()
    print("Python Agent example:")
    print("from nexus_agent import NexusAgent")
    print("agent = NexusAgent.public_ipv6(")
    print(f"    'auto', auth='none', tenant='demo', agent_id='agent-1', port={port},")
    print(")")
    if users:
        print()
        print(f"Sign out and back in once so {', '.join(users)} receives the {GROUP_NAME} group.")
    print("No permanent firewall rule was created or changed.")
    print("Run 'nexus-agent ipv6 doctor' after starting a new login session.")
    return 0


def run_elevated_linux(arguments: Sequence[str]) -> int:
    _linux_only()
    sudo = shutil.which("sudo")
    if not sudo:
        raise HostAliasError(
            "sudo is required for one-command setup; run the same command as root"
        )
    package_root = str(pathlib.Path(__file__).resolve().parent.parent)
    bootstrap = (
        "import sys;"
        f"sys.path.insert(0, {package_root!r});"
        "from nexus_agent.ipv6_cli import main;"
        "raise SystemExit(main())"
    )
    print("Opening sudo for Linux IPv6 setup...")
    completed = subprocess.run(
        [sudo, sys.executable, "-c", bootstrap, *arguments],
        check=False,
        shell=False,
    )
    return int(completed.returncode)


def _current_user_in_group(group_name: str) -> bool:
    if grp is None or pwd is None:
        return False
    try:
        group = grp.getgrnam(group_name)
        user = pwd.getpwuid(os.getuid())
    except (KeyError, OSError):
        return False
    return user.pw_name in group.gr_mem or user.pw_gid == group.gr_gid or group.gr_gid in os.getgroups()


def _print_checks(checks: Sequence[Tuple[str, bool, str]], json_output: bool) -> int:
    if json_output:
        print(json.dumps([
            {"name": name, "ok": ok, "detail": detail}
            for name, ok, detail in checks
        ], ensure_ascii=False, indent=2))
    else:
        width = max(len(name) for name, _ok, _detail in checks)
        for name, ok, detail in checks:
            print(f"{name:<{width}}  {'OK' if ok else 'FAIL'}  {detail}")
    return 0 if all(ok for _name, ok, _detail in checks) else 1


def doctor_linux(arguments: Any) -> int:
    _linux_only()
    checks: List[Tuple[str, bool, str]] = []
    configuration: Optional[Dict[str, Any]] = None
    try:
        configuration = _load_configuration()
        checks.append(("Configuration", True, "system configuration loaded"))
    except HostAliasError:
        checks.append((
            "Configuration", False,
            "run 'nexus-agent ipv6 setup' to configure addressd",
        ))

    backend = LinuxCommandBackend(timeout=5.0)
    try:
        state = backend.run(["systemctl", "is-active", SERVICE_NAME], check=False).strip()
    except HostAliasError as exc:
        state = str(exc)
    checks.append(("Addressd service", state == "active", state or "not installed"))
    if configuration:
        status: Optional[Dict[str, Any]] = None
        try:
            status = UnixAddressdTransport(
                str(configuration.get("socket", SOCKET_PATH)), timeout=2.0
            ).call("status", {"owner": "doctor", "probe": True})
            checks.append((
                "Unix Socket", True,
                f"ready; leases={status.get('leases', 0)}/{status.get('max_addresses', '?')}",
            ))
        except Exception as exc:
            checks.append(("Unix Socket", False, str(exc)))
        if configuration.get("mode") == "dhcpv6-ia-na":
            dhcp = status.get("address_backend", {}) if status else {}
            ready = bool(dhcp.get("ready"))
            servers = dhcp.get("servers", [])
            server = servers[0] if servers else dhcp.get("server", "not discovered")
            detail = (
                f"{configuration['interface']}  server={server}  "
                f"bindings={dhcp.get('bindings', 0)}"
                if ready else str(dhcp.get("last_error", "DHCPv6 server unavailable"))
            )
            checks.append(("DHCPv6 IA_NA", ready, detail))
        elif configuration.get("mode") == "upstream-relay":
            relay = status.get("upstream_relay", {}) if status else {}
            ready = bool(relay.get("ready"))
            detail = (
                f"{configuration['interface']}  {configuration['prefix']}  "
                f"via {relay.get('router', 'upstream RA unavailable')}"
                if ready else str(relay.get("detail", "addressd status unavailable"))
            )
            checks.append(("Upstream IPv6 relay", ready, detail))
        else:
            try:
                prefix = ipaddress.IPv6Network(configuration["prefix"], strict=True)
                ready = LinuxAddressBackend(timeout=5.0).prefix_ready(
                    str(configuration["interface"]), prefix
                )
                checks.append((
                    "Global prefix", ready,
                    f"{configuration['interface']}  {configuration['prefix']}",
                ))
            except Exception as exc:
                checks.append(("Global prefix", False, str(exc)))
        port = int(configuration.get("recommended_port", 9443))
        checks.append((
            "Recommended port", 1 <= port <= 65535,
            str(port) if 1 <= port <= 65535 else "invalid port",
        ))
        in_group = _current_user_in_group(str(configuration.get("socket_group", GROUP_NAME)))
        checks.append((
            "Current user", in_group or os.geteuid() == 0,
            "root" if os.geteuid() == 0 else (
                f"member of {GROUP_NAME}" if in_group else f"sign out/in after joining {GROUP_NAME}"
            ),
        ))
    return _print_checks(checks, bool(arguments.json))
