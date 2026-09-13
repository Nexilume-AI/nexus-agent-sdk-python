"""Windows Service wrapper and administrator configuration for addressd."""

import argparse
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path, PurePath
import shutil
import subprocess
import sys
import uuid
import venv
from typing import Any, Dict, Iterable, Optional

from .addressd import AddressdApplication, AddressdStateStore, _secret
from .host_alias import HostAliasAllocator, HostAliasError, WindowsAddressBackend
from .windows_pipe import (
    AddressdNamedPipeServer,
    DEFAULT_PIPE_GROUP,
    DEFAULT_PIPE_NAME,
    validate_pipe_name,
)

_REGISTRY_PATH = r"SOFTWARE\Nexus\AgentAddressd"
_SERVICE_NAME = "NexusAgentAddressd"
_DEFAULT_ROOT = r"C:\ProgramData\Nexus"
_SERVICE_RUNTIME_ROOT = _DEFAULT_ROOT + r"\addressd-runtimes"


def _copy_distribution(distribution_name: str, destination: Path) -> str:
    """Copy one installed distribution without trusting paths outside site-packages."""

    try:
        distribution = importlib.metadata.distribution(distribution_name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise HostAliasError(
            f"the addressd service runtime requires {distribution_name}"
        ) from exc
    copied = 0
    for entry in distribution.files or ():
        relative = PurePath(str(entry))
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "__pycache__" in relative.parts
        ):
            continue
        source = Path(distribution.locate_file(entry))
        if not source.is_file():
            continue
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    if not copied:
        raise HostAliasError(
            f"could not stage {distribution_name} into the addressd service runtime"
        )
    return distribution.version


def _validate_service_runtime(python_executable: Path) -> None:
    environment = os.environ.copy()
    environment["PYTHONNOUSERSITE"] = "1"
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            str(python_executable),
            "-s",
            "-c",
            (
                "import nexus_agent.windows_service, servicemanager, "
                "win32serviceutil"
            ),
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30.0,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        reason = detail[-1] if detail else f"exit code {completed.returncode}"
        raise HostAliasError(
            f"isolated addressd service runtime validation failed: {reason}"
        )


