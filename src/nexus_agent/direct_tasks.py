"""Bounded in-memory broker for router-to-router interactive invokes.

This module deliberately has no persistence or cloud coupling.  A task and all
of its interactions, events and browser assets are protected by one high
entropy bearer token and disappear when the Agent process restarts.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


@dataclass
class _Interaction:
    interaction_id: str
    key: str
    prompt: str
    kind: str
    choices: List[Dict[str, str]]
    expires_at: float
    visibility: str = "public"
    status: str = "pending"
    response: Dict[str, Any] = field(default_factory=dict)
    answered_at: str = ""

    def public(self) -> Dict[str, Any]:
        return {
            "id": self.interaction_id,
            "key": self.key,
            "prompt": self.prompt,
            "kind": self.kind,
            "choices": list(self.choices),
            "status": self.status,
            "response": dict(self.response),
            "answered_at": self.answered_at,
        }


@dataclass
class _Asset:
    asset_id: str
    content: bytes = field(repr=False)
    content_type: str = "application/octet-stream"
    file_name: str = "asset"
    sha256: str = ""
    width: Optional[int] = None
    height: Optional[int] = None


class DirectTask:
    """One local invocation and its private event/interaction state."""

    def __init__(self, *, task_id: Optional[str] = None, ttl_seconds: float = 3600.0) -> None:
        self.task_id = str(task_id or uuid.uuid4())
        self._token = secrets.token_urlsafe(32)
        self.created_at = time.time()
        self.expires_at = self.created_at + ttl_seconds
        self.status = "queued"
        self.result: Any = None
        self.error_code = ""
        self.error_message = ""
        self._events: List[Dict[str, Any]] = []
        self._interactions: Dict[str, _Interaction] = {}
        self._interaction_ids: Dict[str, str] = {}
        self._assets: Dict[str, _Asset] = {}
        self._condition = threading.Condition()
        self._cancelled = False

    @property
    def token(self) -> str:
        return self._token

    def token_matches(self, token: str) -> bool:
        return bool(token) and hmac.compare_digest(self._token, str(token))

    @property
    def cancelled(self) -> bool:
        with self._condition:
            return self._cancelled

    def emit(self, event: str, data: Mapping[str, Any]) -> Dict[str, Any]:
        with self._condition:
            item = {
                "seq": len(self._events) + 1,
                "event": str(event),
                "data": dict(data),
                "created_at": time.time(),
            }
            self._events.append(item)
            self._condition.notify_all()
            return dict(item)

    def start(self) -> None:
        with self._condition:
            if self._cancelled:
                return
            self.status = "running"
        self.emit("task_started", {"task_id": self.task_id, "status": "running"})

    def complete(self, result: Any) -> None:
        with self._condition:
            if self._cancelled:
                return
            self.result = result
            self.status = "completed"
        self.emit("result", {"task_id": self.task_id, "result": result})

    def fail(self, code: str, message: str) -> None:
        with self._condition:
            if self._cancelled:
                return
            self.error_code = str(code or "HANDLER_FAILED")
            self.error_message = str(message or "Agent handler failed")[:512]
            self.status = "failed"
        self.emit(
            "error",
            {
                "task_id": self.task_id,
                "code": self.error_code,
                "message": self.error_message,
            },
        )

    def cancel(self) -> None:
        with self._condition:
            if self.status in TERMINAL_STATUSES:
                return
            self._cancelled = True
            self.status = "cancelled"
            for interaction in self._interactions.values():
                if interaction.status == "pending":
                    interaction.status = "cancelled"
            self._condition.notify_all()
        self.emit("cancelled", {"task_id": self.task_id, "status": "cancelled"})

    def public_status(self) -> Dict[str, Any]:
        with self._condition:
            value: Dict[str, Any] = {
                "task_id": self.task_id,
                "status": self.status,
                "created_at": self.created_at,
                "expires_at": self.expires_at,
                "last_seq": len(self._events),
            }
            if self.status == "completed":
                value["result"] = self.result
            elif self.status == "failed":
                value["error"] = {
                    "code": self.error_code,
                    "message": self.error_message,
                }
            return value

    def events_after(self, after: int) -> List[Dict[str, Any]]:
        with self._condition:
            return [dict(item) for item in self._events if int(item["seq"]) > after]

    def wait_events(self, after: int, timeout: float) -> List[Dict[str, Any]]:
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._condition:
            while len(self._events) <= after and self.status not in TERMINAL_STATUSES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return [dict(item) for item in self._events if int(item["seq"]) > after]

    def interaction_request(
        self,
        path: str,
        *,
        method: str,
        payload: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        clean_path = str(path or "").strip("/")
        normalized_method = method.upper()
        with self._condition:
            if self._cancelled:
                return {"status": "cancelled"}
            if normalized_method == "POST" and not clean_path:
                body = dict(payload or {})
                key = str(body.get("key") or "").strip()
                if not key:
                    raise ValueError("interaction key is required")
                existing_id = self._interaction_ids.get(key)
                if existing_id:
                    return self._interactions[existing_id].public()
                interaction = _Interaction(
                    interaction_id=uuid.uuid4().hex,
                    key=key,
                    prompt=str(body.get("prompt") or "")[:4000],
                    kind=str(body.get("kind") or "text"),
                    choices=[dict(item) for item in body.get("choices") or ()],
                    expires_at=time.time() + min(max(int(body.get("timeout_seconds") or 300), 1), 900),
                    visibility=str(body.get("visibility") or "public"),
                )
                self._interactions[interaction.interaction_id] = interaction
                self._interaction_ids[key] = interaction.interaction_id
                self.status = "input_required"
                created = interaction.public()
            elif normalized_method == "GET" and clean_path:
                interaction = self._interactions.get(clean_path)
                if interaction is None:
                    raise KeyError(clean_path)
                if interaction.status == "pending" and time.time() >= interaction.expires_at:
                    interaction.status = "expired"
                    self.status = "failed"
                    self._condition.notify_all()
                return interaction.public()
            else:
                raise ValueError("unsupported interaction operation")
        self.emit("input_required", created)
        return created

    def reply(self, key: str, value: Any) -> Dict[str, Any]:
        with self._condition:
            interaction_id = self._interaction_ids.get(str(key))
            interaction = self._interactions.get(interaction_id or "")
            if interaction is None or interaction.status != "pending":
                raise KeyError(str(key))
            if time.time() >= interaction.expires_at:
                interaction.status = "expired"
                self._condition.notify_all()
                raise KeyError(str(key))
            if isinstance(value, Mapping):
                response = dict(value)
            else:
                response = {"value": str(value), "text": str(value)}
            interaction.response = response
            interaction.status = "answered"
            interaction.answered_at = str(time.time())
            if self.status == "input_required":
                self.status = "running"
            current = interaction.public()
            self._condition.notify_all()
        self.emit(
            "interaction_answered",
            {"id": interaction.interaction_id, "key": interaction.key, "status": "answered"},
        )
        return current

    def add_asset(
        self,
        *,
        content: bytes,
        content_type: str,
        file_name: str,
        sha256: str,
        width: Optional[int],
        height: Optional[int],
    ) -> Dict[str, Any]:
        asset = _Asset(
            asset_id=uuid.uuid4().hex,
            content=bytes(content),
            content_type=str(content_type),
            file_name=str(file_name or "asset"),
            sha256=str(sha256),
            width=width,
            height=height,
        )
        with self._condition:
            self._assets[asset.asset_id] = asset
        return {
            "id": asset.asset_id,
            "url": f"/agent/v1/tasks/{self.task_id}/assets/{asset.asset_id}",
            "content_type": asset.content_type,
            "file_name": asset.file_name,
            "sha256": asset.sha256,
            "width": width,
            "height": height,
        }

    def asset(self, asset_id: str) -> Optional[_Asset]:
        with self._condition:
            return self._assets.get(str(asset_id))


class DirectTaskStore:
    """Thread-safe bounded store shared by one NexusAgentServer."""

    def __init__(self, *, max_tasks: int = 128, ttl_seconds: float = 3600.0) -> None:
        if max_tasks < 1:
            raise ValueError("direct task max_tasks must be positive")
        if ttl_seconds < 1:
            raise ValueError("direct task ttl_seconds must be at least one second")
        self.max_tasks = int(max_tasks)
        self.ttl_seconds = float(ttl_seconds)
        self._tasks: "OrderedDict[str, DirectTask]" = OrderedDict()
        self._lock = threading.RLock()

    def _prune(self) -> None:
        now = time.time()
        expired = [key for key, task in self._tasks.items() if task.expires_at <= now]
        for key in expired:
            self._tasks.pop(key, None)
        while len(self._tasks) >= self.max_tasks:
            key, task = next(iter(self._tasks.items()))
            if task.status not in TERMINAL_STATUSES:
                task.cancel()
            self._tasks.pop(key, None)

    def create(self) -> DirectTask:
        with self._lock:
            self._prune()
            task = DirectTask(ttl_seconds=self.ttl_seconds)
            self._tasks[task.task_id] = task
            return task

    def get(self, task_id: str, token: str) -> Optional[DirectTask]:
        with self._lock:
            self._prune()
            task = self._tasks.get(str(task_id))
            if task is None or not task.token_matches(token):
                return None
            self._tasks.move_to_end(task.task_id)
            return task

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._prune()
            statuses: Dict[str, int] = {}
            for task in self._tasks.values():
                statuses[task.status] = statuses.get(task.status, 0) + 1
            return {"tasks": len(self._tasks), "statuses": statuses}
