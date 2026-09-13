"""Bounded in-memory event history for resumable Nexus Agent streams."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import json
import threading
import time
from typing import Callable, Deque, Dict, Iterable, Iterator, Optional, Tuple

from .models import SseEvent


class StreamResumeError(Exception):
    """A resume request cannot safely attach to the original task."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class _TaskEntry:
    key: str
    fingerprint: str
    created_at: float
    updated_at: float
    condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.RLock())
    )
    events: Deque[Tuple[int, SseEvent, int]] = field(default_factory=deque)
    history_bytes: int = 0
    next_event_id: int = 1
    completed: bool = False
    producer: Optional[Iterator[SseEvent]] = None


class StreamResumeStore:
    """Run each task once and retain a bounded replay window for subscribers."""

    def __init__(
        self,
        *,
        max_tasks: int = 128,
        max_events_per_task: int = 256,
        max_history_bytes_per_task: int = 262144,
        retention_seconds: float = 300.0,
    ) -> None:
        if not 1 <= max_tasks <= 4096:
            raise ValueError("max_tasks must be between 1 and 4096")
        if not 2 <= max_events_per_task <= 4096:
            raise ValueError("max_events_per_task must be between 2 and 4096")
        if not 1024 <= max_history_bytes_per_task <= 16 * 1024 * 1024:
            raise ValueError(
                "max_history_bytes_per_task must be between 1024 and 16777216"
            )
        if not 1.0 <= retention_seconds <= 86400.0:
            raise ValueError("retention_seconds must be between 1 and 86400")
        self.max_tasks = max_tasks
        self.max_events_per_task = max_events_per_task
        self.max_history_bytes_per_task = max_history_bytes_per_task
        self.retention_seconds = float(retention_seconds)
        self._tasks: "OrderedDict[str, _TaskEntry]" = OrderedDict()
        self._lock = threading.RLock()
        self._closed = False
        self._stats: Dict[str, int] = {
            "tasks_started": 0,
            "tasks_completed": 0,
            "resume_subscriptions": 0,
            "events_recorded": 0,
            "events_replayed": 0,
            "history_evictions": 0,
            "task_evictions": 0,
            "resume_misses": 0,
            "resume_conflicts": 0,
            "history_expired": 0,
        }

    @staticmethod
    def _event_size(event: SseEvent) -> int:
        return (
            len(event.data.encode("utf-8"))
            + len((event.event or "").encode("utf-8"))
            + len((event.event_id or "").encode("ascii", "ignore"))
            + 32
        )

    def _prune_locked(self, now: float) -> None:
        expired = [
            key
            for key, entry in self._tasks.items()
            if entry.completed and now - entry.updated_at >= self.retention_seconds
        ]
        for key in expired:
            self._tasks.pop(key, None)
            self._stats["task_evictions"] += 1

    def _make_room_locked(self, now: float) -> None:
        self._prune_locked(now)
        if len(self._tasks) < self.max_tasks:
            return
        for key, entry in list(self._tasks.items()):
            if entry.completed:
                self._tasks.pop(key, None)
                self._stats["task_evictions"] += 1
                return
        raise StreamResumeError(
            503,
            "RESUME_CAPACITY_EXHAUSTED",
            "all resumable task slots are active",
        )

    def subscribe(
        self,
        key: str,
        fingerprint: str,
        producer_factory: Callable[[], Iterable[SseEvent]],
        *,
        after_event_id: int = 0,
    ) -> Iterator[SseEvent]:
        if after_event_id < 0:
            raise StreamResumeError(
                400, "INVALID_RESUME_CURSOR", "resume event ID must be non-negative"
            )
        now = time.monotonic()
        start_worker = False
        with self._lock:
            if self._closed:
                raise StreamResumeError(
                    503, "RESUME_STORE_CLOSED", "resumable stream store is closed"
                )
            self._prune_locked(now)
            entry = self._tasks.get(key)
            if entry is None:
                if after_event_id > 0:
                    self._stats["resume_misses"] += 1
                    raise StreamResumeError(
                        404,
                        "STREAM_TASK_NOT_FOUND",
                        "the original stream task is not retained on this Agent",
                    )
                self._make_room_locked(now)
                entry = _TaskEntry(key, fingerprint, now, now)
                self._tasks[key] = entry
                self._stats["tasks_started"] += 1
                start_worker = True
            elif entry.fingerprint != fingerprint:
                self._stats["resume_conflicts"] += 1
                raise StreamResumeError(
                    409,
                    "STREAM_TASK_CONFLICT",
                    "task_id is already bound to a different request",
                )
            else:
                self._tasks.move_to_end(key)
                self._stats["resume_subscriptions"] += 1

            with entry.condition:
                earliest = entry.events[0][0] if entry.events else entry.next_event_id
                latest = entry.next_event_id - 1
                if after_event_id < earliest - 1:
                    self._stats["history_expired"] += 1
                    raise StreamResumeError(
                        410,
                        "EVENT_HISTORY_EXPIRED",
                        "the requested event is older than the retained replay window",
                    )
                if after_event_id > latest:
                    raise StreamResumeError(
                        409,
                        "INVALID_RESUME_CURSOR",
                        "resume event ID is ahead of the task history",
                    )

        if start_worker:
            thread = threading.Thread(
                target=self._run_task,
                args=(entry, producer_factory),
                name=f"nexus-stream-{key[:24]}",
                daemon=True,
            )
            thread.start()
        return self._iter_events(entry, after_event_id)

    def _append(self, entry: _TaskEntry, item: SseEvent) -> None:
        with entry.condition:
            event_id = entry.next_event_id
            entry.next_event_id += 1
            event = SseEvent(
                data=item.data,
                event=item.event,
                event_id=str(event_id),
                retry_ms=item.retry_ms,
            )
            size = self._event_size(event)
            entry.events.append((event_id, event, size))
            entry.history_bytes += size
            while (
                len(entry.events) > self.max_events_per_task
                or entry.history_bytes > self.max_history_bytes_per_task
            ):
                _, _, removed = entry.events.popleft()
                entry.history_bytes -= removed
                with self._lock:
                    self._stats["history_evictions"] += 1
            entry.updated_at = time.monotonic()
            with self._lock:
                self._stats["events_recorded"] += 1
            entry.condition.notify_all()

    def _run_task(
        self,
        entry: _TaskEntry,
        producer_factory: Callable[[], Iterable[SseEvent]],
    ) -> None:
        producer: Optional[Iterator[SseEvent]] = None
        try:
            producer = iter(producer_factory())
            entry.producer = producer
            for item in producer:
                if not isinstance(item, SseEvent):
                    raise TypeError("resumable stream producer must yield SseEvent")
                with self._lock:
                    if self._closed:
                        break
                self._append(entry, item)
        except BaseException as exc:
            code = str(getattr(exc, "code", "STREAM_HANDLER_FAILED"))[:64]
            message = str(getattr(exc, "message", "stream handler failed"))[:512]
            self._append(
                entry,
                SseEvent(
                    event="error",
                    data=json.dumps(
                        {"code": code, "message": message},
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                ),
            )
        finally:
            if producer is not None:
                close = getattr(producer, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
            with entry.condition:
                entry.producer = None
                entry.completed = True
                entry.updated_at = time.monotonic()
                entry.condition.notify_all()
            with self._lock:
                self._stats["tasks_completed"] += 1

    def _iter_events(
        self, entry: _TaskEntry, after_event_id: int
    ) -> Iterator[SseEvent]:
        cursor = after_event_id
        while True:
            with entry.condition:
                earliest = entry.events[0][0] if entry.events else entry.next_event_id
                if cursor < earliest - 1:
                    with self._lock:
                        self._stats["history_expired"] += 1
                    raise StreamResumeError(
                        410,
                        "EVENT_HISTORY_EXPIRED",
                        "subscriber fell behind the retained replay window",
                    )
                available = [item for item in entry.events if item[0] > cursor]
                if not available:
                    if entry.completed:
                        return
                    entry.condition.wait(timeout=1.0)
                    continue
            for event_id, event, _ in available:
                if event_id <= cursor:
                    continue
                if after_event_id > 0:
                    with self._lock:
                        self._stats["events_replayed"] += 1
                cursor = event_id
                yield event

    def snapshot(self) -> Dict[str, int]:
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            result = dict(self._stats)
            result["tasks_retained"] = len(self._tasks)
            result["tasks_active"] = sum(
                1 for entry in self._tasks.values() if not entry.completed
            )
            result["retention_seconds"] = int(self.retention_seconds)
            result["max_tasks"] = self.max_tasks
            result["max_events_per_task"] = self.max_events_per_task
            result["max_history_bytes_per_task"] = self.max_history_bytes_per_task
            return result

    def close(self) -> None:
        with self._lock:
            self._closed = True
            entries = list(self._tasks.values())
        for entry in entries:
            with entry.condition:
                producer = entry.producer
                close = getattr(producer, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
                entry.condition.notify_all()


__all__ = ["StreamResumeError", "StreamResumeStore"]
