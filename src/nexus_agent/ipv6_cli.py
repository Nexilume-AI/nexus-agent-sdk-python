"""Friendly setup and diagnostics for one-Agent-one-IPv6 host aliases."""

import argparse
import contextlib
import ctypes
import getpass
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .direct_ipv6 import DirectIPv6Agent
from .errors import NexusAgentError
from .host_alias import HostAliasError, WindowsAddressBackend
from .public_ipv6_agent import PublicIPv6Agent
from .server_auth import HmacJwtServerAuth
from .windows_pipe import WindowsNamedPipeTransport


@dataclass(frozen=True)
class IPv6Candidate:
    interface: str
    interface_index: int
    prefix: str
    current_address: str


def _windows_only() -> None:
    if os.name != "nt":
        raise HostAliasError(
            "this IPv6 operation requires Windows"
        )


def _is_administrator() -> bool:
    if os.name != "nt":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _run_elevated(arguments: Sequence[str]) -> int:
    _windows_only()
    try:
        import win32event
        import win32process
        from win32com.shell import shell, shellcon
    except ImportError as exc:
        raise HostAliasError(
            "automatic UAC requires: pip install 'nexus-openwrt-agent-sdk[windows]'"
        ) from exc
    command_line = subprocess.list2cmdline([
        "-m", "nexus_agent.ipv6_cli", *arguments,
    ])
    try:
        process = shell.ShellExecuteEx(
            fMask=shellcon.SEE_MASK_NOCLOSEPROCESS,
            lpVerb="runas",
            lpFile=sys.executable,
            lpParameters=command_line,
            nShow=1,
        )
    except Exception as exc:
        if getattr(exc, "winerror", None) == 1223:
            raise HostAliasError("Windows UAC was cancelled") from exc
        raise HostAliasError("could not open Windows UAC") from exc
    handle = process["hProcess"]
    win32event.WaitForSingleObject(handle, win32event.INFINITE)
    return int(win32process.GetExitCodeProcess(handle))


def _parse_interfaces(output: str) -> Dict[int, str]:
    interfaces: Dict[int, str] = {}
    for line in output.splitlines():
        columns = line.strip().split(None, 4)
        if len(columns) == 5 and columns[0].isdigit():
            interfaces[int(columns[0])] = columns[4]
    return interfaces


def _parse_global_64_routes(output: str) -> List[Tuple[int, ipaddress.IPv6Network]]:
    routes = set()
    for line in output.splitlines():
        tokens = line.split()
        for position, token in enumerate(tokens[:-1]):
            if "/" not in token:
                continue
            try:
                prefix = ipaddress.IPv6Network(token, strict=False)
                interface_index = int(tokens[position + 1])
            except (ValueError, IndexError):
                continue
            if prefix.prefixlen == 64 and prefix.is_global:
                routes.add((interface_index, prefix))
    return sorted(routes, key=lambda item: (item[0], int(item[1].network_address)))


def _parse_ipv6_addresses(output: str) -> Tuple[ipaddress.IPv6Address, ...]:
    addresses = set()
    for line in output.splitlines():
        for token in line.replace("[", " ").replace("]", " ").split():
            candidate = token.strip("(),;")
            if ":" not in candidate:
                continue
            if "%" in candidate:
                candidate = candidate.split("%", 1)[0]
            try:
                address = ipaddress.IPv6Address(candidate)
            except ValueError:
                continue
            addresses.add(address)
    return tuple(sorted(addresses, key=int))


