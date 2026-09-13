"""Opt-in, bounded AG-UI write-ahead files. Never stores transport credentials.

One writer per Run; mount the directory on persistent storage for crash recovery.
Only an authenticated context for the SAME Run may replay these event IDs.
"""
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid


class EventOutbox:
    def __init__(self, directory, run_id, max_bytes):
        self.directory = Path(directory) / hashlib.sha256(run_id.encode()).hexdigest()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise ValueError("Outbox must not be a symlink")
        self.max_bytes = max_bytes
        self.lock = threading.Lock()

    def records(self):
        with self.lock:
            result = []
            for path in sorted(self.directory.glob("*.json")):
                if path.is_symlink() or path.stat().st_size > self.max_bytes:
                    continue
                try:
                    value = json.loads(path.read_bytes())
                    body = value["body"].encode("utf-8")
                    if not isinstance(value["event_id"], str):
                        continue
                    result.append((path, value["event_id"], body))
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    continue
            return result

    def oldest(self):
        return min(self.directory.glob("*.json"), default=None)

    def append(self, event_id, body):
        payload = json.dumps({"event_id": event_id, "body": body.decode("utf-8")}, ensure_ascii=False).encode("utf-8")
        with self.lock:
            paths = list(self.directory.glob("*.json"))
            if len(paths) >= 10000 or sum(path.stat().st_size for path in paths) + len(payload) > self.max_bytes:
                raise OSError("Outbox capacity reached")
            last = max((int(path.name.split("-")[0]) for path in paths), default=0)
            stem = "{:020d}-{}".format(max(time.time_ns(), last + 1), uuid.uuid4().hex)
            temporary = self.directory / (stem + ".pending")
            target = self.directory / (stem + ".json")
            try:
                descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(str(temporary), str(target))
            finally:
                if temporary.exists():
                    temporary.unlink()
            return target

    def ack(self, path):
        with self.lock:
            if path.parent != self.directory:
                raise ValueError("Invalid outbox record")
            path.unlink(missing_ok=True)
