"""Caller-owned Nexus Computer Runtime with an outbound Cloud WebSocket."""

from __future__ import annotations

import argparse
import asyncio
import base64
import codecs
from collections import deque
from datetime import datetime, timezone
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform as platform_module
import queue
import select
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Mapping, Optional, Sequence, Union
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
import uuid


PROTOCOL_VERSION = 1
DEFAULT_ROOT = Path.home() / ".nexus" / "computer"
CONFIG_NAME = "config.json"
REGISTRY_NAME = "registrations.json"
REGISTRY_BACKUP_NAME = "registrations.backup.json"
REGISTRATIONS_DIRECTORY_NAME = "registrations"
RUNTIME_STATE_NAME = "runtime-state.json"
REGISTRY_LOCK_NAME = "registrations.lock"
KEY_NAME = "device.key"
CLOUD_CA_NAME = "cloud-ca.pem"
JOURNAL_NAME = "journal.json"
LOG_NAME = "runtime.log"
LOCK_NAME = "runtime.lock"
PID_NAME = "runtime.pid"
RESTART_REQUEST_NAME = "restart.request"
WINDOWS_TASK_NAME = "Nexus Computer Runtime"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_RUN_VALUE = "Nexus Computer Runtime"
MAX_TEXT_RESULT = 4 * 1024 * 1024
MAX_PAIRING_CA_BYTES = 64 * 1024
MAX_TERMINAL_STREAM_FRAME_BYTES = 64 * 1024


class NexusComputerRuntimeError(RuntimeError):
    pass


class RuntimeOperationError(NexusComputerRuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code or "COMPUTER_RUNTIME_COMMAND_FAILED")


class _RuntimeInstanceLock:
    """Cross-platform advisory lock held for the lifetime of one Runtime."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            raise NexusComputerRuntimeError(
                "Nexus Computer Runtime is already running for this pairing directory"
            ) from None
        self.handle = handle

    def release(self) -> None:
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _wait_for_instance_lock_release(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + max(float(timeout), 0.1)
    while time.monotonic() < deadline:
        probe = _RuntimeInstanceLock(path)
        try:
            probe.acquire()
        except NexusComputerRuntimeError:
            time.sleep(0.1)
            continue
        probe.release()
        return
    raise NexusComputerRuntimeError("The previous Nexus Computer Runtime did not stop in time")


def _require_crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise NexusComputerRuntimeError(
            "Computer Runtime requires: pip install 'nexilume[computer]'"
        ) from exc
    return serialization, Ed25519PrivateKey


def _require_websockets():
    try:
        import websockets
    except ImportError as exc:
        raise NexusComputerRuntimeError(
            "Computer Runtime requires: pip install 'nexilume[computer]'"
        ) from exc
    return websockets


def _platform_name() -> str:
    value = sys.platform.lower()
    if value == "win32":
        return "windows"
    if value == "darwin":
        return "macos"
    if value.startswith("linux"):
        return "linux"
    raise NexusComputerRuntimeError(f"Computer Runtime does not support {sys.platform!r}")


def _protect_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        try:
            identity = subprocess.run(
                ["whoami"], capture_output=True, text=True, timeout=5, check=True
            ).stdout.strip()
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{identity}:(OI)(CI)F"],
                capture_output=True,
                timeout=15,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise NexusComputerRuntimeError("Could not protect the Computer Runtime directory with a user ACL") from exc
    else:
        os.chmod(path, 0o700)


def _protect_file(path: Path) -> None:
    if os.name == "nt":
        try:
            identity = subprocess.run(
                ["whoami"], capture_output=True, text=True, timeout=5, check=True
            ).stdout.strip()
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{identity}:F"],
                capture_output=True,
                timeout=15,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise NexusComputerRuntimeError("Could not protect a Computer Runtime credential file") from exc
    else:
        os.chmod(path, 0o600)


def _atomic_write(path: Path, data: bytes) -> None:
    _protect_directory(path.parent)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _protect_file(temporary)
        os.replace(temporary, path)
        _protect_file(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_user_file_write(path: Path, data: bytes) -> None:
    """Replace a user-owned config without changing the parent directory ACL."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _ssl_context(ca_file: str = "", *, ca_pem: bytes = b"") -> ssl.SSLContext:
    context = ssl.create_default_context()
    if ca_file:
        path = Path(ca_file).expanduser().resolve()
        if not path.is_file():
            raise NexusComputerRuntimeError("Computer Runtime CA file does not exist")
        context.load_verify_locations(cafile=str(path))
    elif ca_pem:
        try:
            context.load_verify_locations(cadata=ca_pem.decode("ascii"))
        except (UnicodeDecodeError, ssl.SSLError) as exc:
            raise NexusComputerRuntimeError("Pairing URL contains an invalid Cloud CA bundle") from exc
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _pairing_cloud_trust(query: Mapping[str, Sequence[str]]) -> bytes:
    mode = str((query.get("trust") or [""])[0]).strip()
    if not mode:
        return b""
    if mode != "pinned-pem":
        raise NexusComputerRuntimeError("Pairing URL uses an unsupported Cloud trust mode")
    encoded = str((query.get("ca") or [""])[0]).strip()
    expected_digest = str((query.get("ca_sha256") or [""])[0]).strip().lower()
    if not encoded or len(expected_digest) != 64:
        raise NexusComputerRuntimeError("Pairing URL Cloud trust is incomplete")
    try:
        value = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise NexusComputerRuntimeError("Pairing URL Cloud CA bundle is invalid") from exc
    if not value or len(value) > MAX_PAIRING_CA_BYTES or b"PRIVATE KEY" in value:
        raise NexusComputerRuntimeError("Pairing URL Cloud CA bundle is unsafe")
    actual_digest = hashlib.sha256(value).hexdigest()
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise NexusComputerRuntimeError("Pairing URL Cloud CA digest does not match")
    try:
        from cryptography import x509

        certificates = x509.load_pem_x509_certificates(value)
    except (ImportError, ValueError) as exc:
        raise NexusComputerRuntimeError("Pairing URL Cloud CA bundle is not valid PEM") from exc
    if not certificates:
        raise NexusComputerRuntimeError("Pairing URL Cloud CA bundle contains no certificates")
    serialization, _private_key = _require_crypto()
    return b"".join(
        certificate.public_bytes(serialization.Encoding.PEM) for certificate in certificates
    )


def _validated_cloud_origin(value: str) -> str:
    parsed = urlsplit(str(value).strip().rstrip("/"))
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise NexusComputerRuntimeError("Computer Runtime pairing requires HTTPS outside loopback development")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise NexusComputerRuntimeError("Computer Runtime Cloud origin is invalid")
    return f"{parsed.scheme}://{parsed.netloc}"


COMPUTER_USER_AGENT = "Nexus-Computer/0.49.0"


def _request_json(
    cloud_origin: str,
    path: str,
    *,
    payload: Mapping[str, Any],
    ca_file: str = "",
    ca_pem: bytes = b"",
    timeout: float = 20.0,
) -> dict[str, Any]:
    body = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        urljoin(cloud_origin.rstrip("/") + "/", path.lstrip("/")),
        data=body,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8", "User-Agent": COMPUTER_USER_AGENT},
    )
    handlers = [ProxyHandler({})]
    if cloud_origin.startswith("https://"):
        handlers.append(HTTPSHandler(context=_ssl_context(ca_file, ca_pem=ca_pem)))
    try:
        with build_opener(*handlers).open(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000]
        try:
            parsed = json.loads(detail)
            message = parsed.get("error", {}).get("message") or parsed.get("message") or detail
        except (ValueError, TypeError):
            message = detail
        raise NexusComputerRuntimeError(f"Nexus Cloud rejected Computer Runtime request ({exc.code}): {message}") from None
    except (URLError, OSError) as exc:
        raise NexusComputerRuntimeError(f"Nexus Cloud Computer Runtime request failed: {exc}") from None
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        return value["data"]
    if not isinstance(value, dict):
        raise NexusComputerRuntimeError("Nexus Cloud returned an invalid Computer Runtime response")
    return value


class _ComputerAssetNoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise NexusComputerRuntimeError("Computer Runtime asset transfers cannot redirect")


def _upload_bytes(
    cloud_origin: str,
    endpoint: str,
    *,
    token: str,
    content: bytes,
    content_type: str,
    ca_file: str = "",
) -> dict[str, Any]:
    origin = _validated_cloud_origin(cloud_origin)
    parsed_endpoint = urlsplit(str(endpoint or ""))
    if parsed_endpoint.scheme or parsed_endpoint.netloc or not str(endpoint).startswith("/api/v1/computer-runtime/"):
        raise NexusComputerRuntimeError("Computer Runtime upload endpoint is invalid")
    request = Request(
        urljoin(origin + "/", str(endpoint).lstrip("/")),
        data=bytes(content),
        method="PUT",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": COMPUTER_USER_AGENT,
            "Content-Type": str(content_type or "application/octet-stream"),
        },
    )
    handlers = [ProxyHandler({}), _ComputerAssetNoRedirect()]
    if origin.startswith("https://"):
        handlers.append(HTTPSHandler(context=_ssl_context(ca_file)))
    try:
        with build_opener(*handlers).open(request, timeout=60) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise NexusComputerRuntimeError("Nexus Cloud Computer Runtime upload failed") from exc
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        return value["data"]
    if not isinstance(value, dict):
        raise NexusComputerRuntimeError("Nexus Cloud returned an invalid Computer Runtime upload response")
    return value