def discover_windows_candidates(
    backend: Optional[WindowsAddressBackend] = None,
) -> Tuple[IPv6Candidate, ...]:
    _windows_only()
    backend = backend or WindowsAddressBackend()
    interfaces = _parse_interfaces(
        backend._run(["netsh", "interface", "ipv6", "show", "interface"])
    )
    routes = _parse_global_64_routes(
        backend._run(["netsh", "interface", "ipv6", "show", "route"])
    )
    found = set()
    candidates = []
    for interface_index, prefix in routes:
        interface = interfaces.get(interface_index)
        if not interface or (interface_index, str(prefix)) in found:
            continue
        addresses = _parse_ipv6_addresses(
            backend._run([
                "netsh", "interface", "ipv6", "show", "address",
                f"interface={interface_index}",
            ])
        )
        current = next(
            (address for address in addresses if address.is_global and address in prefix),
            None,
        )
        if current is None:
            continue
        found.add((interface_index, str(prefix)))
        candidates.append(IPv6Candidate(
            interface=interface,
            interface_index=interface_index,
            prefix=str(prefix),
            current_address=current.compressed,
        ))
    return tuple(candidates)


def _parse_excluded_ports(output: str) -> Tuple[Tuple[int, int], ...]:
    ranges = set()
    for line in output.splitlines():
        columns = line.split()
        if len(columns) >= 2 and columns[0].isdigit() and columns[1].isdigit():
            start, end = int(columns[0]), int(columns[1])
            if 1 <= start <= end <= 65535:
                ranges.add((start, end))
    return tuple(sorted(ranges))


def _excluded_ports(backend: WindowsAddressBackend) -> Tuple[Tuple[int, int], ...]:
    return _parse_excluded_ports(backend._run([
        "netsh", "interface", "ipv6", "show", "excludedportrange", "protocol=tcp",
    ]))


