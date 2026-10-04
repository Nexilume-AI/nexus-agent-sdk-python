"""Mobile SDK parity with the negotiated Cloud/Android action contract."""
import asyncio
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nexus_agent import MOBILE_SCOPES, NexusRunContext
from nexus_agent.reporting import NexusMobilePermissionRequired, NexusMobileUnavailable


class MobileActionsTests(unittest.TestCase):
    def setUp(self):
        self.ctx = NexusRunContext(run_id="action-fixture", mobile_enabled=True,
            mobile_delegate_url="https://cloud.example/mobile/", mobile_delegate_token="private-token",
            mobile_capabilities=MOBILE_SCOPES)
        self.patcher = mock.patch.object(self.ctx, "_mobile_request", return_value={
            "id": "command", "status": "succeeded", "result": {}})
        self.request = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_sync_and_async_new_actions_share_existing_grants(self):
        for api in (self.ctx.mobile, self.ctx.aio.mobile):
            for name, kwargs, action in (("press_home", {}, "press_home"),
                ("press_recents", {}, "press_recents"),
                ("long_press", {"x": 1, "y": 1, "coordinate_space": "pixels"}, "long_press")):
                value = getattr(api, name)(**kwargs)
                if api is self.ctx.aio.mobile:
                    value = asyncio.run(value)
                self.assertEqual(value.status, "succeeded")
                payload = self.request.call_args.kwargs["payload"]
                self.assertEqual(payload["action"], action)
                if action == "long_press":
                    self.assertEqual(payload["arguments"], {"x": 1., "y": 1., "coordinate_space": "pixels", "duration_ms": 750})

    def test_explicit_coordinates_and_legacy_inference(self):
        for value, space, expected in ((.5, "auto", "normalized"), (10, "auto", "pixels"),
            (1, "pixels", "pixels"), (.5, "normalized", "normalized")):
            self.ctx.mobile.tap(x=value, y=value, coordinate_space=space)
            self.assertEqual(self.request.call_args.kwargs["payload"]["arguments"]["coordinate_space"], expected)
        asyncio.run(self.ctx.aio.mobile.swipe(1, 1, 0, 0, coordinate_space="pixels", duration_ms=500))
        self.assertEqual(self.request.call_args.kwargs["payload"]["arguments"]["coordinate_space"], "pixels")

    def test_invalid_coordinates_and_duration_never_dispatch(self):
        for value in (float("nan"), float("inf"), -1, 100001):
            for action in (self.ctx.mobile.tap, self.ctx.mobile.long_press):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    action(x=value, y=0)
        for kwargs in ({"x": 2, "y": 0, "coordinate_space": "normalized"},
                       {"x": .5, "y": .5, "coordinate_space": "other"}):
            with self.assertRaises(ValueError): self.ctx.mobile.tap(**kwargs)
        for duration in (True, "750", 499, 5001, 750.5):
            with self.assertRaises(ValueError): self.ctx.mobile.long_press(x=.5, y=.5, duration_ms=duration)
        for duration in (True, "300", 49, 5001, 300.5):
            with self.assertRaises(ValueError): self.ctx.mobile.swipe(0, 0, 1, 1, duration_ms=duration)
        self.request.assert_not_called()

    def test_new_actions_cannot_bypass_missing_scope(self):
        self.ctx.mobile_capabilities = ("mobile.observe",)
        for name, kwargs in (("press_home", {}), ("press_recents", {}), ("long_press", {"x": .5, "y": .5})):
            with self.assertRaises(NexusMobilePermissionRequired): getattr(self.ctx.mobile, name)(**kwargs)
        self.request.assert_not_called()

    def test_status_actions_and_old_cloud_default(self):
        self.request.return_value = {"available": True, "supported_actions": ["press_home", "long_press"]}
        self.assertEqual(self.ctx.mobile.status().supported_actions, ("press_home", "long_press"))
        self.request.return_value = {"available": True}
        self.assertEqual(self.ctx.mobile.status().supported_actions, ())

    def test_unsupported_and_stale_errors_are_safe_and_do_not_retry(self):
        # Exercise the real HTTP error mapping, not the command stub.
        self.patcher.stop()
        for code, text in (("MOBILE_ACTION_UNSUPPORTED", "update Nexus Mobile"),
                           ("MOBILE_SCREEN_STALE", "fresh screen")):
            body = json.dumps({"error": {"code": code, "message": "private-token private input"}}).encode()
            with mock.patch.object(self.ctx, "_open_cloud", side_effect=HTTPError(
                self.ctx.mobile_delegate_url, 409, "Conflict", {}, io.BytesIO(body))) as opener:
                with self.assertRaises(NexusMobileUnavailable) as error: self.ctx.mobile.press_home()
                self.assertIn(text, str(error.exception))
                self.assertNotIn("private-token", str(error.exception))
                opener.assert_called_once()


if __name__ == "__main__": unittest.main()
