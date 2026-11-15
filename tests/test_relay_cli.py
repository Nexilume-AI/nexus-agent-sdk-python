import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from nexus_agent import relay_cli
from nexus_agent.relay_models import RelayModelError


class RelayCLITests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.status.return_value = {"device_id": "device", "models": [], "registrations": {}}
        self.client.refresh.return_value = {"device_id": "device", "status": "connected"}
        self.client.revoke.return_value = {"status": "revoked"}
        self.client.register_model.return_value = {"model": "clef", "router_id": "router", "status": "registered"}
        self.client.begin_login.return_value = {"device_id": "device", "device_code": "MUST-NOT-PRINT",
            "access_token": "ALSO-PRIVATE", "user_code": "ABCDE-FGHJK", "verification_uri": "https://cloud.example/approve",
            "expires_in": 600, "interval": 5}
        self.client.poll_login.return_value = True
        self.factory = patch.object(relay_cli, "RelayModelClient", return_value=self.client).start()
        self.addCleanup(patch.stopall)

    def run_cli(self, args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch("sys.stdin", io.StringIO(stdin)):
            code = relay_cli.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_common_flags_before_and_after_subcommands(self):
        code, out, _ = self.run_cli(["--cloud", "https://cloud.example", "--device", "test", "model", "register", "clef", "--json"])
        self.assertEqual(code, 0)
        self.factory.assert_called_once_with("https://cloud.example", "test", timeout=90)
        self.client.register_model.assert_called_once_with("clef")
        self.assertEqual(json.loads(out)["router_id"], "router")
        self.run_cli(["status", "--device", "another", "--cloud", "https://cloud.other"])
        self.factory.assert_called_with("https://cloud.other", "another", timeout=90)

    def test_environment_defaults(self):
        with patch.dict(os.environ, {"NEXUS_RELAY_DEVICE": "persistent", "NEXUS_RELAY_CLOUD": "https://cloud.example"}):
            self.run_cli(["status"])
        self.factory.assert_called_with("https://cloud.example", "persistent", timeout=90)

    def test_new_login_hides_device_secret_and_has_clean_json(self):
        code, out, err = self.run_cli(["login", "--model", "clef", "--endpoint", "https://model.example/v1",
            "--protocol", "system_one", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "connected")
        self.assertIn("ABCDE-FGHJK", err)
        self.assertNotIn("MUST-NOT-PRINT", out+err)
        self.assertNotIn("ALSO-PRIVATE", out+err)

    def test_no_wait_and_resume_do_not_create_another_code(self):
        code, out, _ = self.run_cli(["login", "--model", "clef", "--endpoint", "https://model.example/v1", "--no-wait"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "pending")
        self.client.poll_login.assert_not_called()
        self.assertNotIn("MUST-NOT-PRINT", out)
        self.run_cli(["login", "--resume"])
        self.assertEqual(self.client.begin_login.call_count, 1)
        self.client.status.assert_called_once()

    def test_wait_timeout_and_interrupt(self):
        self.client.poll_login.return_value = False
        with patch.object(relay_cli.time, "monotonic", side_effect=[0, 10]):
            code, out, _ = self.run_cli(["login", "--resume", "--wait-timeout", "1"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["status"], "pending")
        self.client.poll_login.side_effect = KeyboardInterrupt()
        code, _, err = self.run_cli(["login", "--resume"])
        self.assertEqual(code, 130)
        self.assertIn("--resume", err)

    def test_secret_stdin_import_and_no_raw_key_argument(self):
        code, out, err = self.run_cli(["model", "register", "clef", "--endpoint-key-stdin"], stdin="secret-for-test\n")
        self.assertEqual(code, 0)
        self.client.register_model.assert_called_once_with("clef", endpoint_api_key="secret-for-test")
        self.assertNotIn("secret-for-test", out+err)
        code, _, _ = self.run_cli(["model", "register", "clef", "--endpoint-key-stdin"], stdin="bad key")
        self.assertEqual(code, 2)
        self.assertEqual(self.client.register_model.call_count, 1)

    def test_revoke_requires_confirmation_without_terminal(self):
        code, out, _ = self.run_cli(["revoke"])
        self.assertEqual(code, 2)
        self.assertIn("--yes", out)
        self.client.revoke.assert_not_called()
        code, _, _ = self.run_cli(["revoke", "--yes"])
        self.assertEqual(code, 0)
        self.client.revoke.assert_called_once()

    def test_refresh_and_cloud_failure_are_not_replayed(self):
        code, _, _ = self.run_cli(["refresh"])
        self.assertEqual(code, 0)
        self.client.refresh.assert_called_once()
        self.client.register_model.side_effect = RelayModelError("Cloud rejected request", 503)
        code, out, _ = self.run_cli(["model", "register", "clef"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["http_status"], 503)
        self.client.register_model.assert_called_once()

    def test_invalid_declarations_never_contact_cloud(self):
        for args in [["login"], ["login", "--resume", "--model", "clef"]]:
            code, _, _ = self.run_cli(args)
            self.assertEqual(code, 2)
        self.factory.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"models.json"
            path.write_text(json.dumps([{"model":"clef", "key":"do-not-send"}]))
            code, out, err = self.run_cli(["login", "--models-json", str(path)])
            self.assertEqual(code, 2)
            self.assertNotIn("do-not-send", out+err)
            self.factory.assert_not_called()

    def test_multiple_model_declarations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"models.json"
            specs = [{"name": "Model "+name, "model": name, "endpoint":"https://model.example/v1", "protocol":"system_one"} for name in ["a","b"]]
            path.write_text(json.dumps(specs))
            code, _, _ = self.run_cli(["login", "--models-json", str(path), "--no-wait"])
            self.assertEqual(code, 0)
            self.client.begin_login.assert_called_once_with(specs)

    def test_unrecognized_key_argument_is_not_echoed(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            relay_cli.main(["model", "register", "clef", "--key", "accidental-secret"])
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn("accidental-secret", out.getvalue()+err.getvalue())
        self.client.register_model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