def _port_bindable(port: int) -> bool:
    probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        probe.bind(("::1", port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def choose_port(
    requested: Optional[int],
    excluded: Sequence[Tuple[int, int]],
) -> int:
    choices = [requested] if requested is not None else [9443, 20043, 20443, 25443, 30043]
    for port in choices:
        if port is None or not 1 <= port <= 65535:
            continue
        if any(start <= port <= end for start, end in excluded):
            if requested is not None:
                raise HostAliasError(f"TCP port {port} is reserved by Windows/Hyper-V")
            continue
        if _port_bindable(port):
            return port
        if requested is not None:
            raise HostAliasError(f"TCP port {port} is already in use or unavailable")
    raise HostAliasError("could not find a usable Agent TCP port")


def _select_candidate(
    candidates: Sequence[IPv6Candidate],
    *,
    interface: Optional[str],
    prefix: Optional[str],
) -> IPv6Candidate:
    selected = list(candidates)
    if interface:
        selected = [item for item in selected if item.interface == interface]
    if prefix:
        try:
            normalized = str(ipaddress.IPv6Network(prefix, strict=True))
        except ValueError as exc:
            raise HostAliasError("--prefix must be a canonical IPv6 /64") from exc
        selected = [item for item in selected if item.prefix == normalized]
    if not selected:
        raise HostAliasError("no usable global on-link IPv6 /64 matched the request")
    if len(selected) == 1:
        return selected[0]
    if not sys.stdin.isatty():
        raise HostAliasError("multiple IPv6 interfaces found; specify --interface and --prefix")
    print("Multiple usable IPv6 networks were found:")
    for number, item in enumerate(selected, 1):
        print(f"  {number}. {item.interface}  {item.prefix}  ({item.current_address})")
    while True:
        answer = input("Select network [1]: ").strip() or "1"
        if answer.isdigit() and 1 <= int(answer) <= len(selected):
            return selected[int(answer) - 1]
        print("Enter one of the listed numbers.")


def _current_windows_user() -> str:
    domain = os.environ.get("USERDOMAIN", "").strip()
    user = os.environ.get("USERNAME", "").strip() or getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def _ensure_service_running() -> None:
    from .windows_service import (
        NexusAgentAddressdService,
        _require_windows_modules,
        cleanup_service_runtimes,
        install_service_runtime,
    )

    pywintypes, _sm, _we, _wn, win32service, win32serviceutil, _wr = (
        _require_windows_modules()
    )
    class_name = (
        f"{NexusAgentAddressdService.__module__}."
        f"{NexusAgentAddressdService.__name__}"
    )
    service_executable = install_service_runtime()
    service_exists = True
    try:
        current_state = win32serviceutil.QueryServiceStatus(
            NexusAgentAddressdService._svc_name_
        )[1]
    except pywintypes.error as exc:
        if exc.winerror != 1060:  # ERROR_SERVICE_DOES_NOT_EXIST
            raise
        service_exists = False
        current_state = win32service.SERVICE_STOPPED
        win32serviceutil.InstallService(
            class_name,
            NexusAgentAddressdService._svc_name_,
            NexusAgentAddressdService._svc_display_name_,
            startType=win32service.SERVICE_AUTO_START,
            exeName=service_executable,
            description=NexusAgentAddressdService._svc_description_,
        )
    else:
        win32serviceutil.ChangeServiceConfig(
            class_name,
            NexusAgentAddressdService._svc_name_,
            startType=win32service.SERVICE_AUTO_START,
            exeName=service_executable,
            displayName=NexusAgentAddressdService._svc_display_name_,
            description=NexusAgentAddressdService._svc_description_,
        )
    if service_exists and current_state != win32service.SERVICE_STOPPED:
        try:
            win32serviceutil.StopService(NexusAgentAddressdService._svc_name_)
        except pywintypes.error as exc:
            if exc.winerror != 1062:  # ERROR_SERVICE_NOT_ACTIVE
                raise
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            current_state = win32serviceutil.QueryServiceStatus(
                NexusAgentAddressdService._svc_name_
            )[1]
            if current_state == win32service.SERVICE_STOPPED:
                break
            time.sleep(0.25)
        if current_state != win32service.SERVICE_STOPPED:
            raise HostAliasError("could not stop the existing addressd service")
    try:
        win32serviceutil.StartService(NexusAgentAddressdService._svc_name_)
    except pywintypes.error as exc:
        if exc.winerror != 1056:  # ERROR_SERVICE_INSTANCE_ALREADY_RUNNING
            raise
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        state = win32serviceutil.QueryServiceStatus(
            NexusAgentAddressdService._svc_name_
        )[1]
        if state == win32service.SERVICE_RUNNING:
            cleanup_service_runtimes(service_executable)
            return
        if state == win32service.SERVICE_STOPPED:
            break
        time.sleep(0.25)
    raise HostAliasError("NexusAgentAddressd did not reach the Running state")


def _wait_for_pipe(pipe_name: str, timeout: float = 10.0) -> Dict[str, Any]:
    transport = WindowsNamedPipeTransport(pipe_name, timeout=1.0)
    deadline = time.monotonic() + timeout
    last_error: Optional[BaseException] = None
    while time.monotonic() < deadline:
        try:
            return dict(transport.call("status", {"owner": "setup"}))
        except HostAliasError as exc:
            last_error = exc
            time.sleep(0.25)
    raise HostAliasError("addressd Named Pipe did not become ready") from last_error


@contextlib.contextmanager
def _temporary_firewall_block(port: int, backend: WindowsAddressBackend):
    rule_name = "Nexus temporary IPv6 setup self-test " + uuid.uuid4().hex
    backend._run([
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={rule_name}", "dir=in", "action=block", "protocol=TCP",
        f"localport={port}",
    ])
    try:
        yield
    finally:
        try:
            backend._run([
                "netsh", "advfirewall", "firewall", "delete", "rule",
                f"name={rule_name}",
            ])
        except HostAliasError:
            pass


def _self_test(candidate: IPv6Candidate, port: int) -> Dict[str, Any]:
    source = "agent://nexus-setup/local-doctor"
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
        "auto",
        auth=auth,
        tenant="nexus-setup",
        agent_id="ipv6-self-test",
        port=port,
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
            {"probe": "windows-host-alias"},
            tenant="nexus-setup",
            source_agent=source,
        )
        return {"address": agent.address, "port": port, "result": result}
    finally:
        handle.close()


def setup_windows(arguments: argparse.Namespace) -> int:
    _windows_only()
    if not _is_administrator():
        raise HostAliasError("setup did not receive an elevated Windows token")
    backend = WindowsAddressBackend(timeout=10.0)
    print("[1/6] Discovering usable global IPv6 networks...")
    candidates = discover_windows_candidates(backend)
    candidate = _select_candidate(
        candidates, interface=arguments.interface, prefix=arguments.prefix
    )
    print(f"      {candidate.interface}  {candidate.prefix}")

    print("[2/6] Selecting an available TCP port...")
    port = choose_port(arguments.port, _excluded_ports(backend))
    print(f"      {port}")

    print("[3/6] Configuring the local user group and addressd...")
    from .windows_service import DEFAULT_PIPE_NAME, _load_configuration, configure

    try:
        previous = _load_configuration()
    except Exception:
        previous = None
    if previous:
        try:
            previous_status = WindowsNamedPipeTransport(
                previous["pipe_name"], timeout=1.0
            ).call("status", {"owner": "setup"})
        except HostAliasError:
            previous_status = {}
        active_leases = int(previous_status.get("leases", 0))
        if active_leases:
            raise HostAliasError(
                f"addressd has {active_leases} active lease(s); stop those Agents before reconfiguration"
            )

    allowed_users = arguments.allow_user or [_current_windows_user()]
    configure([
        "--interface", candidate.interface,
        "--prefix", candidate.prefix,
        "--pipe", DEFAULT_PIPE_NAME,
        "--recommended-port", str(port),
        *sum((["--allow-user", user] for user in allowed_users), []),
    ])

    print("[4/6] Installing and starting NexusAgentAddressd...")
    _ensure_service_running()
    status = _wait_for_pipe(DEFAULT_PIPE_NAME)
    print(f"      Running, leases={status.get('leases', 0)}")

    print("[5/6] Running a protected /128 Agent self-test...")
    if arguments.skip_self_test:
        print("      Skipped by request")
        self_test = None
    else:
        with _temporary_firewall_block(port, backend):
            self_test = _self_test(candidate, port)
        print(f"      Passed on [{self_test['address']}]:{port}; address released")

    print("[6/6] Setup complete")
    print()
    print("Python Agent example:")
    print("from nexus_agent import NexusAgent")
    print("agent = NexusAgent.public_ipv6(")
    print(f"    'auto', auth='none', tenant='demo', agent_id='agent-1', port={port},")
    print(")")
    print()
    print("Sign out and back in once before running a non-elevated Agent.")
    print("No permanent public firewall rule was created.")
    print("Run 'nexus-agent ipv6 doctor' after signing in again.")
    return 0


def _service_state() -> str:
    try:
        from .windows_service import NexusAgentAddressdService, _require_windows_modules

        pywintypes, _sm, _we, _wn, win32service, win32serviceutil, _wr = (
            _require_windows_modules()
        )
        state = win32serviceutil.QueryServiceStatus(
            NexusAgentAddressdService._svc_name_
        )[1]
        names = {
            win32service.SERVICE_RUNNING: "Running",
            win32service.SERVICE_STOPPED: "Stopped",
            win32service.SERVICE_START_PENDING: "Starting",
            win32service.SERVICE_STOP_PENDING: "Stopping",
        }
        return names.get(state, f"State {state}")
    except Exception:
        return "Not installed or inaccessible"


def _current_token_in_group(group_name: str) -> bool:
    try:
        import win32security

        sid = win32security.LookupAccountName(None, group_name)[0]
        return bool(win32security.CheckTokenMembership(None, sid))
    except Exception:
        return False


def doctor_windows(arguments: argparse.Namespace) -> int:
    _windows_only()
    from .windows_service import _load_configuration

    checks = []
    configuration = None
    try:
        configuration = _load_configuration()
        checks.append(("Configuration", True, "Registry configuration loaded"))
    except Exception as exc:
        checks.append((
            "Configuration",
            False,
            "run 'nexus-agent ipv6 setup' to configure addressd",
        ))

    service_state = _service_state()
    checks.append(("Addressd service", service_state == "Running", service_state))

    if configuration:
        backend = WindowsAddressBackend(timeout=5.0)
        try:
            prefix = ipaddress.IPv6Network(configuration["prefix"])
            ready = backend.prefix_ready(configuration["interface"], prefix)
            checks.append((
                "Global prefix", ready,
                f"{configuration['interface']}  {configuration['prefix']}",
            ))
        except Exception as exc:
            checks.append(("Global prefix", False, str(exc)))
        try:
            pipe_status = WindowsNamedPipeTransport(
                configuration["pipe_name"], timeout=2.0
            ).call("status", {"owner": "doctor"})
            checks.append((
                "Named Pipe", True,
                f"ready; leases={pipe_status.get('leases', 0)}/{pipe_status.get('max_addresses', '?')}",
            ))
        except Exception as exc:
            checks.append(("Named Pipe", False, str(exc)))
        port = configuration.get("recommended_port", 9443)
        excluded = _excluded_ports(backend)
        port_ok = not any(start <= port <= end for start, end in excluded)
        checks.append((
            "Recommended port", port_ok,
            f"{port}" + ("" if port_ok else " is reserved by Windows/Hyper-V"),
        ))
        group = configuration.get("pipe_group", "Nexus Agent Users")
        in_group = _current_token_in_group(group)
        checks.append((
            "Current user", in_group,
            f"member of {group}" if in_group else f"sign out/in after joining {group}",
        ))

    if arguments.json:
        print(json.dumps([
            {"name": name, "ok": ok, "detail": detail}
            for name, ok, detail in checks
        ], ensure_ascii=False, indent=2))
    else:
        width = max(len(name) for name, _ok, _detail in checks)
        for name, ok, detail in checks:
            print(f"{name:<{width}}  {'OK' if ok else 'FAIL'}  {detail}")
    return 0 if all(ok for _name, ok, _detail in checks) else 1


def setup_platform(arguments: argparse.Namespace) -> int:
    if os.name == "nt":
        return setup_windows(arguments)
    if sys.platform.startswith("linux"):
        from .linux_ipv6 import setup_linux

        return setup_linux(arguments)
    raise HostAliasError("friendly IPv6 setup supports Linux and Windows")


def doctor_platform(arguments: argparse.Namespace) -> int:
    if os.name == "nt":
        return doctor_windows(arguments)
    if sys.platform.startswith("linux"):
        from .linux_ipv6 import doctor_linux

        return doctor_linux(arguments)
    raise HostAliasError("IPv6 diagnostics support Linux and Windows")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nexus-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    ipv6 = commands.add_parser("ipv6", help="manage per-Agent IPv6 /128 mode")
    ipv6_commands = ipv6.add_subparsers(dest="ipv6_command", required=True)

    setup = ipv6_commands.add_parser("setup", help="install and self-test addressd")
    setup.add_argument(
        "--mode",
        choices=("auto", "routed-prefix", "upstream-relay", "dhcpv6-ia-na"),
        default="auto",
        help="auto-select a routed prefix, upstream RA /64, or DHCPv6 IA_NA /128",
    )
    setup.add_argument("--interface")
    setup.add_argument("--prefix")
    setup.add_argument("--port", type=int)
    setup.add_argument("--allow-user", action="append", default=[])
    setup.add_argument("--skip-self-test", action="store_true")
    setup.set_defaults(handler=setup_platform)

    doctor = ipv6_commands.add_parser("doctor", help="diagnose addressd and IPv6")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=doctor_platform)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    arguments = parser.parse_args(raw_arguments)
    try:
        if (
            arguments.command == "ipv6"
            and arguments.ipv6_command == "setup"
            and not _is_administrator()
        ):
            if os.name == "nt":
                print("Opening Windows UAC for IPv6 setup...")
                return _run_elevated(raw_arguments)
            if sys.platform.startswith("linux"):
                from .linux_ipv6 import run_elevated_linux

                return run_elevated_linux(raw_arguments)
        return int(arguments.handler(arguments))
    except (HostAliasError, NexusAgentError, OSError, ValueError) as exc:
        print(f"nexus-agent: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
