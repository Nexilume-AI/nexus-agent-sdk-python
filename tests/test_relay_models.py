import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from nexus_agent.relay_models import RelayModelClient, RelayModelError


@unittest.skipUnless(os.name == "posix", "private credential storage targets Linux/OpenWrt")
class RelaySDKTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        self.pending = True
        self.spec = {"model": "clef", "name": "Clef", "endpoint": "https://example.com/v1", "protocol": "system_one"}
        self.client = RelayModelClient("https://cloud.example", "test", storage_dir=self.tmp.name, transport=self.http)
        self.generation = 0

    def http(self, path, data, token):
        self.calls.append((path, data, token))
        if path == "start/":
            return {"device_id": "id", "device_code": "device-secret", "user_code": "SHORT-CODE",
                "verification_uri": "https://cloud.example/approve", "expires_in": 600, "interval": 5}
        if path == "token/":
            if "device_code" in data and self.pending:
                raise RelayModelError("authorization_pending", 400)
            self.generation += 1
            return {"device_id": "id", "access_token": "access-%s" % self.generation,
                "refresh_token": "refresh-%s" % self.generation, "expires_in": 600, "models": [self.spec]}
        if path == "models/":
            return {"model": "clef", "status": "registered", "router_id": "router"}
        return {"status": "revoked"}

    def connected(self):
        display = self.client.begin_login([self.spec])
        self.assertNotIn("device_code", display)
        self.pending = False
        self.assertTrue(self.client.poll_login())

    def test_login_restart_auto_refresh_receipt_revoke(self):
        self.connected()
        key = self.client.endpoint_api_key("clef")
        self.assertTrue(key.startswith("nrelay_up_"))
        self.assertEqual(self.client.register_model("clef")["status"], "registered")
        self.assertEqual((Path(self.tmp.name)/"device.json").stat().st_mode & 0o777, 0o600)
        restarted = RelayModelClient("https://cloud.example", "test", storage_dir=self.tmp.name, transport=self.http)
        self.assertEqual(restarted.endpoint_api_key("clef"), key)
        with patch("nexus_agent.relay_models.time.time", return_value=10**12):
            restarted.status()
        self.assertEqual(self.generation, 2)
        restarted.revoke()
        content = (Path(self.tmp.name)/"device.json").read_text()
        self.assertNotIn(key, content)
        self.assertNotIn("access-", content)
        self.assertNotIn("refresh-", content)

    def test_poll_throttling_and_scope(self):
        self.client.begin_login([self.spec])
        self.assertFalse(self.client.poll_login())
        before = len(self.calls)
        self.assertFalse(self.client.poll_login())
        self.assertEqual(len(self.calls), before)
        with self.assertRaises(RelayModelError):
            self.client.endpoint_api_key("foreign")

    def test_public_directory_and_symlink_rejected(self):
        os.chmod(self.tmp.name, 0o755)
        with self.assertRaises(RelayModelError):
            self.client.begin_login([self.spec])
        os.chmod(self.tmp.name, 0o700)
        victim = Path(self.tmp.name)/"victim"
        victim.write_text("unchanged")
        (Path(self.tmp.name)/"device.json").symlink_to(victim)
        with self.assertRaises(OSError):
            self.client.begin_login([self.spec])
        self.assertEqual(victim.read_text(), "unchanged")

    def test_scope_and_key_cannot_silently_change(self):
        self.connected()
        self.client.endpoint_api_key("clef")
        with self.assertRaises(RelayModelError):
            self.client.register_model("clef", endpoint_api_key="replacement")
        with self.assertRaises(RelayModelError):
            self.client.register_model("foreign")
        with self.assertRaises(RelayModelError):
            self.client.begin_login([self.spec])


class URLTests(unittest.TestCase):
    def test_cloud_origin(self):
        for value in ["http://cloud.example", "https://u:p@cloud.example", "https://cloud.example/?k=secret", "https://cloud.example/path"]:
            with self.assertRaises(ValueError):
                RelayModelClient(value, "test")


if __name__ == "__main__":
    unittest.main()
