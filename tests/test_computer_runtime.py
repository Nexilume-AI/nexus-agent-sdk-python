from __future__ import annotations

import asyncio
import codecs
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

SDK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent.computer_runtime import (  # noqa: E402
    NexusComputerRuntime,
    NexusComputerRuntimeError,
    RuntimeOperationError,
    _RuntimeInstanceLock,
    _TerminalProcess,
    main,
)
from nexus_agent.browser import (  # noqa: E402
    NexusBrowserStaleObservation,
    NexusBrowserUnavailable,
)


def _test_ca_pem() -> bytes:
    key = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Nexus Pairing Test CA")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, algorithm=None)
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


def _pairing_url(*, ca_pem: bytes) -> str:
    import base64
    import hashlib
    from urllib.parse import urlencode

    encoded = base64.urlsafe_b64encode(ca_pem).decode("ascii").rstrip("=")
    return "nexus-computer://pair?" + urlencode(
        {
            "cloud": "https://cloud.example.test",
            "code": "single-use",
            "trust": "pinned-pem",
            "ca": encoded,
            "ca_sha256": hashlib.sha256(ca_pem).hexdigest(),
        }
    )


class NexusComputerRuntimeTests(unittest.TestCase):
    def runtime(self, root: Path) -> NexusComputerRuntime:
        root.mkdir(parents=True, exist_ok=True)
        key_path = root / "device.key"
        if not key_path.is_file():
            key_path.write_bytes(
                Ed25519PrivateKey.generate().private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
        runtime = NexusComputerRuntime(root)
        workspace = root / "workspace"
        runtime.config = {
            "cloud_origin": "https://cloud.example.test",
            "device_id": "00000000-0000-0000-0000-000000000001",
            "connection_id": "00000000-0000-0000-0000-000000000002",
            "workspace_root": str(workspace),
        }
        return runtime

    def test_runtime_pairing_directory_allows_only_one_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.lock"
            first = _RuntimeInstanceLock(path)
            second = _RuntimeInstanceLock(path)
            first.acquire()
            try:
                with self.assertRaisesRegex(NexusComputerRuntimeError, "already running"):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()

    def test_workspace_operations_are_bounded_to_paired_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = self.runtime(root)
            workspace = Path(runtime.config["workspace_root"])

            written = runtime.dispatch(
                "workspace.write_file",
                "files.write",
                {
                    "workspace_root": str(workspace / "run-1"),
                    "path": "results/value.txt",
                    "content": "caller-a",
                },
            )
            self.assertEqual(written["content"], "caller-a")
            read = runtime.dispatch(
                "workspace.read_file",
                "files.read",
                {
                    "workspace_root": str(workspace / "run-1"),
                    "path": "results/value.txt",
                    "max_bytes": 1024,
                },
            )
            self.assertEqual(read["content"], "caller-a")

            with self.assertRaisesRegex(RuntimeOperationError, "outside"):
                runtime.dispatch(
                    "workspace.read_file",
                    "files.read",
                    {
                        "workspace_root": str(workspace),
                        "path": "../../outside.txt",
                    },
                )
            with self.assertRaisesRegex(RuntimeOperationError, "scope"):
                runtime.dispatch(
                    "workspace.read_file",
                    "files.write",
                    {"workspace_root": str(workspace), "path": "value.txt"},
                )

    def test_setup_persists_only_private_device_identity_locally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "computer"
            response = {
                "device_id": "00000000-0000-0000-0000-000000000001",
                "connection_id": "00000000-0000-0000-0000-000000000002",
                "workspace_root": str(Path(directory) / "workspace"),
            }
            with mock.patch(
                "nexus_agent.computer_runtime._request_json", return_value=response
            ) as request_json, mock.patch.object(
                NexusComputerRuntime, "detect_capabilities", return_value=({"workspace.v1": 1}, {})
            ):
                runtime = NexusComputerRuntime.setup(
                    "nexus-computer://pair?cloud=https%3A%2F%2Fcloud.example.test&code=single-use",
                    root=root,
                    install=False,
                )

            enrollment = request_json.call_args.kwargs["payload"]
            self.assertEqual(enrollment["pairing_code"], "single-use")
            self.assertIn("BEGIN PUBLIC KEY", enrollment["public_key_pem"])
            self.assertNotIn("PRIVATE KEY", json.dumps(enrollment))
            self.assertIn("BEGIN PRIVATE KEY", runtime.key_path.read_text(encoding="ascii"))
            self.assertNotIn("single-use", runtime.config_path.read_text(encoding="utf-8"))
            if os.name != "nt":
                self.assertEqual(runtime.root.stat().st_mode & 0o777, 0o700)
                self.assertEqual(runtime.key_path.stat().st_mode & 0o777, 0o600)

    def test_setup_adds_a_second_workspace_without_overwriting_the_first_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "computer"
            responses = [
                {
                    "device_id": "00000000-0000-0000-0000-000000000011",
                    "connection_id": "00000000-0000-0000-0000-000000000012",
                    "workspace_root": str(Path(directory) / "workspace-a"),
                },
                {
                    "device_id": "00000000-0000-0000-0000-000000000021",
                    "connection_id": "00000000-0000-0000-0000-000000000022",
                    "workspace_root": str(Path(directory) / "workspace-b"),
                },
            ]
            with mock.patch(
                "nexus_agent.computer_runtime._request_json", side_effect=responses
            ), mock.patch.object(
                NexusComputerRuntime, "detect_capabilities", return_value=({"workspace.v1": 1}, {})
            ):
                first = NexusComputerRuntime.setup(
                    "nexus-computer://pair?cloud=https%3A%2F%2Fcloud-a.example.test&code=space-a",
                    root=root,
                    install=False,
                )
                first_key = first.key_path.read_bytes()
                second = NexusComputerRuntime.setup(
                    "nexus-computer://pair?cloud=https%3A%2F%2Fcloud-b.example.test&code=space-b",
                    root=root,
                    install=False,
                )

            supervisor = NexusComputerRuntime(root)
            registrations = supervisor.registration_statuses()
            self.assertEqual([item["device_id"] for item in registrations], [
                responses[0]["device_id"],
                responses[1]["device_id"],
            ])
            self.assertEqual(first.key_path.read_bytes(), first_key)
            self.assertNotEqual(second.key_path.read_bytes(), first_key)
            self.assertNotEqual(first.root, second.root)
            registry_text = (root / "registrations.json").read_text(encoding="utf-8")
            self.assertNotIn("space-a", registry_text)
            self.assertNotIn("space-b", registry_text)

            (root / "registrations.json").write_text("{corrupt", encoding="utf-8")
            recovered = NexusComputerRuntime(root).registration_statuses()
            self.assertEqual(len(recovered), 2)

    def test_broken_registration_identity_is_reported_without_hiding_healthy_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = self.runtime(root)
            runtime._save_config()
            runtime._save_registration_entries([
                {
                    "registration_id": runtime.config["device_id"],
                    "device_id": runtime.config["device_id"],
                    "connection_id": runtime.config["connection_id"],
                    "cloud_origin": runtime.config["cloud_origin"],
                    "path": ".",
                },
                {
                    "registration_id": "broken-space",
                    "device_id": "broken-space",
                    "connection_id": "broken-connection",
                    "cloud_origin": "https://broken.example.test",
                    "path": "registrations/broken-space",
                },
            ])

            statuses = NexusComputerRuntime(root).registration_statuses()
            self.assertEqual(len(statuses), 2)
            broken = next(item for item in statuses if item["registration_id"] == "broken-space")
            self.assertEqual(broken["state"], "identity_error")
            self.assertIn("missing or unreadable", broken["last_error"])

    def test_multi_workspace_connections_recover_independently(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "computer"
                first = self.runtime(root)
                first._save_config()
                second = self.runtime(root / "registrations" / "space-b")
                second.config = {
                    **second.config,
                    "device_id": "00000000-0000-0000-0000-000000000021",
                    "connection_id": "00000000-0000-0000-0000-000000000022",
                    "cloud_origin": "https://cloud-b.example.test",
                }
                second._save_config()
                first._save_registration_entries([
                    {
                        "registration_id": first.config["device_id"],
                        "device_id": first.config["device_id"],
                        "connection_id": first.config["connection_id"],
                        "cloud_origin": first.config["cloud_origin"],
                        "path": ".",
                    },
                    {
                        "registration_id": second.config["device_id"],
                        "device_id": second.config["device_id"],
                        "connection_id": second.config["connection_id"],
                        "cloud_origin": second.config["cloud_origin"],
                        "path": "registrations/space-b",
                    },
                ])
                connected = asyncio.Event()

                async def connection(runtime):
                    if runtime.config["device_id"] == first.config["device_id"]:
                        raise NexusComputerRuntimeError("space A unavailable")
                    runtime._write_runtime_state(
                        state="connected",
                        retry_seconds=0,
                        last_error="",
                        connected=True,
                    )
                    connected.set()
                    await asyncio.Future()

                supervisor = NexusComputerRuntime(root)
                with mock.patch.object(
                    NexusComputerRuntime,
                    "_run_connection",
                    autospec=True,
                    side_effect=connection,
                ):
                    task = asyncio.create_task(supervisor.run_forever())
                    await asyncio.wait_for(connected.wait(), timeout=2)
                    deadline = time.monotonic() + 2
                    statuses = supervisor.registration_statuses()
                    while time.monotonic() < deadline and not any(
                        item["state"] == "reconnecting" for item in statuses
                    ):
                        await asyncio.sleep(0.05)
                        statuses = supervisor.registration_statuses()
                    self.assertEqual(
                        {item["state"] for item in statuses},
                        {"connected", "reconnecting"},
                    )
                    supervisor._stop.set()
                    await asyncio.wait_for(task, timeout=3)

        asyncio.run(scenario())

    def test_multi_workspace_unpair_requires_an_explicit_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = self.runtime(root)
            runtime._save_config()
            second = self.runtime(root / "registrations" / "space-b")
            second.config["device_id"] = "00000000-0000-0000-0000-000000000021"
            second._save_config()
            runtime._save_registration_entries([
                {
                    "registration_id": runtime.config["device_id"],
                    "device_id": runtime.config["device_id"],
                    "connection_id": runtime.config["connection_id"],
                    "cloud_origin": runtime.config["cloud_origin"],
                    "path": ".",
                },
                {
                    "registration_id": second.config["device_id"],
                    "device_id": second.config["device_id"],
                    "connection_id": second.config["connection_id"],
                    "cloud_origin": second.config["cloud_origin"],
                    "path": "registrations/space-b",
                },
            ])

            with self.assertRaisesRegex(NexusComputerRuntimeError, "--registration"):
                runtime.registration_runtime()

    def test_setup_preserves_pairing_and_reports_repair_when_service_install_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "computer"
            response = {
                "device_id": "00000000-0000-0000-0000-000000000001",
                "connection_id": "00000000-0000-0000-0000-000000000002",
                "workspace_root": str(Path(directory) / "workspace"),
            }
            with mock.patch(
                "nexus_agent.computer_runtime._request_json", return_value=response
            ), mock.patch.object(
                NexusComputerRuntime, "detect_capabilities", return_value=({"workspace.v1": 1}, {})
            ), mock.patch.object(
                NexusComputerRuntime,
                "install_user_service",
                side_effect=NexusComputerRuntimeError("Access is denied"),
            ):
                with self.assertRaisesRegex(
                    NexusComputerRuntimeError,
                    "pairing succeeded.*nexus-computer repair",
                ):
                    NexusComputerRuntime.setup(
                        "nexus-computer://pair?cloud=https%3A%2F%2Fcloud.example.test&code=single-use",
                        root=root,
                    )

            persisted = json.loads((root / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["device_id"], response["device_id"])
            self.assertEqual(persisted["service_install_error"], "Access is denied")
            self.assertNotIn("single-use", json.dumps(persisted))

    def test_windows_service_install_falls_back_when_task_scheduler_denies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(Path(directory))
            task_failure = mock.Mock(returncode=1, stdout="", stderr="Access is denied.")
            with mock.patch("nexus_agent.computer_runtime.os.name", "nt"), mock.patch(
                "nexus_agent.computer_runtime.shutil.which",
                return_value=r"C:\\Tools\\nexus-computer.exe",
            ), mock.patch(
                "nexus_agent.computer_runtime.subprocess.run", return_value=task_failure
            ) as run, mock.patch.object(
                runtime, "_install_windows_run_fallback"
            ) as fallback, mock.patch(
                "nexus_agent.computer_runtime.subprocess.Popen"
            ) as popen:
                backend = runtime.install_user_service()

            self.assertEqual(backend, "registry_run")
            self.assertIn("/Create", run.call_args.args[0])
            fallback.assert_called_once()
            popen.assert_called_once()

    def test_repair_waits_for_runtime_start_before_reporting_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(Path(directory))
            runtime._save_config()
            expected = {
                "installed": True,
                "running": True,
                "backend": "registry_run",
                "state": "running",
                "code": "",
                "message": "connected",
                "last_install_error": "",
            }
            stopped = {**expected, "running": False, "state": "stopped"}
            with mock.patch.object(
                runtime, "install_user_service", return_value="registry_run"
            ), mock.patch.object(
                runtime, "_wait_for_instance_start", return_value=True
            ) as wait_for_start, mock.patch.object(
                runtime, "service_status", side_effect=[stopped, expected]
            ):
                status = runtime.repair_user_service()

            wait_for_start.assert_called_once()
            self.assertEqual(status, expected)
            persisted = json.loads(runtime.config_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["service_backend"], "registry_run")
            self.assertNotIn("service_install_error", persisted)

    def test_repair_does_not_reinstall_an_already_running_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(Path(directory))
            runtime._save_config()
            running = {
                "installed": True,
                "running": True,
                "backend": "scheduled_task",
                "state": "running",
                "code": "",
                "message": "connected",
                "last_install_error": "",
                "registrations": [],
            }
            with mock.patch.object(runtime, "service_status", return_value=running), mock.patch.object(
                runtime, "install_user_service"
            ) as install:
                status = runtime.repair_user_service()

            self.assertEqual(status, running)
            install.assert_not_called()

    def test_windows_status_prefers_the_configured_registry_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(Path(directory))
            runtime.config["service_backend"] = "registry_run"
            task_query = mock.Mock(returncode=0)
            with mock.patch("nexus_agent.computer_runtime.os.name", "nt"), mock.patch.object(
                runtime, "registration_statuses", return_value=[{"state": "connected"}]
            ), mock.patch.object(
                runtime, "_instance_running", return_value=True
            ), mock.patch.object(
                runtime, "_windows_run_fallback_installed", return_value=True
            ), mock.patch(
                "nexus_agent.computer_runtime.subprocess.run", return_value=task_query
            ):
                status = runtime.service_status()

            self.assertEqual(status["backend"], "registry_run")
            self.assertTrue(status["installed"])
            self.assertTrue(status["running"])

    def test_windows_registry_restart_force_stops_a_legacy_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(Path(directory))
            runtime.pid_path.write_text("1234", encoding="ascii")
            status = {
                "installed": True,
                "running": True,
                "backend": "registry_run",
            }
            forced_stop = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch("nexus_agent.computer_runtime.os.name", "nt"), mock.patch.object(
                runtime, "service_status", return_value=status
            ), mock.patch.object(
                runtime, "_wait_for_instance_start", return_value=True
            ), mock.patch(
                "nexus_agent.computer_runtime._wait_for_instance_lock_release",
                side_effect=[NexusComputerRuntimeError("still running"), None],
            ), mock.patch(
                "nexus_agent.computer_runtime.subprocess.run", return_value=forced_stop
            ) as run, mock.patch(
                "nexus_agent.computer_runtime.subprocess.Popen"
            ) as popen:
                runtime.restart_user_service()

            run.assert_called_once_with(
                ["taskkill.exe", "/PID", "1234", "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            popen.assert_called_once()
            self.assertFalse(runtime.restart_request_path.exists())

    def test_windows_registry_restart_recovers_when_pid_metadata_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(Path(directory))
            status = {
                "installed": True,
                "running": True,
                "backend": "registry_run",
            }
            with mock.patch("nexus_agent.computer_runtime.os.name", "nt"), mock.patch.object(
                runtime, "service_status", return_value=status
            ), mock.patch.object(
                runtime, "_wait_for_instance_start", return_value=True
            ) as wait_for_start, mock.patch(
                "nexus_agent.computer_runtime.subprocess.Popen"
            ) as popen:
                runtime.restart_user_service()

            popen.assert_called_once()
            wait_for_start.assert_called_once()
            self.assertFalse(runtime.restart_request_path.exists())

    def test_running_runtime_consumes_graceful_restart_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(Path(directory))
            runtime.restart_request_path.write_text("restart", encoding="ascii")

            self.assertTrue(runtime._consume_restart_request())
            self.assertFalse(runtime.restart_request_path.exists())
            self.assertFalse(runtime._consume_restart_request())

    def test_cli_reports_repair_error_without_python_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            NexusComputerRuntime,
            "repair_user_service",
            side_effect=NexusComputerRuntimeError("Windows current-user autostart was denied"),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = main(["--root", directory, "repair"])

        self.assertEqual(exit_code, 2)
        self.assertIn("current-user autostart was denied", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_setup_automatically_uses_and_persists_pairing_cloud_ca(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "computer"
            ca_pem = _test_ca_pem()
            response = {
                "device_id": "00000000-0000-0000-0000-000000000001",
                "connection_id": "00000000-0000-0000-0000-000000000002",
                "workspace_root": str(Path(directory) / "workspace"),
            }
            with mock.patch(
                "nexus_agent.computer_runtime._request_json", return_value=response
            ) as request_json, mock.patch.object(
                NexusComputerRuntime, "detect_capabilities", return_value=({"workspace.v1": 1}, {})
            ):
                runtime = NexusComputerRuntime.setup(
                    _pairing_url(ca_pem=ca_pem), root=root, install=False
                )

            self.assertEqual(request_json.call_args.kwargs["ca_pem"], ca_pem)
            stored_ca = root / "cloud-ca.pem"
            self.assertEqual(stored_ca.read_bytes(), ca_pem)
            self.assertEqual(runtime.config["ca_file"], str(stored_ca.resolve()))
            self.assertNotIn("single-use", runtime.config_path.read_text(encoding="utf-8"))

    def test_setup_rejects_tampered_pairing_cloud_ca_before_enrollment(self) -> None:
        import hashlib

        ca_pem = _test_ca_pem()
        url = _pairing_url(ca_pem=ca_pem).replace(
            hashlib.sha256(ca_pem).hexdigest(), "0" * 64
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "nexus_agent.computer_runtime._request_json"
        ) as request_json:
            with self.assertRaisesRegex(NexusComputerRuntimeError, "digest does not match"):
                NexusComputerRuntime.setup(url, root=Path(directory), install=False)
        request_json.assert_not_called()

    def test_same_command_id_reuses_inflight_execution(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                runtime = self.runtime(Path(directory))
                calls = 0
                lock = threading.Lock()

                def dispatch(*_args, **_kwargs):
                    nonlocal calls
                    with lock:
                        calls += 1
                    time.sleep(0.1)
                    return {"marker": "only-once"}

                runtime.dispatch = dispatch  # type: ignore[method-assign]

                class Socket:
                    def __init__(self):
                        self.frames = []

                    async def send(self, value):
                        self.frames.append(json.loads(value))

                first = Socket()
                second = Socket()
                command = {
                    "command_id": "command-1",
                    "deadline": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
                    "operation": "workspace.test",
                    "required_scope": "connection.list",
                    "payload": {},
                }
                await asyncio.gather(
                    runtime._handle_command(first, command),
                    runtime._handle_command(second, command),
                )
                self.assertEqual(calls, 1)
                self.assertEqual(first.frames[-1]["result"]["marker"], "only-once")
                self.assertEqual(second.frames[-1]["result"]["marker"], "only-once")

        asyncio.run(scenario())

    def test_completed_result_survives_restart_encrypted_and_is_not_executed_twice(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first = self.runtime(root)
                calls = 0

                def dispatch(*_args, **_kwargs):
                    nonlocal calls
                    calls += 1
                    return {"marker": "private-completed-value"}

                first.dispatch = dispatch  # type: ignore[method-assign]

                class Socket:
                    def __init__(self):
                        self.frames = []

                    async def send(self, value):
                        self.frames.append(json.loads(value))

                command = {
                    "command_id": "command-after-restart",
                    "deadline": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
                    "operation": "workspace.test",
                    "required_scope": "connection.list",
                    "payload": {},
                }
                before = Socket()
                await first._handle_command(before, command)
                self.assertEqual(calls, 1)
                journal = first.journal_path.read_text(encoding="utf-8")
                self.assertNotIn("private-completed-value", journal)
                self.assertNotIn("command-after-restart", journal)

                restarted = self.runtime(root)
                restarted.dispatch = dispatch  # type: ignore[method-assign]
                after = Socket()
                await restarted._handle_command(after, command)
                self.assertEqual(calls, 1)
                self.assertEqual(after.frames[-1]["result"]["marker"], "private-completed-value")

        asyncio.run(scenario())

    def test_command_execute_observes_runtime_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(Path(directory))
            cancel = threading.Event()
            cancel.set()
            with self.assertRaisesRegex(RuntimeOperationError, "canceled"):
                runtime.dispatch(
                    "command.execute",
                    "command.execute",
                    {
                        "workspace_root": runtime.config["workspace_root"],
                        "cwd": ".",
                        "command": "echo should-not-complete",
                    },
                    cancel,
                )

    def test_command_execute_returns_process_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(Path(directory))
            result = runtime.dispatch(
                "command.execute",
                "command.execute",
                {
                    "workspace_root": runtime.config["workspace_root"],
                    "cwd": ".",
                    "command": "echo nexus-runtime-ok",
                    "output_max_bytes": 4096,
                },
            )
            self.assertEqual(result["exit_code"], 0)
            self.assertIn("nexus-runtime-ok", result["stdout"])

    def test_terminal_stream_uses_live_websocket_frames_without_command_rpc(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                runtime = self.runtime(Path(directory))

                class Socket:
                    def __init__(self):
                        self.frames = []

                    async def send(self, value):
                        self.frames.append(json.loads(value))

                socket = Socket()
                stream_id = "stream-live-terminal"
                await runtime._handle_terminal_stream_frame(socket, {
                    "type": "terminal_stream_open",
                    "stream_id": stream_id,
                    "shell": "auto",
                    "workspace_root": runtime.config["workspace_root"],
                })
                self.assertEqual(socket.frames[-1]["type"], "terminal_stream_opened")

                await runtime._handle_terminal_stream_frame(socket, {
                    "type": "terminal_stream_input",
                    "stream_id": stream_id,
                    "data": "echo nexus-stream-ok\n",
                })
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if any(
                        frame.get("type") == "terminal_stream_output"
                        and "nexus-stream-ok" in str(frame.get("data") or "")
                        for frame in socket.frames
                    ):
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(any(
                    frame.get("type") == "terminal_stream_output"
                    and "nexus-stream-ok" in str(frame.get("data") or "")
                    for frame in socket.frames
                ))

                await runtime._handle_terminal_stream_frame(socket, {
                    "type": "terminal_stream_close",
                    "stream_id": stream_id,
                })
                self.assertEqual(socket.frames[-1]["type"], "terminal_stream_closed")
                self.assertNotIn(stream_id, runtime._terminals)

        asyncio.run(scenario())

    def test_terminal_output_preserves_utf8_sequence_split_across_reads(self) -> None:
        terminal = object.__new__(_TerminalProcess)
        terminal.output = queue.Queue()
        terminal._output_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        encoded = "中文".encode("utf-8")

        terminal.output.put(encoded[:2])
        self.assertEqual(terminal.read(0.01), "")
        terminal.output.put(encoded[2:4])
        self.assertEqual(terminal.read(0.01), "中")
        terminal.output.put(encoded[4:])
        self.assertEqual(terminal.read(0.01), "文")

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell encoding regression")
    def test_windows_terminal_round_trips_chinese_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            terminal = _TerminalProcess(shell="powershell", cwd=Path(directory))
            try:
                terminal.write("Write-Output '中文测试'\r\n")
                output = ""
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and "中文测试" not in output:
                    output += terminal.read(0.1)
                self.assertIn("中文测试", output)
                self.assertNotIn("�", output)
            finally:
                terminal.close()

    def test_runtime_advertises_direct_terminal_stream_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(Path(directory))
            capabilities, _facts = runtime.detect_capabilities()
        self.assertEqual(capabilities["terminal.stream.v1"], 1)

    def test_browser_unavailable_is_not_reported_as_an_action_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(Path(directory))
            session = mock.Mock()
            session.open.side_effect = NexusBrowserUnavailable("Chrome browser worker could not start")
            runtime._browsers["browser-session"] = session

            with self.assertRaises(RuntimeOperationError) as raised:
                runtime.dispatch(
                    "browser.open",
                    "browser.control",
                    {
                        "browser_session_id": "browser-session",
                        "url": "https://example.test",
                    },
                )

        self.assertEqual(raised.exception.code, "BROWSER_UNAVAILABLE")

    def test_stale_browser_observation_keeps_its_recoverable_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(Path(directory))
            session = mock.Mock()
            session.perform.side_effect = NexusBrowserStaleObservation(
                "Browser observation is stale; observe the page again"
            )
            runtime._browsers["browser-session"] = session

            with self.assertRaises(RuntimeOperationError) as raised:
                runtime.dispatch(
                    "browser.action",
                    "browser.control",
                    {
                        "browser_session_id": "browser-session",
                        "action": {
                            "kind": "locator_click",
                            "parameters": {"target": "e1", "is_ref": True},
                            "expected_revision": 1,
                        },
                    },
                )

        self.assertEqual(raised.exception.code, "BROWSER_STALE_OBSERVATION")


if __name__ == "__main__":
    unittest.main()
