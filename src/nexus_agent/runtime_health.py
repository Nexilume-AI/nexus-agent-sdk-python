"""Bounded MCP dependency diagnosis and opt-in, pre-execution repair.

Never install packages during SDK import or replay an Agent after execution.
Repairs target the current venv/user installation, never the system Python.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Dict, Iterable, Optional


_PROBE = r'''
import importlib.metadata as metadata
import json, sys
result = {"python": list(sys.version_info[:2]), "versions": {}, "import_ok": False,
          "strenum_error": False}
for name in ("nexilume", "fastmcp", "mcp", "griffelib"):
    try:
        result["versions"][name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        pass
try:
    from fastmcp import FastMCP, Client
    from fastmcp.server.http import create_streamable_http_app
    import uvicorn
    result["import_ok"] = True
except Exception as error:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, ImportError) and "StrEnum" in str(error):
            result["strenum_error"] = True
        error = error.__cause__ or error.__context__
print(json.dumps(result))
'''

_MCP_REQUIREMENTS = [
    "fastmcp>=3.4.7,<4", "mcp>=1.30,<2", "uvicorn>=0.35,<1",
    "griffelib!=2.3.1; python_version < '3.11'",
]


def _version(value: str):
    parts = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", value)
    return tuple(int(part or 0) for part in parts.groups()) if parts else ()


def _failure(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "code": code, "message": message,
            "repair_requirements": [], **extra}


def classify(data: Dict[str, Any]) -> Dict[str, Any]:
    versions = data.get("versions", {})
    details = {"python": data.get("python", []), "versions": versions}
    if tuple(data.get("python", ())) < (3, 10):
        return _failure("MCP_PYTHON_UNSUPPORTED", "MCP requires Python 3.10 or newer.", **details)
    if (tuple(data["python"]) < (3, 11) and versions.get("griffelib") == "2.3.1"
            and data.get("strenum_error")):
        return _failure("MCP_DEPENDENCY_INCOMPATIBLE",
                        "griffelib 2.3.1 imports StrEnum, unavailable on Python 3.10. "
                        "Repair selects the verified 2.3.0 dependency; server support is not missing.",
                        repair_requirements=["griffelib==2.3.0"], **details)
    if not versions.get("fastmcp") or not versions.get("mcp"):
        return _failure("MCP_DEPENDENCIES_MISSING", "MCP dependencies are not installed.",
                        repair_requirements=list(_MCP_REQUIREMENTS), **details)
    if not ((3, 4, 7) <= _version(versions["fastmcp"]) < (4, 0, 0)
            and (1, 30, 0) <= _version(versions["mcp"]) < (2, 0, 0)):
        return _failure("MCP_VERSION_UNSUPPORTED",
                        "Use FastMCP >=3.4.7,<4 and MCP >=1.30,<2. "
                        "Review this environment before changing incompatible versions.", **details)
    if not data.get("import_ok"):
        return _failure("MCP_IMPORT_FAILED", "MCP import failed for an unrecognized reason. "
                        "Inspect the Python exception; automatic package replacement is disabled.", **details)
    return {"ok": True, "code": "READY", "message": "MCP dependencies are ready.",
            "repair_requirements": [], **details}


def diagnose() -> Dict[str, Any]:
    try:
        result = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True,
                                text=True, timeout=20)
        if result.returncode != 0:
            raise ValueError("probe failed")
        data = json.loads(result.stdout.strip().splitlines()[-1])
        if not isinstance(data, dict) or not isinstance(data.get("versions"), dict):
            raise ValueError("invalid probe result")
        return classify(data)
    except (OSError, ValueError, IndexError, TypeError, KeyError, subprocess.TimeoutExpired):
        return _failure("SDK_DIAGNOSTIC_FAILED", "The isolated MCP import check failed or timed out. "
                        "No dependencies or services were changed.")


def _install_scope():
    if sys.prefix != sys.base_prefix:
        return []
    if os.name != "nt" and os.geteuid() == 0:
        return None
    return ["--user"]


def _lock_root() -> Path:
    key = hashlib.sha256((sys.executable + sys.prefix).encode()).hexdigest()[:20]
    return Path.home() / ".cache" / "nexilume" / "sdk-repair" / key


def repair(*, confirmed: bool) -> Dict[str, Any]:
    report = diagnose()
    if report["ok"] or not report["repair_requirements"]:
        return report
    if not confirmed:
        return _failure("SDK_REPAIR_CONFIRMATION_REQUIRED",
                        "Run nexus-agent repair --yes or launch with nexus-agent run --repair. "
                        "Only listed dependencies in this venv/user installation will change.",
                        repair_requirements=report["repair_requirements"])
    scope = _install_scope()
    if scope is None:
        return _failure("SDK_REPAIR_SYSTEM_PYTHON_REFUSED", "Activate a virtual environment. "
                        "Repair will not modify system Python as root.")
    lock = _lock_root() / "repair.lock"
    acquired = False
    try:
        lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if lock.parent.is_symlink():
            raise OSError("invalid repair directory")
        try:
            lock.mkdir(mode=0o700)
            acquired = True
        except FileExistsError:
            return _failure("SDK_REPAIR_BUSY", "Another repair owns this environment lock. "
                            "Wait for it to finish; interrupted locks require operator review.")
        # Recheck after acquiring the lock: a previous repair may have finished.
        report = diagnose()
        if report["ok"] or not report["repair_requirements"]:
            return report
        result = subprocess.run([
            sys.executable, "-m", "pip", "--isolated", "install", "--disable-pip-version-check",
            "--quiet", "--only-binary=:all:", "--retries", "1", "--timeout", "15",
            "--index-url", "https://pypi.org/simple",
            *scope, *report["repair_requirements"],
        ], capture_output=True, text=True, timeout=180)
        if result.returncode:
            return _failure("SDK_REPAIR_FAILED", "Dependency installation failed. "
                            "Check network, certificate trust and package permissions, then run doctor. "
                            "No Agent was started; pip may have partially updated dependencies.")
        verified = diagnose()
        if not verified["ok"]:
            return _failure("SDK_REPAIR_VERIFICATION_FAILED", "Dependencies changed but the fresh "
                            "MCP import check is not healthy. No Agent was started.",
                            diagnostic=verified)
        return {**verified, "repaired": True, "restart_required": True}
    except (OSError, subprocess.TimeoutExpired):
        return _failure("SDK_REPAIR_FAILED", "Repair could not finish. Check permissions/network "
                        "and run doctor before retrying. No Agent was started.")
    finally:
        if acquired:
            try:
                lock.rmdir()
            except OSError:
                pass  # Preserve the diagnostic; never remove another path recursively.


def _display(report: Dict[str, Any], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print(f"{report['code']}: {report.get('message', '')}")
        if report.get("repair_requirements"):
            print("Repair dependencies: " + ", ".join(report["repair_requirements"]))


def main(argv: Optional[Iterable[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "ipv6":
        from .ipv6_cli import main as ipv6_main
        return ipv6_main(arguments)
    parser = argparse.ArgumentParser(prog="nexus-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="check MCP imports in a fresh process")
    doctor.add_argument("--json", action="store_true")
    fix = commands.add_parser("repair", help="repair recognized MCP dependency failures")
    fix.add_argument("--yes", action="store_true", help="allow bounded venv/user dependency repair")
    fix.add_argument("--json", action="store_true")
    run = commands.add_parser("run", help="check dependencies, then execute a Python Agent once")
    run.add_argument("--repair", action="store_true", help="automatically repair recognized dependency failures")
    run.add_argument("script")
    run.add_argument("args", nargs=argparse.REMAINDER)
    commands.add_parser("ipv6", help="IPv6 setup/doctor (nexus-agent ipv6 --help)")
    args = parser.parse_args(arguments)
    if args.command == "run":
        script = Path(args.script).resolve()
        if not script.is_file() or script.suffix.lower() != ".py":
            parser.error("script must be an existing Python (.py) file")
        report = diagnose()
        if not report["ok"] and args.repair:
            report = repair(confirmed=True)
        if not report["ok"]:
            _display(report)
            return 2
        # No retry after application execution: external effects may already exist.
        return subprocess.call([sys.executable, str(script), *args.args])
    report = repair(confirmed=args.yes) if args.command == "repair" else diagnose()
    _display(report, as_json=args.json)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
