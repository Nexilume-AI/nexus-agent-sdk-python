import pathlib
import sys
import time
import unittest

SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent.models import SseEvent  # noqa: E402
from nexus_agent.resume import StreamResumeError, StreamResumeStore  # noqa: E402


class StreamResumeStoreTest(unittest.TestCase):
    def test_expired_history_and_invalid_cursor_fail_closed(self):
        store = StreamResumeStore(
            max_tasks=2,
            max_events_per_task=2,
            max_history_bytes_per_task=4096,
            retention_seconds=30,
        )

        def produce():
            for index in range(1, 5):
                yield SseEvent(event="progress", data=str(index))
                time.sleep(0.01)

        first = list(store.subscribe("task", "fingerprint", produce))
        self.assertEqual([event.event_id for event in first], ["1", "2", "3", "4"])
        with self.assertRaises(StreamResumeError) as expired:
            store.subscribe(
                "task", "fingerprint", produce, after_event_id=1
            )
        self.assertEqual(expired.exception.status, 410)
        self.assertEqual(expired.exception.code, "EVENT_HISTORY_EXPIRED")
        with self.assertRaises(StreamResumeError) as ahead:
            store.subscribe(
                "task", "fingerprint", produce, after_event_id=9
            )
        self.assertEqual(ahead.exception.code, "INVALID_RESUME_CURSOR")
        with self.assertRaises(StreamResumeError) as missing:
            store.subscribe(
                "missing", "fingerprint", produce, after_event_id=1
            )
        self.assertEqual(missing.exception.status, 404)
        self.assertEqual(missing.exception.code, "STREAM_TASK_NOT_FOUND")
        store.close()


if __name__ == "__main__":
    unittest.main()