def _download_bytes(cloud_origin: str, endpoint: str, *, token: str, ca_file: str = "") -> bytes:
    from .workspace_files import FILE_CHUNK_BYTES

    origin = _validated_cloud_origin(cloud_origin)
    parsed = urlsplit(str(endpoint or ""))
    if parsed.scheme or parsed.netloc or not str(endpoint).startswith("/api/v1/computer-runtime/v1/commands/"):
        raise NexusComputerRuntimeError("Computer Runtime download endpoint is invalid")
    handlers = [ProxyHandler({}), _ComputerAssetNoRedirect()]
    if origin.startswith("https://"):
        handlers.append(HTTPSHandler(context=_ssl_context(ca_file)))
    request = Request(urljoin(origin + "/", endpoint.lstrip("/")), headers={"Authorization": f"Bearer {token}"})
    try:
        with build_opener(*handlers).open(request, timeout=35) as response:
            content = response.read(FILE_CHUNK_BYTES + 1)
        if len(content) > FILE_CHUNK_BYTES:
            raise NexusComputerRuntimeError("Computer Runtime download exceeded the chunk limit")
        return content
    except (HTTPError, URLError, OSError) as exc:
        raise NexusComputerRuntimeError("Nexus Cloud Computer Runtime download failed") from exc


class _TerminalProcess:
    def __init__(self, *, shell: str, cwd: Path, cols: int = 120, rows: int = 34) -> None:
        self._master_fd: Optional[int] = None
        self._pty_lock = threading.Lock()
        self._reader_stop = threading.Event()
        if os.name == "nt":
            if shell in {"auto", "powershell"}:
                # The browser and WSS protocol always send UTF-8. Windows PowerShell
                # otherwise reads redirected stdin using the active legacy code page
                # (for example CP936), corrupting non-ASCII input before execution.
                utf8_init = (
                    "$utf8 = [System.Text.UTF8Encoding]::new($false); "
                    "[Console]::InputEncoding = $utf8; "
                    "[Console]::OutputEncoding = $utf8; "
                    "$OutputEncoding = $utf8"
                )
                command = [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NoExit",
                    "-Command",
                    utf8_init,
                ]
            else:
                command = ["cmd.exe", "/Q", "/K", "chcp 65001>nul"]
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            start_new_session = False
        else:
            if shell == "auto" and platform_module.system() == "Darwin":
                executable = shutil.which("zsh") or "/bin/zsh"
            else:
                executable = shutil.which("bash" if shell in {"auto", "bash"} else "sh") or "/bin/sh"
            command = [sys.executable, os.path.join(os.path.dirname(__file__), "_terminal_child.py"), executable]
            flags = 0
            start_new_session = True
        if os.name == "nt":
            self.process = subprocess.Popen(
                command, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0, creationflags=flags,
                start_new_session=start_new_session,
            )
        else:
            self._start_pty(command, cwd=cwd, cols=cols, rows=rows)
        self.output: "queue.Queue[bytes]" = queue.Queue()
        # Keep decoder state across reads so a UTF-8 code point split between two
        # pipe/WebSocket frames is not replaced by U+FFFD.
        self._output_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.reader = threading.Thread(target=self._read, name="nexus-computer-terminal", daemon=True)
        self.reader.start()

    def _start_pty(self, command: list[str], *, cwd: Path, cols: int, rows: int) -> None:
        import pty

        master, slave = pty.openpty()
        self._master_fd = master
        try:
            self.resize(cols=cols, rows=rows)
            os.set_blocking(master, False)
            environment = os.environ.copy()
            environment["TERM"] = "xterm-256color"
            self.process = subprocess.Popen(
                command, cwd=str(cwd), stdin=slave, stdout=slave, stderr=slave,
                env=environment, start_new_session=True, close_fds=True,
            )
        except BaseException:
            os.close(master)
            self._master_fd = None
            raise
        finally:
            os.close(slave)

    def resize(self, *, cols: int, rows: int) -> None:
        if os.name == "nt":
            return
        import fcntl
        import struct
        import termios

        size = struct.pack("HHHH", max(1, min(int(rows), 1000)), max(1, min(int(cols), 1000)), 0, 0)
        with self._pty_lock:
            if self._master_fd is not None:
                fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, size)

    def _read(self) -> None:
        if self._master_fd is not None:
            self._read_pty()
            return
        stream = self.process.stdout
        if stream is None:
            return
        while True:
            data = stream.read(4096)
            if not data:
                return
            self.output.put(data)

    def _read_pty(self) -> None:
        try:
            while not self._reader_stop.is_set():
                # This thread owns closing master; writers/resizers share the lock.
                master = self._master_fd
                if not select.select([master], [], [], 0.1)[0]:
                    continue
                try:
                    data = os.read(master, 4096)
                except BlockingIOError:
                    continue
                except OSError:
                    # Linux reports EIO when the last slave is closed; macOS EOF.
                    break
                if not data:
                    break
                self.output.put(data)
        finally:
            with self._pty_lock:
                if self._master_fd is not None:
                    os.close(self._master_fd)
                    self._master_fd = None

    def read(self, timeout: float) -> str:
        chunks: list[bytes] = []
        try:
            chunks.append(self.output.get(timeout=max(0.01, timeout)))
        except queue.Empty:
            return ""
        total = len(chunks[0])
        while total < 65536:
            try:
                item = self.output.get_nowait()
            except queue.Empty:
                break
            chunks.append(item)
            total += len(item)
        return self._output_decoder.decode(b"".join(chunks), final=False)

    def write(self, data: str) -> None:
        if self.process.poll() is not None:
            raise RuntimeOperationError("TERMINAL_SESSION_LOST", "Computer terminal session is no longer running")
        if os.name != "nt":
            pending = memoryview(str(data).encode("utf-8"))
            deadline = time.monotonic() + 5
            with self._pty_lock:
                master = self._master_fd
                if master is None:
                    raise RuntimeOperationError("TERMINAL_SESSION_LOST", "Computer terminal session is no longer running")
                while pending:
                    if time.monotonic() >= deadline:
                        raise RuntimeOperationError("TERMINAL_INPUT_TIMEOUT", "Computer terminal is not accepting input")
                    if not select.select([], [master], [], 0.1)[1]:
                        continue
                    try:
                        pending = pending[os.write(master, pending):]
                    except BlockingIOError:
                        continue
            return
        if self.process.stdin is None:
            raise RuntimeOperationError("TERMINAL_SESSION_LOST", "Computer terminal session is no longer running")
        self.process.stdin.write(str(data).encode("utf-8"))
        self.process.stdin.flush()

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                if os.name == "nt":
                    self.process.terminate()
                else:
                    with self._pty_lock:
                        if self._master_fd is not None:
                            try:
                                foreground = os.tcgetpgrp(self._master_fd)
                                if foreground > 0 and foreground != self.process.pid:
                                    os.killpg(foreground, signal.SIGHUP)
                            except OSError:
                                pass
                    os.killpg(self.process.pid, signal.SIGHUP)
                self.process.wait(timeout=5)
        except Exception:
            try:
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:
                pass
        finally:
            self._reader_stop.set()
            self.reader.join(timeout=1)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass


class NexusComputerRuntime:
    """Cross-platform Runtime that executes bounded commands from an outbound WSS."""

    def __init__(self, root: Union[Path, str] = DEFAULT_ROOT) -> None:
        self.root = Path(root).expanduser().resolve()
        self.config_path = self.root / CONFIG_NAME
        self.registry_path = self.root / REGISTRY_NAME
        self.registry_backup_path = self.root / REGISTRY_BACKUP_NAME
        self.key_path = self.root / KEY_NAME
        self.journal_path = self.root / JOURNAL_NAME
        self.state_path = self.root / RUNTIME_STATE_NAME
        self.log_path = self.root / LOG_NAME
        self.pid_path = self.root / PID_NAME
        self.restart_request_path = self.root / RESTART_REQUEST_NAME
        self.config = self._load_json(self.config_path)
        self._stop = asyncio.Event()
        self._send_sequence = 0
        self._send_lock: Optional[asyncio.Lock] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._terminals: Dict[str, _TerminalProcess] = {}
        self._terminal_stream_tasks: Dict[str, asyncio.Task] = {}
        self._browsers: Dict[str, Any] = {}
        from .computer_files import ComputerFileTransfers
        self._file_transfers = ComputerFileTransfers(self)
        self._memory_results: Dict[str, tuple[bool, dict[str, Any]]] = self._load_journal_results()
        self._inflight: Dict[str, asyncio.Task] = {}
        self._cancel_events: Dict[str, threading.Event] = {}
        legacy_completed = self._load_json(self.journal_path).get("completed", [])
        self._completed_ids = deque(
            [*legacy_completed, *self._memory_results.keys()],
            maxlen=512,
        )

    def _registration_entries(self) -> list[dict[str, str]]:
        registry = self._load_json(self.registry_path)
        registry_valid = int(registry.get("schema_version") or 0) == 2 and isinstance(
            registry.get("registrations"), list
        )
        if not registry_valid:
            registry = self._load_json(self.registry_backup_path)
            registry_valid = int(registry.get("schema_version") or 0) == 2 and isinstance(
                registry.get("registrations"), list
            )
        raw_entries = registry.get("registrations") if isinstance(registry, dict) else []
        entries: list[dict[str, str]] = []
        seen: set[str] = set()
        for value in raw_entries if isinstance(raw_entries, list) else []:
            if not isinstance(value, dict):
                continue
            registration_id = str(value.get("registration_id") or value.get("device_id") or "").strip()
            relative_path = str(value.get("path") or "").strip()
            if not registration_id or not relative_path or registration_id in seen:
                continue
            candidate = (self.root / relative_path).resolve()
            try:
                candidate.relative_to(self.root)
            except ValueError:
                continue
            entries.append({
                "registration_id": registration_id,
                "device_id": str(value.get("device_id") or registration_id),
                "connection_id": str(value.get("connection_id") or ""),
                "cloud_origin": str(value.get("cloud_origin") or ""),
                "path": relative_path,
            })
            seen.add(registration_id)
        if entries:
            return entries
        if not registry_valid and self.config.get("device_id") and self.key_path.is_file():
            device_id = str(self.config["device_id"])
            return [{
                "registration_id": device_id,
                "device_id": device_id,
                "connection_id": str(self.config.get("connection_id") or ""),
                "cloud_origin": str(self.config.get("cloud_origin") or ""),
                "path": ".",
            }]
        return []

    def _save_registration_entries(self, entries: Sequence[Mapping[str, Any]]) -> None:
        payload = {
            "schema_version": 2,
            "registrations": [
                {
                    "registration_id": str(entry.get("registration_id") or entry.get("device_id") or ""),
                    "device_id": str(entry.get("device_id") or ""),
                    "connection_id": str(entry.get("connection_id") or ""),
                    "cloud_origin": str(entry.get("cloud_origin") or ""),
                    "path": str(entry.get("path") or ""),
                }
                for entry in entries
            ],
        }
        encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        _atomic_write(self.registry_path, encoded)
        _atomic_write(self.registry_backup_path, encoded)

    def registration_runtimes(self) -> list[tuple[dict[str, str], "NexusComputerRuntime"]]:
        return [
            (entry, NexusComputerRuntime((self.root / entry["path"]).resolve()))
            for entry in self._registration_entries()
        ]

    def registration_statuses(self) -> list[dict[str, Any]]:
        statuses: list[dict[str, Any]] = []
        for entry, runtime in self.registration_runtimes():
            state = self._load_json(runtime.state_path)
            identity_ready = bool(runtime.config.get("device_id")) and runtime.key_path.is_file()
            statuses.append({
                "registration_id": entry["registration_id"],
                "device_id": str(runtime.config.get("device_id") or entry["device_id"]),
                "connection_id": str(runtime.config.get("connection_id") or entry["connection_id"]),
                "cloud_origin": str(runtime.config.get("cloud_origin") or entry["cloud_origin"]),
                "workspace_root": str(runtime.config.get("workspace_root") or ""),
                "state": (
                    str(state.get("state") or "starting")
                    if identity_ready
                    else "identity_error"
                ),
                "last_connected_at": state.get("last_connected_at"),
                "last_error": (
                    str(state.get("last_error") or "")
                    if identity_ready
                    else "Registration identity is missing or unreadable; repair or unpair this registration locally."
                ),
                "retry_seconds": float(state.get("retry_seconds") or 0),
            })
        return statuses

    def registration_runtime(self, registration_id: str = "") -> tuple[dict[str, str], "NexusComputerRuntime"]:
        registrations = self.registration_runtimes()
        requested = str(registration_id or "").strip()
        if not requested:
            if len(registrations) == 1:
                return registrations[0]
            if not registrations:
                raise NexusComputerRuntimeError("Computer Runtime is not paired")
            raise NexusComputerRuntimeError(
                "Multiple Computer registrations exist; pass --registration with a registration_id from nexus-computer status"
            )
        for entry, runtime in registrations:
            if requested in {entry["registration_id"], entry["device_id"]}:
                return entry, runtime
        raise NexusComputerRuntimeError("Computer Runtime registration was not found")

    def remove_registration(self, registration_id: str = "", *, local_only: bool = False) -> str:
        registry_lock = _RuntimeInstanceLock(self.root / REGISTRY_LOCK_NAME)
        registry_lock.acquire()
        try:
            selected, runtime = self.registration_runtime(registration_id)
            if not local_only:
                runtime.unpair_cloud()
            remaining = [
                entry
                for entry in self._registration_entries()
                if entry["registration_id"] != selected["registration_id"]
            ]
            self._save_registration_entries(remaining)
            if runtime.root == self.root:
                for path in (
                    runtime.key_path,
                    runtime.config_path,
                    runtime.journal_path,
                    runtime.state_path,
                    runtime.root / CLOUD_CA_NAME,
                ):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            else:
                shutil.rmtree(runtime.root, ignore_errors=True)
        finally:
            registry_lock.release()
        if not self._registration_entries():
            self.uninstall_user_service()
        return selected["registration_id"]

    @classmethod
    def setup(
        cls,
        pairing_url: str,
        *,
        root: Union[Path, str] = DEFAULT_ROOT,
        name: str = "",
        ca_file: str = "",
        install: bool = True,
    ) -> "NexusComputerRuntime":
        parsed = urlsplit(str(pairing_url).strip())
        if parsed.scheme != "nexus-computer" or parsed.netloc != "pair":
            raise NexusComputerRuntimeError("Pairing URL must use nexus-computer://pair")
        query = parse_qs(parsed.query)
        cloud_origin = _validated_cloud_origin(str((query.get("cloud") or [""])[0]))
        pairing_code = str((query.get("code") or [""])[0])
        if not pairing_code:
            raise NexusComputerRuntimeError("Pairing URL does not contain a pairing code")
        pairing_ca = b"" if ca_file else _pairing_cloud_trust(query)
        if pairing_ca and not cloud_origin.startswith("https://"):
            raise NexusComputerRuntimeError("Pairing URL Cloud trust requires HTTPS")
        supervisor = cls(root)
        _protect_directory(supervisor.root)
        registry_lock = _RuntimeInstanceLock(supervisor.root / REGISTRY_LOCK_NAME)
        registry_lock.acquire()
        try:
            existing = supervisor._registration_entries()
            if existing:
                registration_root = supervisor.root / REGISTRATIONS_DIRECTORY_NAME / uuid.uuid4().hex
            else:
                registration_root = supervisor.root
            runtime = cls(registration_root)
            _protect_directory(runtime.root)
            serialization, Ed25519PrivateKey = _require_crypto()
            private_key = Ed25519PrivateKey.generate()
            private_bytes = private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            public_pem = private_key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("ascii")
            capabilities, facts = runtime.detect_capabilities()
            response = _request_json(
                cloud_origin,
                "/api/v1/computer-runtime/v1/enroll/",
                payload={
                    "pairing_code": pairing_code,
                    "public_key_pem": public_pem,
                    "name": name,
                    "platform": _platform_name(),
                    "protocol_version": PROTOCOL_VERSION,
                    "capabilities": capabilities,
                    "facts": facts,
                },
                ca_file=ca_file,
                ca_pem=pairing_ca,
            )
            _atomic_write(runtime.key_path, private_bytes)
            persisted_ca = ""
            if pairing_ca:
                _atomic_write(runtime.root / CLOUD_CA_NAME, pairing_ca)
                persisted_ca = str((runtime.root / CLOUD_CA_NAME).resolve())
            config = {
                "schema_version": 1,
                "cloud_origin": cloud_origin,
                "device_id": str(response["device_id"]),
                "connection_id": str(response["connection_id"]),
                "workspace_root": str(response.get("workspace_root") or "~/.nexus"),
                "ca_file": str(Path(ca_file).expanduser().resolve()) if ca_file else persisted_ca,
                "paired_at": time.time(),
            }
            _atomic_write(runtime.config_path, json.dumps(config, indent=2, sort_keys=True).encode("utf-8"))
            runtime.config = config
            if runtime.root == supervisor.root:
                supervisor.config = dict(config)
            relative_path = os.path.relpath(runtime.root, supervisor.root)
            supervisor._save_registration_entries([
                *existing,
                {
                    "registration_id": str(response["device_id"]),
                    "device_id": str(response["device_id"]),
                    "connection_id": str(response["connection_id"]),
                    "cloud_origin": cloud_origin,
                    "path": relative_path,
                },
            ])
        finally:
            registry_lock.release()
        if install:
            supervisor.repair_user_service(pairing_succeeded=True)
        return runtime

    def _save_config(self) -> None:
        _atomic_write(
            self.config_path,
            json.dumps(self.config, indent=2, sort_keys=True).encode("utf-8"),
        )

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _private_key(self):
        serialization, Ed25519PrivateKey = _require_crypto()
        try:
            value = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise NexusComputerRuntimeError("Computer Runtime device key is missing or invalid") from exc
        if not isinstance(value, Ed25519PrivateKey):
            raise NexusComputerRuntimeError("Computer Runtime device key must use Ed25519")
        return value

    def _journal_key(self) -> bytes:
        serialization, _Ed25519PrivateKey = _require_crypto()
        raw = self._private_key().private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        return hashlib.sha256(b"nexus-computer-runtime-journal-v1\0" + raw).digest()

    def _load_journal_results(self) -> Dict[str, tuple[bool, dict[str, Any]]]:
        if not self.journal_path.is_file() or not self.key_path.is_file():
            return {}
        envelope = self._load_json(self.journal_path)
        if int(envelope.get("version") or 0) != 1:
            return {}
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            nonce_value = str(envelope["nonce"])
            ciphertext_value = str(envelope["ciphertext"])
            nonce = base64.urlsafe_b64decode(nonce_value + "=" * (-len(nonce_value) % 4))
            ciphertext = base64.urlsafe_b64decode(
                ciphertext_value + "=" * (-len(ciphertext_value) % 4)
            )
            plaintext = AESGCM(self._journal_key()).decrypt(
                nonce,
                ciphertext,
                b"nexus-computer-runtime-journal-v1",
            )
            decoded = json.loads(plaintext)
        except Exception:
            return {}
        result: Dict[str, tuple[bool, dict[str, Any]]] = {}
        entries = decoded.get("results") if isinstance(decoded, dict) else []
        for entry in entries[-128:] if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            command_id = str(entry.get("command_id") or "")
            value = entry.get("value")
            if command_id and len(command_id) <= 128 and isinstance(value, dict):
                result[command_id] = (bool(entry.get("ok")), value)
        return result

    def _save_journal_results(self) -> None:
        if not self.key_path.is_file():
            return
        entries = []
        total = 0
        for command_id, (ok, value) in reversed(list(self._memory_results.items())):
            entry = {"command_id": command_id, "ok": ok, "value": value}
            encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > 512 * 1024 or total + len(encoded) > 8 * 1024 * 1024:
                continue
            entries.append(entry)
            total += len(encoded)
            if len(entries) >= 128:
                break
        entries.reverse()
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        plaintext = json.dumps({"results": entries}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ciphertext = AESGCM(self._journal_key()).encrypt(
            nonce,
            plaintext,
            b"nexus-computer-runtime-journal-v1",
        )
        envelope = {
            "version": 1,
            "nonce": base64.urlsafe_b64encode(nonce).decode("ascii").rstrip("="),
            "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii").rstrip("="),
        }
        _atomic_write(
            self.journal_path,
            json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
        )

    def detect_capabilities(self) -> tuple[dict[str, int], dict[str, Any]]:
        browser_available = False
        browser_name = ""
        try:
            from .browser import _discover_chrome

            executable = _discover_chrome()
            import playwright.sync_api  # noqa: F401
            browser_available = bool(executable)
            browser_name = Path(executable).name if executable else ""
        except Exception:
            browser_available = False
        capabilities = {
            "workspace.v1": 1,
            "workspace.binary.v1": 1,
            "terminal.v1": 1,
            "terminal.stream.v1": 1,
            "tool_setup.v1": 1,
            "tool_setup.cas.v1": 1,
            "tool_setup.cas.v2": 1,
        }
        if browser_available:
            capabilities["browser.v1"] = 1
        facts = {
            "device_name": platform_module.node(),
            "os": platform_module.system(),
            "release": platform_module.release(),
            "machine": platform_module.machine(),
            "python": platform_module.python_version(),
            "browser_available": browser_available,
            "browser_name": browser_name,
        }
        return capabilities, facts

    def _resolved_root(self, requested: str = "") -> Path:
        configured = Path(str(self.config.get("workspace_root") or "~/.nexus")).expanduser().resolve()
        candidate = Path(str(requested)).expanduser().resolve() if requested else configured
        try:
            candidate.relative_to(configured)
        except ValueError as exc:
            raise RuntimeOperationError(
                "WORKSPACE_ROOT_MISMATCH",
                "Command workspace is outside the paired Computer root",
            ) from exc
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    def _safe_path(self, value: str, *, root: Path) -> Path:
        requested = Path(str(value or ".")).expanduser()
        candidate = requested.resolve() if requested.is_absolute() else (root / requested).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise RuntimeOperationError("WORKSPACE_PATH_OUTSIDE_ROOT", "Workspace path is outside the authorized root") from exc
        return candidate

    def _verify_scope(self, operation: str, required_scope: str) -> None:
        expected = {
            "workspace.test": {"connection.list", "connection.test"},
            "workspace.ensure_directory": {"files.write"},
            "workspace.list_files": {"files.list"},
            "workspace.read_file": {"files.read"},
            "workspace.write_file": {"files.write"},
            "workspace.transfer_read": {"files.read"},
            "workspace.transfer_write": {"files.write"},
            "command.execute": {"command.execute"},
            "terminal.open": {"command.execute"},
            "terminal.read": {"command.execute"},
            "terminal.write": {"command.execute"},
            "terminal.resize": {"command.execute"},
            "terminal.close": {"command.execute"},
            "tool_setup.detect": {"tool.setup"},
            "tool_setup.read_codex_config": {"tool.setup"},
            "tool_setup.write_codex_config": {"tool.setup"},
            "tool_setup.write_codex_config_cas": {"tool.setup"},
            "tool_setup.write_codex_config_fenced": {"tool.setup"},
            "tool_setup.fence_codex_config": {"tool.setup"},
            "tool_setup.backup_codex_config": {"tool.setup"},
            "tool_setup.rollback_codex_config": {"tool.setup"},
            "browser.open": {"browser.control"},
            "browser.observe": {"browser.control"},
            "browser.action": {"browser.control"},
            "browser.close": {"browser.control"},
        }.get(operation)
        if expected is None or required_scope not in expected:
            raise RuntimeOperationError("COMPUTER_SCOPE_MISMATCH", "Computer Runtime command scope does not match its operation")

    def dispatch(
        self,
        operation: str,
        required_scope: str,
        payload: Mapping[str, Any],
        cancel_event: Optional[threading.Event] = None,
    ) -> dict[str, Any]:
        self._verify_scope(operation, required_scope)
        data = dict(payload)
        if operation == "workspace.test":
            capabilities, facts = self.detect_capabilities()
            root = self._resolved_root()
            facts.update({"home_writable": os.access(Path.home(), os.W_OK), "workspace_root": str(root), "capabilities": capabilities})
            return {"facts": facts}
        if operation.startswith("workspace."):
            return self._workspace(operation, data)
        if operation == "command.execute":
            return self._run_command(data, cancel_event=cancel_event)
        if operation.startswith("terminal."):
            return self._terminal(operation, data)
        if operation.startswith("tool_setup."):
            return self._tool_setup(operation, data)
        if operation.startswith("browser."):
            return self._browser(operation, data)
        raise RuntimeOperationError("COMPUTER_OPERATION_UNSUPPORTED", "Computer Runtime operation is not supported")

    def _workspace(self, operation: str, data: dict[str, Any]) -> dict[str, Any]:
        if operation in {"workspace.transfer_read", "workspace.transfer_write"}:
            expected = "read_" if operation.endswith("read") else "write_"
            if not str(data.get("action") or "").startswith(expected):
                raise RuntimeOperationError("COMPUTER_SCOPE_MISMATCH", "File transfer action does not match scope")
            return self._file_transfers.operate(data)
        root = self._resolved_root(str(data.get("workspace_root") or ""))
        path = self._safe_path(str(data.get("path") or "."), root=root)
        if operation == "workspace.ensure_directory":
            path.mkdir(parents=True, exist_ok=True)
            return {"path": str(path.relative_to(root)).replace("\\", "/") or "."}
        if operation == "workspace.list_files":
            if not path.is_dir():
                raise RuntimeOperationError("WORKSPACE_NOT_FOUND", "Workspace directory was not found")
            items = []
            for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))[:1000]:
                stat = child.stat()
                items.append({
                    "name": child.name,
                    "path": str(child.relative_to(root)).replace("\\", "/"),
                    "relative_path": str(child.relative_to(root)).replace("\\", "/"),
                    "type": "directory" if child.is_dir() else "file",
                    "size_bytes": 0 if child.is_dir() else int(stat.st_size),
                    "modified_at": int(stat.st_mtime),
                })
            return {"path": str(path.relative_to(root)).replace("\\", "/") or ".", "items": items}
        if operation == "workspace.read_file":
            if not path.is_file():
                raise RuntimeOperationError("WORKSPACE_NOT_FOUND", "Workspace file was not found")
            maximum = max(1, min(int(data.get("max_bytes") or 1048576), MAX_TEXT_RESULT))
            with path.open("rb") as handle:
                raw = handle.read(maximum + 1)
            if len(raw) > maximum:
                raise RuntimeOperationError("WORKSPACE_FILE_TOO_LARGE", "Workspace file exceeds the allowed size")
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeOperationError("WORKSPACE_FILE_NOT_TEXT", "Workspace file is not UTF-8 text") from exc
            return {"path": str(path.relative_to(root)).replace("\\", "/"), "content": content, "size_bytes": len(raw)}
        if operation == "workspace.write_file":
            content = str(data.get("content") or "")
            encoded = content.encode("utf-8")
            if len(encoded) > MAX_TEXT_RESULT:
                raise RuntimeOperationError("WORKSPACE_FILE_TOO_LARGE", "Workspace write exceeds the allowed size")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".nexus-tmp-" + uuid.uuid4().hex)
            temporary.write_bytes(encoded)
            os.replace(temporary, path)
            return {"path": str(path.relative_to(root)).replace("\\", "/"), "content": content, "size_bytes": len(encoded)}
        raise RuntimeOperationError("COMPUTER_OPERATION_UNSUPPORTED", "Workspace operation is not supported")

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except OSError:
                pass

    def _run_command(
        self,
        data: dict[str, Any],
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> dict[str, Any]:
        root = self._resolved_root(str(data.get("workspace_root") or ""))
        cwd = self._safe_path(str(data.get("cwd") or "."), root=root)
        if not cwd.is_dir():
            raise RuntimeOperationError("WORKSPACE_NOT_FOUND", "Command working directory was not found")
        timeout = max(1, min(int(data.get("timeout_seconds") or 30), 900))
        maximum = max(1024, min(int(data.get("output_max_bytes") or 1048576), MAX_TEXT_RESULT))
        command = str(data.get("command") or "")
        if not command or len(command.encode("utf-8")) > 65536:
            raise RuntimeOperationError("COMMAND_INVALID", "Command is empty or too large")
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + timeout
        stdout = b""
        stderr = b""
        while True:
            if cancel_event is not None and cancel_event.is_set():
                self._terminate_process_tree(process)
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                raise RuntimeOperationError("COMPUTER_RUNTIME_CANCELED", "Computer command was canceled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate_process_tree(process)
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                raise RuntimeOperationError("COMMAND_TIMEOUT", "Computer command timed out")
            try:
                stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        stdout_text = bytes(stdout or b"")[:maximum].decode("utf-8", "replace")
        stderr_text = bytes(stderr or b"")[:maximum].decode("utf-8", "replace")
        return {
            "cwd": str(cwd.relative_to(root)).replace("\\", "/") or ".",
            "command": command,
            "exit_code": int(process.returncode),
            "stdout": stdout_text,
            "stderr": stderr_text,
            "timed_out": False,
            "timeout_seconds": timeout,
        }

    def _terminal(self, operation: str, data: dict[str, Any]) -> dict[str, Any]:
        session_id = str(data.get("session_id") or "")
        if operation == "terminal.open":
            root = self._resolved_root(str(data.get("workspace_root") or ""))
            session_id = uuid.uuid4().hex
            self._terminals[session_id] = _TerminalProcess(
                shell=str(data.get("shell") or "auto"), cwd=root,
                cols=int(data.get("cols") or 120), rows=int(data.get("rows") or 34),
            )
            return {"session_id": session_id}
        terminal = self._terminals.get(session_id)
        if terminal is None:
            raise RuntimeOperationError("TERMINAL_SESSION_LOST", "Computer terminal session was not found")
        if operation == "terminal.read":
            return {"data": terminal.read(float(data.get("timeout_seconds") or 0.25))}
        if operation == "terminal.write":
            terminal.write(str(data.get("data") or ""))
            return {"written": True}
        if operation == "terminal.resize":
            terminal.resize(cols=int(data.get("cols") or 120), rows=int(data.get("rows") or 34))
            return {"resized": True}
        if operation == "terminal.close":
            terminal.close()
            self._terminals.pop(session_id, None)
            return {"closed": True}
        raise RuntimeOperationError("COMPUTER_OPERATION_UNSUPPORTED", "Terminal operation is not supported")

    @staticmethod
    def _codex_path() -> Path:
        return Path.home() / ".codex" / "nexus.config.toml"

    def _tool_setup(self, operation: str, data: dict[str, Any]) -> dict[str, Any]:
        if operation in {"tool_setup.write_codex_config_fenced", "tool_setup.fence_codex_config"}:
            from .computer_tool_config import compare_and_write_codex
            return compare_and_write_codex(self, data, fenced=True, barrier=operation == "tool_setup.fence_codex_config")
        if operation == "tool_setup.write_codex_config_cas":
            from .computer_tool_config import compare_and_write_codex
            return compare_and_write_codex(self, data)
        path = self._codex_path()
        if operation == "tool_setup.detect":
            tools = {}
            for key, executable in (("codex", "codex"), ("claude_code", "claude")):
                located = shutil.which(executable) or ""
                version = ""
                if located:
                    try:
                        version = subprocess.run([located, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()[:256]
                    except Exception:
                        version = ""
                tools[key] = {"installed": bool(located), "command": executable, "path": located, "version": version}
            return {"runner": "computer_runtime", "tools": tools}
        if operation == "tool_setup.read_codex_config":
            return {"path": str(path), "exists": path.is_file(), "content": path.read_text(encoding="utf-8") if path.is_file() else ""}
        if operation == "tool_setup.write_codex_config":
            content = str(data.get("content") or "")
            if len(content.encode("utf-8")) > MAX_TEXT_RESULT:
                raise RuntimeOperationError("TOOL_CONFIG_TOO_LARGE", "Codex config exceeds the allowed size")
            _atomic_user_file_write(path, content.encode("utf-8"))
            return {"path": str(path), "exists": True, "content": content}
        backup_root = self.root / "config-backups"
        if operation == "tool_setup.backup_codex_config":
            backup = backup_root / str(int(time.time() * 1000)) / "nexus.config.toml"
            _atomic_write(backup, str(data.get("content") or "").encode("utf-8"))
            return {"backup_path": str(backup)}
        if operation == "tool_setup.rollback_codex_config":
            candidates = sorted(backup_root.glob("*/nexus.config.toml"), reverse=True)
            if not candidates:
                raise RuntimeOperationError("TOOL_CONFIG_BACKUP_NOT_FOUND", "No Codex config backup is available")
            content = candidates[0].read_bytes()
            _atomic_user_file_write(path, content)
            return {"path": str(path), "exists": True, "content": content.decode("utf-8"), "backup_path": str(candidates[0])}
        raise RuntimeOperationError("COMPUTER_OPERATION_UNSUPPORTED", "Tool setup operation is not supported")

    def _browser(self, operation: str, data: dict[str, Any]) -> dict[str, Any]:
        from .browser import (
            NexusBrowserAction,
            NexusBrowserError,
            NexusBrowserSession,
            NexusBrowserSessionLost,
            NexusBrowserStaleObservation,
            NexusBrowserUnavailable,
        )

        session_id = str(data.get("browser_session_id") or "")
        if not session_id:
            raise RuntimeOperationError("BROWSER_SESSION_LOST", "Browser session ID is missing")
        viewport = data.get("viewport") if isinstance(data.get("viewport"), (list, tuple)) else [1280, 720]
        session = self._browsers.get(session_id)
        try:
            if operation == "browser.close":
                if session is not None:
                    session.close()
                    self._browsers.pop(session_id, None)
                return {"closed": True}
            if session is None:
                if operation != "browser.open":
                    raise RuntimeOperationError("BROWSER_SESSION_LOST", "Browser session state was lost")
                session = NexusBrowserSession(run_id=session_id, publisher=lambda *_args: None, viewport=viewport)
                self._browsers[session_id] = session
            if operation == "browser.open":
                observation = session.open(str(data.get("url") or ""), timeout=float(data.get("timeout") or 30))
            elif operation == "browser.observe":
                observation = session.observe()
            elif operation == "browser.action":
                action = data.get("action") if isinstance(data.get("action"), dict) else {}
                observation = session.perform(NexusBrowserAction(
                    kind=str(action.get("kind") or ""),
                    parameters=action.get("parameters") if isinstance(action.get("parameters"), dict) else {},
                    expected_revision=int(action["expected_revision"]) if action.get("expected_revision") is not None else None,
                ))
            else:
                raise RuntimeOperationError("COMPUTER_OPERATION_UNSUPPORTED", "Browser operation is not supported")
        except RuntimeOperationError:
            raise
        except NexusBrowserSessionLost as exc:
            self._browsers.pop(session_id, None)
            raise RuntimeOperationError("BROWSER_SESSION_LOST", str(exc) or "Browser session was lost") from exc
        except NexusBrowserStaleObservation as exc:
            raise RuntimeOperationError(
                "BROWSER_STALE_OBSERVATION",
                str(exc) or "Browser observation is stale; observe the page again",
            ) from exc
        except NexusBrowserUnavailable as exc:
            raise RuntimeOperationError(
                "BROWSER_UNAVAILABLE",
                str(exc) or "Browser automation is unavailable on this Computer",
            ) from exc
        except NexusBrowserError as exc:
            raise RuntimeOperationError("BROWSER_ACTION_FAILED", str(exc) or "Browser action failed") from exc
        upload = data.get("_nexus_upload") if isinstance(data.get("_nexus_upload"), dict) else {}
        upload_result = _upload_bytes(
            str(self.config.get("cloud_origin") or ""),
            str(upload.get("endpoint") or ""),
            token=str(upload.get("token") or ""),
            content=observation.image,
            content_type=observation.content_type,
            ca_file=str(self.config.get("ca_file") or ""),
        )
        return {
            "observation_id": observation.observation_id,
            "revision": observation.revision,
            "url": observation.url,
            "title": observation.title,
            "viewport": list(observation.viewport),
            "image_upload": {
                "command_id": str(data.get("_nexus_command_id") or ""),
                "upload_id": str(upload_result.get("upload_id") or ""),
            },
            "content_type": observation.content_type,
            "dom": {
                "revision": observation.dom.revision,
                "truncated": observation.dom.truncated,
                "nodes": [
                    {
                        "ref": node.ref, "tag": node.tag, "role": node.role,
                        "name": node.name, "text": node.text, "selector": node.selector,
                        "bounds": list(node.bounds), "disabled": node.disabled,
                        "checked": node.checked, "selected": node.selected, "expanded": node.expanded,
                    }
                    for node in observation.dom.nodes[:500]
                ],
            },
        }

    async def run_forever(self) -> None:
        if not self._registration_entries():
            raise NexusComputerRuntimeError("Computer Runtime is not paired; run nexus-computer setup first")
        instance_lock = _RuntimeInstanceLock(self.root / LOCK_NAME)
        instance_lock.acquire()
        _atomic_write(self.pid_path, str(os.getpid()).encode("ascii"))
        workers: dict[str, tuple[NexusComputerRuntime, asyncio.Task]] = {}
        try:
            while not self._stop.is_set():
                if self._consume_restart_request():
                    self._stop.set()
                    break
                registrations = {
                    entry["registration_id"]: entry
                    for entry in self._registration_entries()
                }
                for registration_id, entry in registrations.items():
                    worker = workers.get(registration_id)
                    registration_root = (self.root / entry["path"]).resolve()
                    identity_ready = (
                        (registration_root / CONFIG_NAME).is_file()
                        and (registration_root / KEY_NAME).is_file()
                    )
                    if not identity_ready:
                        if worker is not None:
                            old_runtime, old_task = workers.pop(registration_id)
                            old_task.cancel()
                            old_runtime.close()
                        continue
                    if worker is None or worker[1].done():
                        if worker is not None:
                            old_runtime, old_task = worker
                            try:
                                old_task.result()
                            except (asyncio.CancelledError, Exception):
                                pass
                            old_runtime.close()
                        runtime = NexusComputerRuntime(registration_root)
                        task = asyncio.create_task(
                            runtime._run_registration_forever(),
                            name=f"nexus-computer-{registration_id}",
                        )
                        workers[registration_id] = (runtime, task)
                removed = set(workers).difference(registrations)
                for registration_id in removed:
                    runtime, task = workers.pop(registration_id)
                    task.cancel()
                    runtime.close()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            for _runtime, task in workers.values():
                task.cancel()
            await asyncio.gather(
                *(task for _runtime, task in workers.values()),
                return_exceptions=True,
            )
            for runtime, _task in workers.values():
                runtime.close()
            try:
                if self.pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                    self.pid_path.unlink()
            except (FileNotFoundError, OSError):
                pass
            instance_lock.release()

    def _consume_restart_request(self) -> bool:
        try:
            self.restart_request_path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            self._log(f"restart request could not be consumed: {self._safe_error(exc)}")
            return False
        return True

    async def _run_registration_forever(self) -> None:
        if not self.config.get("device_id") or not self.key_path.is_file():
            raise NexusComputerRuntimeError("Computer Runtime registration identity is missing")
        self._send_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(8)
        delay = 1.0
        while not self._stop.is_set():
            started = time.monotonic()
            self._write_runtime_state(state="connecting", retry_seconds=0, last_error="")
            try:
                await self._run_connection()
                error = "Cloud closed the Computer Runtime connection"
            except asyncio.CancelledError:
                self._write_runtime_state(state="stopped", retry_seconds=0, last_error="")
                raise
            except Exception as exc:
                error = self._safe_error(exc)
                self._log(f"connection unavailable: {error}")
            if self._stop.is_set():
                break
            if time.monotonic() - started >= 30.0:
                delay = 1.0
            retry_delay = min(delay, 30.0)
            self._write_runtime_state(
                state="reconnecting",
                retry_seconds=retry_delay,
                last_error=error,
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=retry_delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2.0, 30.0)

    def _write_runtime_state(
        self,
        *,
        state: str,
        retry_seconds: float,
        last_error: str,
        connected: bool = False,
    ) -> None:
        previous = self._load_json(self.state_path)
        payload = {
            "schema_version": 1,
            "state": str(state),
            "updated_at": time.time(),
            "retry_seconds": float(retry_seconds),
            "last_error": str(last_error)[:500],
        }
        if connected:
            payload["last_connected_at"] = time.time()
        elif previous.get("last_connected_at") is not None:
            payload["last_connected_at"] = previous["last_connected_at"]
        _atomic_write(
            self.state_path,
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
        )

    def _create_cloud_session(self) -> dict[str, Any]:
        cloud = _validated_cloud_origin(str(self.config["cloud_origin"]))
        device_id = str(self.config["device_id"])
        ca_file = str(self.config.get("ca_file") or "")
        challenge = _request_json(
            cloud,
            "/api/v1/computer-runtime/v1/sessions/",
            payload={"device_id": device_id},
            ca_file=ca_file,
        )
        plaintext = str(challenge["challenge"])
        signature = self._private_key().sign(
            f"nexus-computer-session-v1\n{device_id}\n{plaintext}".encode("utf-8")
        )
        return _request_json(
            cloud,
            "/api/v1/computer-runtime/v1/sessions/",
            payload={
                "device_id": device_id,
                "challenge": plaintext,
                "signature": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
            },
            ca_file=ca_file,
        )

    def unpair_cloud(self) -> None:
        session = self._create_cloud_session()
        _request_json(
            _validated_cloud_origin(str(self.config["cloud_origin"])),
            "/api/v1/computer-runtime/v1/unpair/",
            payload={"ticket": str(session["ticket"])},
            ca_file=str(self.config.get("ca_file") or ""),
        )

    async def _run_connection(self) -> None:
        ca_file = str(self.config.get("ca_file") or "")
        session = await asyncio.to_thread(self._create_cloud_session)
        websockets = _require_websockets()
        websocket_url = str(session["websocket_url"])
        ssl_context = _ssl_context(ca_file) if websocket_url.startswith("wss://") else None
        async with websockets.connect(
            websocket_url,
            additional_headers={"Authorization": f"Bearer {session['ticket']}"},
            user_agent_header=COMPUTER_USER_AGENT,
            ssl=ssl_context,
            max_size=4 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as websocket:
            connected = json.loads(await asyncio.wait_for(websocket.recv(), timeout=15))
            generation = int(connected.get("generation") or 0)
            capabilities, facts = self.detect_capabilities()
            await self._send(websocket, {"type": "hello", "generation": generation, "capabilities": capabilities, "facts": facts})
            self._write_runtime_state(
                state="connected",
                retry_seconds=0,
                last_error="",
                connected=True,
            )
            heartbeat = asyncio.create_task(self._heartbeat(websocket, generation=generation))
            tasks: set[asyncio.Task] = set()
            try:
                async for raw in websocket:
                    value = json.loads(raw)
                    if not isinstance(value, dict) or value.get("version") != PROTOCOL_VERSION:
                        continue
                    if value.get("type") == "command":
                        task = asyncio.create_task(self._handle_command(websocket, value))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                    elif value.get("type") == "cancel":
                        command_id = str(value.get("command_id") or "")
                        cancel_event = self._cancel_events.get(command_id)
                        if cancel_event is not None:
                            cancel_event.set()
                        await self._send(websocket, {"type": "cancel_ack", "command_id": command_id})
                    elif str(value.get("type") or "").startswith("terminal_stream_"):
                        # Stream control frames are ordered.  In particular,
                        # separate xterm input events must reach stdin in the
                        # exact order in which the caller typed them.
                        await self._handle_terminal_stream_frame(websocket, value)
            finally:
                heartbeat.cancel()
                for task in tasks:
                    task.cancel()
                for task in list(self._terminal_stream_tasks.values()):
                    task.cancel()
                await asyncio.gather(
                    heartbeat,
                    *tasks,
                    *list(self._terminal_stream_tasks.values()),
                    return_exceptions=True,
                )
                self._terminal_stream_tasks.clear()
                terminals = list(self._terminals.values())
                self._terminals.clear()
                await asyncio.gather(
                    *(asyncio.to_thread(terminal.close) for terminal in terminals),
                    return_exceptions=True,
                )

    async def _send(self, websocket, payload: dict[str, Any]) -> None:
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock:
            self._send_sequence += 1
            value = {"version": PROTOCOL_VERSION, "sequence": self._send_sequence, **payload}
            await websocket.send(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    async def _heartbeat(self, websocket, *, generation: int) -> None:
        interval = 15
        while True:
            await asyncio.sleep(interval)
            await asyncio.to_thread(self._file_transfers.expire)
            await self._send(websocket, {"type": "heartbeat", "generation": generation, "load": len(self._memory_results)})

    async def _handle_command(self, websocket, command: dict[str, Any]) -> None:
        command_id = str(command.get("command_id") or "")
        if not command_id:
            return
        await self._send(websocket, {"type": "command_ack", "command_id": command_id})
        deadline_text = str(command.get("deadline") or "")
        try:
            deadline = datetime.fromisoformat(deadline_text.replace("Z", "+00:00"))
        except ValueError:
            deadline = None
        if deadline is None or deadline.tzinfo is None or deadline <= datetime.now(timezone.utc):
            value = {"code": "COMPUTER_RUNTIME_DEADLINE_EXCEEDED", "message": "Computer Runtime command deadline expired"}
            self._remember(command_id, False, value)
            await self._send(websocket, {"type": "error", "command_id": command_id, **value})
            return
        cached = self._memory_results.get(command_id)
        if cached is not None:
            ok, value = cached
            await self._send(websocket, {"type": "result" if ok else "error", "command_id": command_id, **value})
            return
        if command_id in self._completed_ids:
            await self._send(websocket, {
                "type": "error", "command_id": command_id,
                "code": "COMMAND_RESULT_LOST",
                "message": "The Runtime restarted after completing this command; it will not execute it twice.",
            })
            return
        execution = self._inflight.get(command_id)
        if execution is None:
            execution = asyncio.create_task(self._execute_command(command_id, command))
            self._inflight[command_id] = execution
            execution.add_done_callback(lambda _task, key=command_id: self._inflight.pop(key, None))
        ok, value = await asyncio.shield(execution)
        await self._send(websocket, {"type": "result" if ok else "error", "command_id": command_id, **value})

    async def _handle_terminal_stream_frame(self, websocket, frame: dict[str, Any]) -> None:
        stream_id = str(frame.get("stream_id") or "")
        if not stream_id or len(stream_id) > 128:
            return
        frame_type = str(frame.get("type") or "")
        try:
            if frame_type == "terminal_stream_open":
                if stream_id in self._terminals:
                    raise RuntimeOperationError("TERMINAL_STREAM_EXISTS", "Computer terminal stream already exists")
                root = self._resolved_root(str(frame.get("workspace_root") or ""))
                terminal = await asyncio.to_thread(
                    _TerminalProcess,
                    shell=str(frame.get("shell") or "auto"),
                    cwd=root,
                    cols=int(frame.get("cols") or 120),
                    rows=int(frame.get("rows") or 34),
                )
                self._terminals[stream_id] = terminal
                await self._send(websocket, {"type": "terminal_stream_opened", "stream_id": stream_id})
                pump = asyncio.create_task(self._pump_terminal_stream(websocket, stream_id, terminal))
                self._terminal_stream_tasks[stream_id] = pump
                pump.add_done_callback(
                    lambda _task, key=stream_id: self._terminal_stream_tasks.pop(key, None)
                )
                return
            terminal = self._terminals.get(stream_id)
            if terminal is None:
                raise RuntimeOperationError("TERMINAL_SESSION_LOST", "Computer terminal stream was not found")
            if frame_type == "terminal_stream_input":
                data = str(frame.get("data") or "")
                if len(data.encode("utf-8", errors="replace")) > MAX_TERMINAL_STREAM_FRAME_BYTES:
                    raise RuntimeOperationError("TERMINAL_INPUT_TOO_LARGE", "Computer terminal input frame exceeded its limit")
                await asyncio.to_thread(terminal.write, data)
                return
            if frame_type == "terminal_stream_resize":
                await asyncio.to_thread(
                    terminal.resize, cols=int(frame.get("cols") or 120), rows=int(frame.get("rows") or 34),
                )
                return
            if frame_type == "terminal_stream_close":
                await self._close_terminal_stream(stream_id)
                await self._send(websocket, {"type": "terminal_stream_closed", "stream_id": stream_id})
                return
        except RuntimeOperationError as exc:
            await self._send(websocket, {
                "type": "terminal_stream_error",
                "stream_id": stream_id,
                "code": exc.code,
                "message": self._safe_error(exc),
            })
        except Exception as exc:
            await self._send(websocket, {
                "type": "terminal_stream_error",
                "stream_id": stream_id,
                "code": "TERMINAL_STREAM_FAILED",
                "message": self._safe_error(exc),
            })

    async def _pump_terminal_stream(self, websocket, stream_id: str, terminal: _TerminalProcess) -> None:
        try:
            while self._terminals.get(stream_id) is terminal:
                data = await asyncio.to_thread(terminal.read, 0.25)
                if data:
                    encoded = data.encode("utf-8", errors="replace")
                    offset = 0
                    while offset < len(encoded):
                        end = min(offset + MAX_TERMINAL_STREAM_FRAME_BYTES, len(encoded))
                        while end < len(encoded) and end > offset and encoded[end] & 0xC0 == 0x80:
                            end -= 1
                        chunk = encoded[offset:end].decode("utf-8")
                        await self._send(websocket, {
                            "type": "terminal_stream_output",
                            "stream_id": stream_id,
                            "data": chunk,
                        })
                        offset = end
                if terminal.process.poll() is not None and not terminal.reader.is_alive() and terminal.output.empty():
                    self._terminals.pop(stream_id, None)
                    await self._send(websocket, {
                        "type": "terminal_stream_closed",
                        "stream_id": stream_id,
                        "exit_code": int(terminal.process.returncode or 0),
                    })
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._terminals.pop(stream_id, None)
            await self._send(websocket, {
                "type": "terminal_stream_error",
                "stream_id": stream_id,
                "code": "TERMINAL_STREAM_FAILED",
                "message": self._safe_error(exc),
            })
        finally:
            if self._terminals.get(stream_id) is terminal:
                self._terminals.pop(stream_id, None)
            await asyncio.to_thread(terminal.close)

    async def _close_terminal_stream(self, stream_id: str) -> None:
        terminal = self._terminals.pop(stream_id, None)
        pump = self._terminal_stream_tasks.pop(stream_id, None)
        current = asyncio.current_task()
        if pump is not None and pump is not current:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        if terminal is not None:
            await asyncio.to_thread(terminal.close)

    async def _execute_command(
        self,
        command_id: str,
        command: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(8)
        async with self._semaphore:
            cancel_event = threading.Event()
            self._cancel_events[command_id] = cancel_event
            try:
                result = await asyncio.to_thread(
                    self.dispatch,
                    str(command.get("operation") or ""),
                    str(command.get("required_scope") or ""),
                    {
                        **(command.get("payload") if isinstance(command.get("payload"), dict) else {}),
                        "_nexus_upload": command.get("upload") if isinstance(command.get("upload"), dict) else {},
                        "_nexus_download": command.get("download") if isinstance(command.get("download"), dict) else {},
                        "_nexus_command_id": command_id,
                    },
                    cancel_event,
                )
            except RuntimeOperationError as exc:
                value = {"code": exc.code, "message": self._safe_error(exc)}
                self._remember(command_id, False, value)
                return False, value
            except Exception as exc:
                value = {"code": "COMPUTER_RUNTIME_COMMAND_FAILED", "message": self._safe_error(exc)}
                self._remember(command_id, False, value)
                return False, value
            else:
                value = {"result": result}
                self._remember(command_id, True, value)
                return True, value
            finally:
                self._cancel_events.pop(command_id, None)

    def _remember(self, command_id: str, ok: bool, value: dict[str, Any]) -> None:
        self._memory_results[command_id] = (ok, value)
        if len(self._memory_results) > 512:
            self._memory_results.pop(next(iter(self._memory_results)))
        self._completed_ids.append(command_id)
        self._save_journal_results()

    @staticmethod
    def _safe_error(exc: BaseException) -> str:
        if isinstance(exc, RuntimeOperationError):
            return str(exc)[:500]
        return f"{exc.__class__.__name__}: Computer Runtime operation failed"[:500]

    def _log(self, message: str) -> None:
        _protect_directory(self.root)
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {str(message)[:1000]}\n"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        _protect_file(self.log_path)

    def close(self) -> None:
        self._file_transfers.close()
        for terminal in list(self._terminals.values()):
            terminal.close()
        self._terminals.clear()
        for browser in list(self._browsers.values()):
            try:
                browser.close()
            except Exception:
                pass
        self._browsers.clear()

    def _runtime_command(self) -> list[str]:
        entrypoint = shutil.which("nexus-computer")
        return (
            [entrypoint, "--root", str(self.root), "run"]
            if entrypoint
            else [
                sys.executable,
                "-c",
                "from nexus_agent.computer_runtime import main; raise SystemExit(main())",
                "--root",
                str(self.root),
                "run",
            ]
        )

    def _install_windows_run_fallback(self, command: Sequence[str]) -> None:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            WINDOWS_RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(
                key,
                WINDOWS_RUN_VALUE,
                0,
                winreg.REG_SZ,
                subprocess.list2cmdline(list(command)),
            )

    @staticmethod
    def _remove_windows_run_fallback() -> None:
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                WINDOWS_RUN_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.DeleteValue(key, WINDOWS_RUN_VALUE)
        except FileNotFoundError:
            pass

    def _windows_run_fallback_installed(self, command: Sequence[str]) -> bool:
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                WINDOWS_RUN_KEY,
                0,
                winreg.KEY_QUERY_VALUE,
            ) as key:
                value, value_type = winreg.QueryValueEx(key, WINDOWS_RUN_VALUE)
        except (FileNotFoundError, OSError):
            return False
        return value_type == winreg.REG_SZ and str(value) == subprocess.list2cmdline(list(command))

    def _instance_running(self) -> bool:
        probe = _RuntimeInstanceLock(self.root / LOCK_NAME)
        try:
            probe.acquire()
        except NexusComputerRuntimeError:
            return True
        probe.release()
        return False

    def _wait_for_instance_start(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(float(timeout), 0.1)
        while time.monotonic() < deadline:
            if self._instance_running():
                return True
            time.sleep(0.05)
        return self._instance_running()

    @staticmethod
    def _service_error(exc: BaseException) -> str:
        value = str(exc).strip()
        return (value or exc.__class__.__name__)[:500]

    def install_user_service(self) -> str:
        command = self._runtime_command()
        if os.name == "nt":
            task = subprocess.run([
                "schtasks.exe", "/Create", "/TN", WINDOWS_TASK_NAME, "/SC", "ONLOGON",
                "/RL", "LIMITED", "/TR", subprocess.list2cmdline(command), "/F",
            ], check=False, capture_output=True, text=True, timeout=30)
            if task.returncode == 0:
                try:
                    self._remove_windows_run_fallback()
                except OSError:
                    pass
                backend = "scheduled_task"
            else:
                try:
                    self._install_windows_run_fallback(command)
                except OSError as exc:
                    task_error = (task.stderr or task.stdout or "Task Scheduler rejected the request").strip()
                    raise NexusComputerRuntimeError(
                        "Windows could not install current-user autostart. "
                        f"Task Scheduler: {task_error[:240]}; Windows Run: {self._service_error(exc)}"
                    ) from exc
                backend = "registry_run"
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return backend
        if sys.platform == "darwin":
            directory = Path.home() / "Library" / "LaunchAgents"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "com.nexus.computer-runtime.plist"
            arguments = "".join(f"<string>{_xml_escape(part)}</string>" for part in command)
            plist = f'<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict><key>Label</key><string>com.nexus.computer-runtime</string><key>ProgramArguments</key><array>{arguments}</array><key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>'
            _atomic_write(path, plist.encode("utf-8"))
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True, timeout=15)
            subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True, capture_output=True, timeout=30)
            return "launch_agent"
        directory = Path.home() / ".config" / "systemd" / "user"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "nexus-computer.service"
        unit = "\n".join([
            "[Unit]", "Description=Nexus Computer Runtime", "After=network-online.target", "Wants=network-online.target", "",
            "[Service]", "Type=simple", "ExecStart=" + " ".join(shlex.quote(part) for part in command), "Restart=always", "RestartSec=3", "",
            "[Install]", "WantedBy=default.target", "",
        ])
        _atomic_write(path, unit.encode("utf-8"))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=30)
        subprocess.run(["systemctl", "--user", "enable", "--now", "nexus-computer.service"], check=True, timeout=30)
        return "systemd_user"

    def repair_user_service(self, *, pairing_succeeded: bool = False) -> dict[str, Any]:
        if not self._registration_entries():
            raise NexusComputerRuntimeError(
                "Computer Runtime is not paired; create a new pairing link and run nexus-computer setup"
            )
        current = self.service_status()
        if current["installed"] and current["running"]:
            self.config.pop("service_install_error", None)
            if self.config:
                self._save_config()
            return current
        try:
            backend = self.install_user_service()
        except Exception as exc:
            detail = self._service_error(exc)
            self.config["service_install_error"] = detail
            self._save_config()
            prefix = "Computer pairing succeeded, but " if pairing_succeeded else ""
            raise NexusComputerRuntimeError(
                f"{prefix}the background Runtime could not be installed. "
                "Run nexus-computer repair after resolving the reported Windows policy error. "
                f"Detail: {detail}"
            ) from exc
        self.config["service_backend"] = backend
        self.config["service_installed_at"] = time.time()
        self.config.pop("service_install_error", None)
        self._save_config()
        self._wait_for_instance_start()
        return self.service_status()

    def service_status(self) -> dict[str, Any]:
        registrations = self.registration_statuses()
        paired = bool(registrations)
        running = self._instance_running() if paired else False
        backend = ""
        installed = False
        if os.name == "nt":
            task = subprocess.run(
                ["schtasks.exe", "/Query", "/TN", WINDOWS_TASK_NAME],
                check=False,
                capture_output=True,
                timeout=15,
            )
            scheduled_task_installed = task.returncode == 0
            registry_run_installed = self._windows_run_fallback_installed(
                self._runtime_command()
            )
            configured_backend = str(self.config.get("service_backend") or "")
            if configured_backend == "registry_run" and registry_run_installed:
                backend, installed = "registry_run", True
            elif configured_backend == "scheduled_task" and scheduled_task_installed:
                backend, installed = "scheduled_task", True
            elif scheduled_task_installed:
                backend, installed = "scheduled_task", True
            elif registry_run_installed:
                backend, installed = "registry_run", True
        elif sys.platform == "darwin":
            installed = (Path.home() / "Library" / "LaunchAgents" / "com.nexus.computer-runtime.plist").is_file()
            backend = "launch_agent" if installed else ""
        else:
            installed = (Path.home() / ".config" / "systemd" / "user" / "nexus-computer.service").is_file()
            backend = "systemd_user" if installed else ""

        if not paired:
            state, code, message = "unpaired", "COMPUTER_RUNTIME_UNPAIRED", "Pair this Computer first."
        elif running:
            state, code, message = "running", "", "The Runtime process is running and reconnects to Cloud automatically."
        elif installed:
            state, code, message = "stopped", "COMPUTER_RUNTIME_STOPPED", "Autostart is installed, but the Runtime is stopped. Run nexus-computer restart."
        else:
            state, code, message = "service_missing", "COMPUTER_RUNTIME_SERVICE_MISSING", "The background Runtime is not installed. Run nexus-computer repair."
        return {
            "installed": installed,
            "running": running,
            "backend": backend,
            "state": state,
            "code": code,
            "message": message,
            "last_install_error": str(self.config.get("service_install_error") or ""),
            "registration_count": len(registrations),
            "connected_registration_count": sum(
                1 for registration in registrations if registration["state"] == "connected"
            ),
            "registrations": registrations,
        }

    def restart_user_service(self) -> None:
        if not self._registration_entries():
            raise NexusComputerRuntimeError(
                "Computer Runtime is not paired; create a pairing link and run nexus-computer setup"
            )
        if os.name == "nt":
            status = self.service_status()
            if not status["installed"]:
                self.repair_user_service()
                return
            if status["backend"] == "scheduled_task":
                subprocess.run(["schtasks.exe", "/End", "/TN", WINDOWS_TASK_NAME], capture_output=True, timeout=15)
                if status["running"]:
                    _wait_for_instance_lock_release(self.root / LOCK_NAME)
                started = subprocess.run(
                    ["schtasks.exe", "/Run", "/TN", WINDOWS_TASK_NAME],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if started.returncode != 0:
                    detail = (started.stderr or started.stdout or "Task Scheduler rejected the restart").strip()
                    raise NexusComputerRuntimeError(
                        f"Nexus Computer Runtime restart failed: {detail[:300]}"
                    )
                if not self._wait_for_instance_start(timeout=10):
                    raise NexusComputerRuntimeError(
                        "Nexus Computer Runtime did not start after Task Scheduler accepted the restart"
                    )
                return
            if status["backend"] == "registry_run":
                if status["running"]:
                    _atomic_user_file_write(
                        self.restart_request_path,
                        str(time.time()).encode("ascii"),
                    )
                    try:
                        _wait_for_instance_lock_release(self.root / LOCK_NAME, timeout=12)
                    except NexusComputerRuntimeError:
                        try:
                            pid = int(self.pid_path.read_text(encoding="ascii").strip())
                            stopped = subprocess.run(
                                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                                check=False,
                                capture_output=True,
                                text=True,
                                timeout=15,
                            )
                            if stopped.returncode != 0:
                                detail = (
                                    stopped.stderr
                                    or stopped.stdout
                                    or "Windows rejected the process-tree stop"
                                ).strip()
                                raise OSError(detail[:300])
                            _wait_for_instance_lock_release(self.root / LOCK_NAME)
                        except (FileNotFoundError, OSError, ValueError) as exc:
                            raise NexusComputerRuntimeError(
                                "The running Computer Runtime did not accept a graceful restart request and no usable PID was available"
                            ) from exc
                    try:
                        self.restart_request_path.unlink()
                    except FileNotFoundError:
                        pass
                subprocess.Popen(
                    self._runtime_command(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if not self._wait_for_instance_start(timeout=10):
                    raise NexusComputerRuntimeError(
                        "Nexus Computer Runtime did not start from current-user autostart"
                    )
                return
            raise NexusComputerRuntimeError(
                "Nexus Computer Runtime autostart is not installed; run nexus-computer repair"
            )
        if sys.platform == "darwin":
            path = Path.home() / "Library" / "LaunchAgents" / "com.nexus.computer-runtime.plist"
            if not path.is_file():
                raise NexusComputerRuntimeError("Nexus Computer Runtime LaunchAgent is not installed")
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.nexus.computer-runtime"],
                check=True,
                capture_output=True,
                timeout=30,
            )
            if not self._wait_for_instance_start(timeout=10):
                raise NexusComputerRuntimeError(
                    "Nexus Computer Runtime LaunchAgent did not become ready after restart"
                )
            return
        path = Path.home() / ".config" / "systemd" / "user" / "nexus-computer.service"
        if not path.is_file():
            self.repair_user_service()
            return
        subprocess.run(["systemctl", "--user", "restart", "nexus-computer.service"], check=True, timeout=30)
        if not self._wait_for_instance_start(timeout=10):
            raise NexusComputerRuntimeError(
                "Nexus Computer Runtime systemd service did not become ready after restart"
            )

    def uninstall_user_service(self) -> None:
        if os.name == "nt":
            subprocess.run(["schtasks.exe", "/End", "/TN", WINDOWS_TASK_NAME], capture_output=True, timeout=15)
            subprocess.run(["schtasks.exe", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"], capture_output=True, timeout=15)
            try:
                self._remove_windows_run_fallback()
            except OSError:
                pass
            return
        if sys.platform == "darwin":
            path = Path.home() / "Library" / "LaunchAgents" / "com.nexus.computer-runtime.plist"
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True, timeout=15)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        subprocess.run(["systemctl", "--user", "disable", "--now", "nexus-computer.service"], capture_output=True, timeout=30)
        path = Path.home() / ".config" / "systemd" / "user" / "nexus-computer.service"
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=30)


def _xml_escape(value: str) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _run_runtime(root: str) -> int:
    runtime = NexusComputerRuntime(root)
    try:
        asyncio.run(runtime.run_forever())
    except KeyboardInterrupt:
        return 0
    except NexusComputerRuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        runtime.close()
    return 0


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Nexus Computer Runtime")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    subparsers = parser.add_subparsers(dest="command", required=True)
    setup_parser = subparsers.add_parser("setup", help="Pair and install this Computer")
    setup_parser.add_argument("pairing_url")
    setup_parser.add_argument("--name", default="")
    setup_parser.add_argument("--ca-file", default="")
    setup_parser.add_argument("--no-install", action="store_true")
    subparsers.add_parser("run", help="Run in the foreground")
    subparsers.add_parser("status", help="Show local pairing and capability status")
    logs_parser = subparsers.add_parser("logs", help="Show recent Runtime log lines")
    logs_parser.add_argument("--registration", default="")
    subparsers.add_parser("repair", help="Repair autostart for an already paired Computer")
    subparsers.add_parser("restart", help="Restart the installed user Runtime")
    subparsers.add_parser("update", help="Update the SDK and restart the user Runtime")
    unpair_parser = subparsers.add_parser("unpair", help="Revoke Cloud access and remove the local device identity")
    unpair_parser.add_argument("--registration", default="", help="Registration ID shown by nexus-computer status")
    unpair_parser.add_argument("--local-only", action="store_true", help="Remove only local identity after the Computer was already revoked in Cloud")
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    root = Path(arguments.root).expanduser().resolve()
    if arguments.command == "setup":
        runtime = NexusComputerRuntime.setup(
            arguments.pairing_url,
            root=root,
            name=arguments.name,
            ca_file=arguments.ca_file,
            install=not arguments.no_install,
        )
        print(json.dumps({
            "paired": True,
            "registration_id": runtime.config.get("device_id"),
            "device_id": runtime.config.get("device_id"),
            "root": str(root),
        }, indent=2))
        return 0
    if arguments.command == "run":
        return _run_runtime(str(root))
    runtime = NexusComputerRuntime(root)
    if arguments.command == "status":
        capabilities, facts = runtime.detect_capabilities()
        registrations = runtime.registration_statuses()
        print(json.dumps({
            "paired": bool(registrations),
            "registration_count": len(registrations),
            "registrations": registrations,
            "service": runtime.service_status(),
            "capabilities": capabilities,
            "facts": facts,
        }, indent=2))
        return 0
    if arguments.command == "logs":
        selected = (
            [runtime.registration_runtime(arguments.registration)]
            if arguments.registration
            else runtime.registration_runtimes()
        )
        lines: list[str] = []
        for entry, registration in selected:
            try:
                recent = registration.log_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()[-100:]
            except FileNotFoundError:
                recent = []
            lines.extend(f"[{entry['registration_id']}] {line}" for line in recent)
        print("\n".join(lines[-200:]))
        return 0
    if arguments.command == "restart":
        runtime.restart_user_service()
        print("Nexus Computer Runtime restart requested.")
        return 0
    if arguments.command == "repair":
        status = runtime.repair_user_service()
        print(json.dumps({"paired": True, "registration_count": len(runtime._registration_entries()), "service": status}, indent=2))
        return 0
    if arguments.command == "update":
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "nexilume[computer,browser]"],
            check=True,
        )
        runtime.restart_user_service()
        print("Nexus Computer Runtime updated and restart requested.")
        return 0
    if arguments.command == "unpair":
        removed = runtime.remove_registration(
            arguments.registration,
            local_only=arguments.local_only,
        )
        print(f"Nexus Computer Runtime registration {removed} was unpaired and removed locally.")
        return 0
    return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return _main(argv)
    except NexusComputerRuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"Nexus Computer Runtime command failed: {NexusComputerRuntime._service_error(exc)}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
