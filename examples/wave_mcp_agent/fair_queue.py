"""Bounded FIFO per caller, round-robin across authenticated callers."""
from collections import deque
from contextlib import contextmanager
import threading
import time


class FairQueue:
    def __init__(self, concurrency=2, per_caller_pending=8, max_pending=32, wait_seconds=10):
        self.condition = threading.Condition()
        self.queues = {}
        self.rotation = deque()
        self.active = set()
        self.concurrency = concurrency
        self.per_caller_pending = per_caller_pending
        self.max_pending = max_pending
        self.wait_seconds = wait_seconds
        self.closed = False

    def _dispatch(self):
        for _ in range(len(self.rotation)):
            if len(self.active) >= self.concurrency:
                break
            caller = self.rotation.popleft()
            pending = self.queues[caller]
            if caller not in self.active:
                pending.popleft().set()
                self.active.add(caller)
            if pending:
                self.rotation.append(caller)
            else:
                del self.queues[caller]
        self.condition.notify_all()

    @contextmanager
    def slot(self, caller):
        ticket = threading.Event()
        deadline = time.monotonic() + self.wait_seconds
        with self.condition:
            if self.closed:
                raise RuntimeError('SERVICE_STOPPING')
            if (len(self.queues.get(caller, ())) >= self.per_caller_pending
                    or sum(map(len, self.queues.values())) >= self.max_pending):
                raise RuntimeError('CALLER_QUEUE_FULL; retry later.')
            if caller not in self.queues:
                self.queues[caller] = deque()
                self.rotation.append(caller)
            self.queues[caller].append(ticket)
            self._dispatch()
            while not ticket.is_set():
                remaining = deadline - time.monotonic()
                if self.closed or remaining <= 0:
                    self.queues[caller].remove(ticket)
                    if not self.queues[caller]:
                        del self.queues[caller]
                        self.rotation.remove(caller)
                    raise RuntimeError('SERVICE_STOPPING' if self.closed else 'CALLER_QUEUE_TIMEOUT; retry later.')
                self.condition.wait(remaining)
        try:
            yield
        finally:
            with self.condition:
                self.active.remove(caller)
                if not self.closed:
                    self._dispatch()
                self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
