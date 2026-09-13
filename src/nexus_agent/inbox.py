"""Cooperative, Run/turn-scoped input. Receiving never starts a second handler."""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin


class NexusFollowUpUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class RunInput:
    id: str
    content: str
    turn_index: int
    _inbox: Any = field(repr=False, compare=False)
    attachments: tuple[dict, ...] = ()
    files: tuple[dict, ...] = ()

    def acknowledge(self) -> None:
        """Call only after the instruction has been incorporated at a safe point."""
        self._inbox.acknowledge(self.id)

    def reject(self) -> None:
        self._inbox.acknowledge(self.id, status="rejected")


class RunInbox:
    """Bounded memory receiver; Cloud retains messages until explicit acknowledgement.

    Polling, reconnection and receipt happen independently of business execution.
    Call receive_pending at safe points; do not execute tools on the receiver thread.
    Unacknowledged input may be returned again. Applications must use the stable ID
    when applying an instruction with effects that are not naturally idempotent.
    """

    def __init__(self, context):
        self._context = context
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._items: dict[str, RunInput] = {}
        self._acknowledged: set[str] = set()
        self.mode = "none"
        self.attachments_enabled = False
        self.last_error = ""

    def _request(self, action, **values):
        ctx = self._context
        if not ctx.checkpoint_url or not ctx._interaction_token:
            raise NexusFollowUpUnavailable("Cloud Run follow-up context is unavailable")
        return ctx._interaction_request_url(urljoin(ctx.checkpoint_url, "../inbox/"),
            method="POST", payload={"action": action, "turn_index": ctx.turn_index, **values})

    def configure(self, mode: str = "steer_and_queue", *, attachments: bool = False) -> None:
        """Opt in only when the handler reads RunInput.attachments/files.

        Read images with ctx.media.read_image(ref), files with ctx.files.
        References are Run-bound; this method never downloads or executes them.
        """
        if type(attachments) is not bool:
            raise ValueError("attachments must be a boolean")
        if mode not in {"none", "queue", "steer_and_queue"}:
            raise ValueError("follow_up must be none, queue or steer_and_queue")
        if self._stop.is_set():
            raise NexusFollowUpUnavailable("The Run inbox is closed")
        control = self._context.control(refresh=True)
        if control.get("follow_up_protocol") != 1:
            raise NexusFollowUpUnavailable("Cloud does not support the Run inbox protocol; upgrade Cloud")
        result = self._request("configure", mode=mode, **({"attachment_protocol": 1} if attachments else {}))
        if result.get("mode") != mode or result.get("protocol") != 1:
            raise NexusFollowUpUnavailable("Cloud did not accept this follow-up mode")
        if attachments and result.get("attachment_protocol") != 1:
            raise NexusFollowUpUnavailable("Cloud does not support follow-up attachments; upgrade Cloud")
        self.mode = mode
        self.attachments_enabled = attachments
        if mode == "steer_and_queue" and self._thread is None:
            self._thread = threading.Thread(target=self._receive_loop, name="nexus-run-inbox", daemon=True)
            self._thread.start()

    def _poll(self):
        response = self._request("receive")
        with self._lock:
            if self._stop.is_set():
                return
            for row in response.get("items", [])[:20]:
                if row.get("turn_index") != self._context.turn_index or row.get("status") != "received":
                    continue
                identity = str(row["id"])
                images, files = row.get("attachments", []), row.get("files", [])
                if (images or files) and not self.attachments_enabled:
                    raise NexusFollowUpUnavailable("This inbox has not opted in to attachments")
                if not isinstance(images, list) or len(images) > 4 or not isinstance(files, list) or len(files) > 8:
                    raise NexusFollowUpUnavailable("Invalid follow-up attachment response")
                if identity not in self._acknowledged and len(self._items) < 20:
                    self._items[identity] = RunInput(identity, str(row["content"]), row["turn_index"], self, tuple(images), tuple(files))
            self.last_error = ""

    def _receive_loop(self):
        delay = 1.0
        while not self._stop.is_set():
            if self.mode == "steer_and_queue":
                try:
                    self._poll()
                    delay = 1.0
                except Exception:
                    # Never expose arbitrary HTTP bodies, tokens or input in logs.
                    with self._lock:
                        self.last_error = "Run inbox temporarily unavailable; reconnecting"
                    delay = min(delay * 2, 10.0)
            self._stop.wait(delay)

    def receive_pending(self) -> list[RunInput]:
        self._context.raise_if_cancelled()
        if self.mode != "steer_and_queue" or self._stop.is_set():
            raise NexusFollowUpUnavailable("Steer is not enabled for this Run")
        with self._lock:
            if self.last_error and not self._items:
                raise NexusFollowUpUnavailable(self.last_error)
            return list(self._items.values())

    def acknowledge(self, message_id: str, *, status: str = "applied") -> None:
        if status not in {"applied", "rejected"}:
            raise ValueError("Acknowledgement must be applied or rejected")
        self._request("acknowledge", message_id=message_id, status=status)
        with self._lock:
            self._items.pop(message_id, None)
            # Bounded per-turn receipt cache. Older IDs are absent on Cloud once acked.
            if len(self._acknowledged) >= 1024:
                self._acknowledged.clear()
            self._acknowledged.add(message_id)

    def close(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=0.2)
        with self._lock:
            self._items.clear()
            self._acknowledged.clear()


class AsyncRunInput:
    def __init__(self, item: RunInput):
        self._item = item
        self.id, self.content, self.turn_index = item.id, item.content, item.turn_index
        self.attachments, self.files = item.attachments, item.files

    async def acknowledge(self) -> None:
        await asyncio.to_thread(self._item.acknowledge)

    async def reject(self) -> None:
        await asyncio.to_thread(self._item.reject)


class AsyncRunInbox:
    def __init__(self, sync: RunInbox):
        self._sync = sync

    async def configure(self, mode: str = "steer_and_queue", *, attachments: bool = False) -> None:
        await asyncio.to_thread(self._sync.configure, mode, **({"attachments": attachments} if attachments is not False else {}))

    async def receive_pending(self) -> list[AsyncRunInput]:
        return [AsyncRunInput(item) for item in await asyncio.to_thread(self._sync.receive_pending)]
