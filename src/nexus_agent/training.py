"""Opt-in collection of complete client-side Agent trajectories for Data Assets.

The caller supplies a Cloud credential with collection management permission.
Router registration credentials and individual MCP calls are not full episodes.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TrainingTrajectory:
    task_id: str
    session_id: str = ""
    client: str = "nexus-sdk"
    model: str = ""
    agent_version: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)

    def message(self, role: str, content: str, *, trainable: bool = True) -> None:
        if role not in {"system", "user", "assistant"}:
            raise ValueError("Use tool_result() for tool messages.")
        value: dict[str, Any] = {"role": role, "content": content}
        if role == "assistant":
            value["weight"] = int(trainable)
        self.messages.append(value)

    def tool_call(self, call_id: str, name: str, arguments: dict[str, Any], *, trainable: bool = True) -> None:
        self.messages.append({"role": "assistant", "content": "", "weight": int(trainable),
            "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments, ensure_ascii=False, allow_nan=False)}}]})

    def tool_result(self, call_id: str, result: Any) -> None:
        self.messages.append({"role": "tool", "tool_call_id": call_id,
            "content": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, allow_nan=False)})

    def payload(self, execution_status: str = "completed") -> dict[str, Any]:
        if execution_status not in {"completed", "failed", "cancelled"}:
            raise ValueError("Only submit a terminal trajectory.")
        return {"schema": "nexus.trajectory.v1", "messages": self.messages, "tools": self.tools,
            "execution_status": execution_status, "context": {"task_id": self.task_id,
                "session_id": self.session_id, "client": self.client, "model": self.model, "agent_version": self.agent_version}}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Training API redirects are not followed; use the canonical Cloud base URL.")


class TrainingClient:
    def __init__(self, base_url: str, *, token: str, tenant_id: str, dataset_id: str, timeout: float = 30):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use a Cloud base URL without embedded credentials, query or fragment.")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("Training uploads require HTTPS, except for local development.")
        import uuid
        self.dataset_id = str(uuid.UUID(dataset_id))
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": "Bearer " + token, "X-Nexus-Tenant": tenant_id,
            "Content-Type": "application/json", "User-Agent": "Nexus-Training-SDK/1.0"}
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect())

    def submit(self, trajectory: TrainingTrajectory, *, request_key: str, rights_confirmed: bool,
               execution_status: str = "completed") -> dict[str, Any]:
        """Reuse request_key on retry. Different attempts must use different keys."""
        if rights_confirmed is not True:
            raise ValueError("Confirm rights to store and use the trajectory for training.")
        if not request_key or len(request_key) > 128:
            raise ValueError("request_key must contain 1–128 characters.")
        payload = trajectory.payload(execution_status)
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 1024 * 1024:
            raise ValueError("Trajectory exceeds 1 MiB; do not truncate tool interactions silently.")
        req = urllib.request.Request(f"{self.base_url}/api/v1/datasets/{self.dataset_id}/training/samples/",
            data=json.dumps({"trajectory": payload, "request_key": request_key, "rights_confirmed": True},
                ensure_ascii=False, allow_nan=False).encode(), headers=self._headers, method="POST")
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            # Do not echo request bodies, credentials or untrusted response pages.
            raise RuntimeError(f"Training upload failed (HTTP {error.code}). Review the dataset permission and trajectory validation.") from None
        if result.get("ok") is not True:
            raise RuntimeError("Training upload did not return a successful Nexus response.")
        return result["data"]