def install_service_runtime(root: str = _SERVICE_RUNTIME_ROOT) -> str:
    """Build a machine-owned runtime that is independent of the installing user.

    The CLI itself may be installed with ``pip --user``.  A LocalSystem service
    must not import that user-writable copy, so setup stages the SDK and pywin32
    into a private venv and registers that venv's pythonservice executable.
    """

    _require_windows_modules()
    runtime_root = Path(root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    from .windows_pipe import protect_admin_tree

    protect_admin_tree(str(runtime_root))
    staging = runtime_root / ("staging-" + uuid.uuid4().hex)
    final = runtime_root / ("runtime-" + uuid.uuid4().hex)
    try:
        venv.EnvBuilder(with_pip=False, symlinks=False).create(staging)
        site_packages = staging / "Lib" / "site-packages"
        sdk_version = _copy_distribution("nexus-openwrt-agent-sdk", site_packages)
        pywin32_version = _copy_distribution("pywin32", site_packages)

        (
            _pw,
            _sm,
            _we,
            _wn,
            _ws,
            win32serviceutil,
            _wr,
        ) = _require_windows_modules()
        service_source = Path(win32serviceutil.LocatePythonServiceExe())
        service_executable = staging / "Scripts" / service_source.name
        shutil.copy2(service_source, service_executable)
        _validate_service_runtime(staging / "Scripts" / "python.exe")
        (staging / "runtime.json").write_text(
            json.dumps(
                {
                    "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                    "nexus_agent_sdk": sdk_version,
                    "pywin32": pywin32_version,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(staging, final)
        return str(final / "Scripts" / service_source.name)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def cleanup_service_runtimes(
    active_executable: str, root: str = _SERVICE_RUNTIME_ROOT
) -> None:
    """Remove superseded, exact runtime directories after the new service starts."""

    runtime_root = Path(root).resolve()
    active = Path(active_executable).resolve()
    active_runtime = active.parent.parent
    if active_runtime.parent != runtime_root:
        return
    for candidate in runtime_root.iterdir():
        if (
            candidate != active_runtime
            and candidate.is_dir()
            and (
                candidate.name.startswith("runtime-")
                or candidate.name.startswith("staging-")
            )
        ):
            shutil.rmtree(candidate, ignore_errors=True)


def _require_windows_modules():
    if os.name != "nt":
        raise HostAliasError("the addressd Windows Service is available only on Windows")
    try:
        import pywintypes
        import servicemanager
        import win32event
        import win32net
        import win32service
        import win32serviceutil
        import winreg
    except ImportError as exc:
        raise HostAliasError(
            "Windows Service support requires: pip install 'nexus-openwrt-agent-sdk[windows]'"
        ) from exc
    return (
        pywintypes,
        servicemanager,
        win32event,
        win32net,
        win32service,
        win32serviceutil,
        winreg,
    )


def _safe_group(value: str) -> str:
    text = str(value).strip()
    if not text or len(text) > 128 or any(
        character in "\r\n\0" or ord(character) < 0x20 for character in text
    ):
        raise ValueError("pipe group is invalid")
    return text


def _validate_network(interface: str, prefix: str):
    interface = str(interface).strip()
    if not interface or len(interface) > 128:
        raise ValueError("interface is invalid")
    try:
        WindowsAddressBackend().interface_index(interface)
    except HostAliasError as exc:
        raise ValueError(f"Windows interface was not found: {interface}") from exc
    try:
        network = ipaddress.IPv6Network(prefix, strict=True)
    except ValueError as exc:
        raise ValueError("prefix must be a canonical IPv6 /64") from exc
    if network.prefixlen != 64 or not network.is_global:
        raise ValueError("prefix must be a global IPv6 /64")
    return interface, str(network)


def _ensure_group(group: str, allowed_users: Iterable[str]) -> None:
    pywintypes, _sm, _we, win32net, _ws, _wsu, _wr = _require_windows_modules()
    try:
        win32net.NetLocalGroupGetInfo(None, group, 1)
    except pywintypes.error as exc:
        if exc.winerror != 2220:  # NERR_GroupNotFound
            raise
        win32net.NetLocalGroupAdd(
            None,
            1,
            {"name": group, "comment": "Users allowed to request Nexus Agent IPv6 addresses"},
        )
    for user in allowed_users:
        name = str(user).strip()
        if not name or any(character in "\r\n\0" for character in name):
            raise ValueError("allowed Windows user is invalid")
        try:
            win32net.NetLocalGroupAddMembers(
                None, group, 3, [{"domainandname": name}]
            )
        except pywintypes.error as exc:
            if exc.winerror != 1378:  # ERROR_MEMBER_IN_ALIAS
                raise


def configure(argv: Optional[Iterable[str]] = None) -> int:
    _pw, _sm, _we, _wn, _ws, _wsu, winreg = _require_windows_modules()
    parser = argparse.ArgumentParser(
        description="Configure the Nexus Agent IPv6 Windows Service"
    )
    parser.add_argument("--interface", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--pipe", default=DEFAULT_PIPE_NAME)
    parser.add_argument("--pipe-group", default=DEFAULT_PIPE_GROUP)
    parser.add_argument("--allow-user", action="append", default=[])
    parser.add_argument("--max-addresses", type=int, default=256)
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--reservation-seconds", type=int, default=15)
    parser.add_argument("--recommended-port", type=int, default=9443)
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    interface, prefix = _validate_network(arguments.interface, arguments.prefix)
    pipe_name = validate_pipe_name(arguments.pipe)
    group = _safe_group(arguments.pipe_group)
    # Reuse allocator validation for bounded numeric settings without touching a NIC.
    if not 1 <= arguments.max_addresses <= 65536:
        parser.error("--max-addresses must be between 1 and 65536")
    if not 30 <= arguments.lease_seconds <= 86400:
        parser.error("--lease-seconds must be between 30 and 86400")
    if not 5 <= arguments.reservation_seconds <= 300:
        parser.error("--reservation-seconds must be between 5 and 300")
    if not 1 <= arguments.recommended_port <= 65535:
        parser.error("--recommended-port must be between 1 and 65535")
    _ensure_group(group, arguments.allow_user)
    with winreg.CreateKeyEx(
        winreg.HKEY_LOCAL_MACHINE,
        _REGISTRY_PATH,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        values = {
            "Interface": interface,
            "Prefix": prefix,
            "PipeName": pipe_name,
            "PipeGroup": group,
            "SecretFile": _DEFAULT_ROOT + r"\addressd.secret",
            "StateFile": _DEFAULT_ROOT + r"\addressd-state.json",
        }
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        for name, value in {
            "MaxAddresses": arguments.max_addresses,
            "LeaseSeconds": arguments.lease_seconds,
            "ReservationSeconds": arguments.reservation_seconds,
            "RecommendedPort": arguments.recommended_port,
        }.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
    print(f"Configured {_SERVICE_NAME} for {interface} and {prefix}")
    if arguments.allow_user:
        print("Group membership takes effect after the user signs in again.")
    return 0


def _load_configuration() -> Dict[str, Any]:
    _pw, _sm, _we, _wn, _ws, _wsu, winreg = _require_windows_modules()
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, _REGISTRY_PATH, 0, winreg.KEY_READ
        )
    except OSError as exc:
        raise HostAliasError(
            "addressd is not configured; run nexus-agent-addressd-service configure"
        ) from exc
    with key:
        def read(name: str, default: Any = None):
            try:
                return winreg.QueryValueEx(key, name)[0]
            except FileNotFoundError:
                return default

        interface = read("Interface")
        prefix = read("Prefix")
        if not interface or not prefix:
            raise HostAliasError("addressd Windows Service configuration is incomplete")
        return {
            "interface": interface,
            "prefix": prefix,
            "pipe_name": read("PipeName", DEFAULT_PIPE_NAME),
            "pipe_group": read("PipeGroup", DEFAULT_PIPE_GROUP),
            "secret_file": read("SecretFile", _DEFAULT_ROOT + r"\addressd.secret"),
            "state_file": read("StateFile", _DEFAULT_ROOT + r"\addressd-state.json"),
            "max_addresses": int(read("MaxAddresses", 256)),
            "lease_seconds": int(read("LeaseSeconds", 300)),
            "reservation_seconds": int(read("ReservationSeconds", 15)),
            "recommended_port": int(read("RecommendedPort", 9443)),
        }


def _build_server(configuration: Dict[str, Any]) -> AddressdNamedPipeServer:
    backend = WindowsAddressBackend()
    allocator = HostAliasAllocator(
        backend=backend,
        interface=configuration["interface"],
        prefix=configuration["prefix"],
        allocation_secret=_secret(
            configuration["secret_file"], protect_windows=True
        ),
        max_addresses=configuration["max_addresses"],
        default_lease_seconds=configuration["lease_seconds"],
        reservation_seconds=configuration["reservation_seconds"],
    )
    application = AddressdApplication(
        allocator,
        state_store=AddressdStateStore(
            configuration["state_file"], protect_windows=True
        ),
    )
    return AddressdNamedPipeServer(
        configuration["pipe_name"],
        application,
        allowed_group=configuration["pipe_group"],
    )


if os.name == "nt":
    (
        _pywintypes,
        servicemanager,
        _win32event,
        _win32net,
        win32service,
        win32serviceutil,
        _winreg,
    ) = _require_windows_modules()

    class NexusAgentAddressdService(win32serviceutil.ServiceFramework):
        _svc_name_ = _SERVICE_NAME
        _svc_display_name_ = "Nexus Agent IPv6 Address Allocator"
        _svc_description_ = "Allocates one bounded global IPv6 /128 per local Nexus Agent."

        def __init__(self, args):
            super().__init__(args)
            self.server: Optional[AddressdNamedPipeServer] = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self.server is not None:
                self.server.shutdown()

        def SvcDoRun(self):
            try:
                self.server = _build_server(_load_configuration())
                self.server.serve_forever()
            except BaseException as exc:
                servicemanager.LogErrorMsg(f"{_SERVICE_NAME}: {exc}")
                raise
            finally:
                if self.server is not None:
                    self.server.server_close()


def main(argv: Optional[Iterable[str]] = None) -> int:
    _require_windows_modules()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "configure":
        return configure(arguments[1:])
    if os.name != "nt":
        raise HostAliasError("the addressd Windows Service is available only on Windows")
    installs_runtime = any(
        argument in {"install", "update"} for argument in arguments
    )
    if installs_runtime:
        NexusAgentAddressdService._exe_name_ = install_service_runtime()
    try:
        return int(win32serviceutil.HandleCommandLine(
            NexusAgentAddressdService,
            argv=[sys.argv[0], *arguments],
        ) or 0)
    finally:
        if installs_runtime and hasattr(NexusAgentAddressdService, "_exe_name_"):
            del NexusAgentAddressdService._exe_name_


if __name__ == "__main__":
    raise SystemExit(main())
