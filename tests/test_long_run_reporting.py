import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nexus_agent import NexusRunContext, NexusReportingConfig
from nexus_agent.reporting import NexusRunCancelled


class Response(io.BytesIO):
    status = 200


class LongRunReportingTests(unittest.TestCase):
    def test_cooperative_cancellation_and_deadline(self):
        context = NexusRunContext(run_id="r1", events_url="http://localhost/events/", token="secret",
            checkpoint_url="http://localhost/runs/r1/checkpoint/", interaction_token="private")
        with mock.patch.object(context, "_interaction_request_url", return_value={
            "managed": True, "lease_active": True, "remaining_seconds": 10, "cancel_requested": True,
        }) as control:
            with self.assertRaises(NexusRunCancelled):
                context.raise_if_cancelled()
            self.assertEqual(control.call_args.args[0], "http://localhost/runs/r1/control/")
        self.assertNotIn("private", repr(context))
        self.assertNotIn("secret", repr(context.report()))

    def test_noop_control_outside_host(self):
        context = NexusRunContext()
        self.assertEqual(context.control(), {"managed": False})
        context.raise_if_cancelled()

    def test_outbox_survives_delivery_failure_and_replays_same_id(self):
        with tempfile.TemporaryDirectory() as directory:
            config = NexusReportingConfig(outbox_directory=directory, max_retries=0)
            first = NexusRunContext(run_id="same-run", events_url="http://localhost/events/", token="secret", config=config)
            with mock.patch.object(first, "_open_cloud", side_effect=OSError("secret must not appear")):
                self.assertTrue(first.emit({"type": "STEP_STARTED", "stepName": "test"}, event_id="stable-id"))
                report = first.flush()
            self.assertEqual(report.buffered, 1)
            self.assertNotIn("secret", repr(report))
            files = list(Path(directory).rglob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertNotIn("secret", files[0].read_text())
            first.close()
            requests = []
            def accept(request, **_):
                requests.append(request)
                return Response(b"{}")
            with mock.patch.object(NexusRunContext, "_open_cloud", side_effect=accept):
                second = NexusRunContext(run_id="same-run", events_url="http://localhost/events/", token="new-secret", config=config)
                report = second.flush()
            self.assertEqual(report.sent, 1)
            self.assertEqual(report.buffered, 0)
            self.assertEqual(requests[0].get_header("X-nexus-agui-event-id"), "stable-id")
            second.close()

    def test_queue_full_is_recoverable_from_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            context = NexusRunContext(run_id="r1", events_url="http://localhost/events/", token="secret",
                config=NexusReportingConfig(outbox_directory=directory))
            with mock.patch("nexus_agent.reporting._dispatcher") as dispatcher:
                dispatcher.return_value.submit.return_value = False
                self.assertFalse(context.emit({"type": "STEP_STARTED", "stepName": "queued"}))
            self.assertEqual(context.report().buffered, 1)
            with mock.patch.object(context, "_open_cloud", return_value=Response(b"{}")):
                self.assertEqual(context.replay_pending(), 1)
                self.assertEqual(context.flush().sent, 1)
            context.close()
