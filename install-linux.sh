#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Install Nexus Agent SDK without modifying the distribution-managed Python.

Usage:
  ./install-linux.sh [--wheel PATH] [--prefix PATH] [--install-only]

Options:
  --wheel PATH    SDK wheel to install (defaults to dist/nexus_openwrt_agent_sdk-*.whl)
  --prefix PATH   User install prefix (defaults to ~/.local)
  --install-only  Install the CLI but do not run IPv6 addressd setup
  -h, --help      Show this help

Run this script as your normal login user. The installed CLI requests sudo only
for the small systemd/network setup phase.
EOF
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
prefix=${HOME}/.local
wheel=
run_setup=1

while [ "$#" -gt 0 ]; do
    case "$1" in
        --wheel)
            [ "$#" -ge 2 ] || { echo "--wheel requires a path" >&2; exit 2; }
            wheel=$2
            shift 2
            ;;
        --prefix)
            [ "$#" -ge 2 ] || { echo "--prefix requires a path" >&2; exit 2; }
            prefix=$2
            shift 2
            ;;
        --install-only)
            run_setup=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    echo "Run install-linux.sh as your normal login user, without sudo." >&2
    echo "The nexus-agent CLI will request sudo only when system setup is required." >&2
    exit 2
fi

command -v python3 >/dev/null 2>&1 || {
    echo "python3 is required (Python 3.9 or newer)." >&2
    exit 1
}

if [ -z "$wheel" ]; then
    wheel=$(python3 - "$script_dir/dist" <<'PY'
import re
from pathlib import Path
import sys

dist = Path(sys.argv[1])
candidates = []
for path in dist.glob("nexus_openwrt_agent_sdk-*.whl"):
    match = re.fullmatch(r"nexus_openwrt_agent_sdk-(\d+)\.(\d+)\.(\d+)-.+\.whl", path.name)
    if match:
        candidates.append((tuple(map(int, match.groups())), path))
if not candidates:
    raise SystemExit(1)
print(max(candidates, key=lambda item: item[0])[1])
PY
    ) || {
        echo "No versioned SDK wheel was found in $script_dir/dist." >&2
        echo "Pass it explicitly with --wheel PATH." >&2
        exit 1
    }
fi

[ -f "$wheel" ] || { echo "SDK wheel not found: $wheel" >&2; exit 1; }

install_root=$prefix/share/nexus-agent
bin_dir=$prefix/bin

python3 - "$wheel" "$install_root" "$bin_dir" <<'PY'
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import zipfile

wheel = Path(sys.argv[1]).expanduser().resolve()
install_root = Path(sys.argv[2]).expanduser().resolve()
bin_dir = Path(sys.argv[3]).expanduser().resolve()

if sys.version_info < (3, 9):
    raise SystemExit(
        f"Python 3.9 or newer is required; found {sys.version.split()[0]}"
    )

install_root.mkdir(parents=True, exist_ok=True)
bin_dir.mkdir(parents=True, exist_ok=True)
stage = Path(tempfile.mkdtemp(prefix="runtime-", dir=install_root))
runtime = install_root / "runtime"

try:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if "nexus_agent/__init__.py" not in names:
            raise SystemExit(f"not a Nexus Agent SDK wheel: {wheel}")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise SystemExit(f"unsafe wheel member: {name}")
        archive.extractall(stage)

    sys.path.insert(0, str(stage))
    import nexus_agent  # noqa: E402

    version = nexus_agent.__version__
    (stage / ".nexus-version").write_text(version + "\n", encoding="utf-8")
    if runtime.exists():
        shutil.rmtree(runtime)
    os.replace(stage, runtime)
except BaseException:
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    raise

entries = {
    "nexus-agent": "nexus_agent.ipv6_cli:main",
    "nexus-agent-security": "nexus_agent.security_profile:main",
    "nexus-agent-addressd": "nexus_agent.addressd:main",
}
for command, entry in entries.items():
    module, function = entry.split(":", 1)
    wrapper = bin_dir / command
    content = f"""#!/usr/bin/env python3
import sys
sys.path.insert(0, {str(runtime)!r})
from {module} import {function}
raise SystemExit({function}())
"""
    temporary = wrapper.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.chmod(0o755)
    os.replace(temporary, wrapper)

user_site = (
    bin_dir.parent
    / "lib"
    / f"python{sys.version_info.major}.{sys.version_info.minor}"
    / "site-packages"
)
user_site.mkdir(parents=True, exist_ok=True)
pth = user_site / "nexus_agent_sdk.pth"
temporary_pth = pth.with_suffix(".tmp")
temporary_pth.write_text(str(runtime) + "\n", encoding="utf-8", newline="\n")
os.replace(temporary_pth, pth)

print(f"Installed Nexus Agent SDK {version} in {runtime}")
print(f"Python import path: {pth}")
PY

"$bin_dir/nexus-agent" ipv6 --help >/dev/null
echo "CLI installed: $bin_dir/nexus-agent"

case ":${PATH}:" in
    *":$bin_dir:"*) ;;
    *)
        echo "Add this directory to PATH if it is not already present:"
        echo "  $bin_dir"
        ;;
esac

if [ "$run_setup" -eq 1 ]; then
    echo "Starting Linux IPv6 address service setup..."
    exec "$bin_dir/nexus-agent" ipv6 setup
fi

echo "Installation complete. Run:"
echo "  $bin_dir/nexus-agent ipv6 setup"
