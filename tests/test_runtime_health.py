import contextlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from nexus_agent import runtime_health as health


class RuntimeHealthTest(unittest.TestCase):
    def result(self, **overrides):
        return {"python": [3, 10], "versions": {"fastmcp": "3.4.8", "mcp": "1.30.0",
                "griffelib": "2.3.0"}, "import_ok": True, "strenum_error": False,
                **overrides}

    def test_identifies_real_strenum_cause_instead_of_missing_server(self):
        data = self.result(import_ok=False, strenum_error=True)
        data["versions"]["griffelib"] = "2.3.1"
        report = health.classify(data)
        self.assertEqual(report["code"], "MCP_DEPENDENCY_INCOMPATIBLE")
        self.assertEqual(report["repair_requirements"], ["griffelib==2.3.0"])

    def test_unknown_import_failure_is_not_automatically_repaired(self):
        report = health.classify(self.result(import_ok=False))
        self.assertEqual(report["code"], "MCP_IMPORT_FAILED")
        self.assertFalse(report["repair_requirements"])

    def test_missing_stack_has_bounded_repair_and_unsupported_python_does_not(self):
        report = health.classify(self.result(versions={}, import_ok=False))
        self.assertEqual(report["code"], "MCP_DEPENDENCIES_MISSING")
        self.assertIn("fastmcp>=3.4.7,<4", report["repair_requirements"])
        self.assertFalse(health.classify(self.result(python=[3, 9]))["repair_requirements"])

    def test_healthy_repair_is_a_noop(self):
        ready = health.classify(self.result())
        with mock.patch.object(health, "diagnose", return_value=ready), \
             mock.patch.object(health.subprocess, "run") as runner:
            self.assertTrue(health.repair(confirmed=True)["ok"])
        runner.assert_not_called()

    def test_unconfirmed_repair_never_installs(self):
        bad = health.classify(self.result(versions={}, import_ok=False))
        with mock.patch.object(health, "diagnose", return_value=bad), \
             mock.patch.object(health.subprocess, "run") as runner:
            result = health.repair(confirmed=False)
        self.assertEqual(result["code"], "SDK_REPAIR_CONFIRMATION_REQUIRED")
        runner.assert_not_called()

    def test_repair_runs_once_then_verifies_in_fresh_process(self):
        bad_data = self.result(import_ok=False, strenum_error=True)
        bad_data["versions"]["griffelib"] = "2.3.1"
        bad, ready = health.classify(bad_data), health.classify(self.result())
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(health, "diagnose", side_effect=[bad, bad, ready]), \
             mock.patch.object(health, "_install_scope", return_value=["--user"]), \
             mock.patch.object(health, "_lock_root", return_value=pathlib.Path(folder)), \
             mock.patch.object(health.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(health.repair(confirmed=True)["ok"])
        args = run.call_args.args[0]
        self.assertIn("griffelib==2.3.0", args)
        self.assertIn("--user", args)
        self.assertNotIn("--break-system-packages", args)
        self.assertNotIn("--trusted-host", args)
        self.assertEqual(run.call_count, 1)

    def test_repair_failure_has_no_secret_output_or_unbounded_retry(self):
        bad = health.classify(self.result(versions={}, import_ok=False))
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(health, "diagnose", return_value=bad), \
             mock.patch.object(health, "_install_scope", return_value=[]), \
             mock.patch.object(health, "_lock_root", return_value=pathlib.Path(folder)), \
             mock.patch.object(health.subprocess, "run", return_value=subprocess.CompletedProcess([], 1,
                               stdout="Bearer secret-token", stderr="private-password")) as run:
            result = health.repair(confirmed=True)
        self.assertEqual(result["code"], "SDK_REPAIR_FAILED")
        self.assertNotIn("secret-token", json.dumps(result))
        self.assertNotIn("private-password", json.dumps(result))
        self.assertEqual(run.call_count, 1)

    def test_repair_lock_does_not_remove_another_repair_lock(self):
        bad = health.classify(self.result(versions={}, import_ok=False))
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(health, "diagnose", return_value=bad), \
             mock.patch.object(health, "_install_scope", return_value=[]), \
             mock.patch.object(health, "_lock_root", return_value=pathlib.Path(folder)):
            (pathlib.Path(folder) / "repair.lock").mkdir()
            self.assertEqual(health.repair(confirmed=True)["code"], "SDK_REPAIR_BUSY")
            self.assertTrue((pathlib.Path(folder) / "repair.lock").exists())

    def test_run_repair_starts_agent_only_once_and_never_replays_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            script = pathlib.Path(folder) / "agent.py"
            script.touch()
            with mock.patch.object(health, "diagnose", return_value={"ok": False}), \
                 mock.patch.object(health, "repair", return_value={"ok": True}) as repair, \
                 mock.patch.object(health.subprocess, "call", return_value=7) as call:
                self.assertEqual(health.main(["run", "--repair", str(script), "--demo"]), 7)
            repair.assert_called_once_with(confirmed=True)
            call.assert_called_once_with([sys.executable, str(script), "--demo"])

    def test_run_does_not_execute_when_environment_still_broken(self):
        with tempfile.TemporaryDirectory() as folder:
            script = pathlib.Path(folder) / "agent.py"
            script.touch()
            with mock.patch.object(health, "diagnose", return_value={"ok": False}), \
                 mock.patch.object(health, "repair", return_value={"ok": False, "code": "SDK_REPAIR_FAILED"}), \
                 mock.patch.object(health.subprocess, "call") as call, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(health.main(["run", "--repair", str(script)]), 2)
            call.assert_not_called()

    def test_existing_ipv6_cli_is_forwarded_unchanged(self):
        with mock.patch("nexus_agent.ipv6_cli.main", return_value=4) as legacy:
            self.assertEqual(health.main(["ipv6", "doctor", "--json"]), 4)
        legacy.assert_called_once_with(["ipv6", "doctor", "--json"])

    def test_probe_timeout_returns_actionable_error(self):
        with mock.patch.object(health.subprocess, "run", side_effect=subprocess.TimeoutExpired("probe", 20)):
            self.assertEqual(health.diagnose()["code"], "SDK_DIAGNOSTIC_FAILED")


if __name__ == "__main__":
    unittest.main()
