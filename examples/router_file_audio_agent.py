"""OpenWrt Private Display example for uploaded files and voice recordings.

Run this file on an Agent Serving host reachable through an enrolled OpenWrt
Router. ``router="auto"`` discovers the local Router and uses its existing
Cloud enrollment, so this source file needs no Cloud password or API key.

The example streams every private input through the Run file data plane,
checks its SHA-256, and publishes a JSON manifest as a downloadable Run output.
It deliberately does not transcribe audio or expose file contents in Chat.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from nexus_agent import McpToolDescriptor, NexusAgent, NexusRunContext


TOOL_NAME = "edge.inspect_uploads"
DEFAULT_AGENT_ID = "router-file-audio-agent"
FILE_REFERENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "file_id": {"type": "string"},
        "name": {"type": "string"},
        "content_type": {"type": "string"},
        "size_bytes": {"type": "integer"},
        "sha256": {"type": "string"},
        "source_kind": {"type": "string"},
    },
    "required": ["file_id"],
    "additionalProperties": False,
}

TOOL = McpToolDescriptor(
    name=TOOL_NAME,
    title="File and voice inspector",
    description=(
        "Verify caller-uploaded files and voice recordings, then publish a "
        "private manifest without exposing their contents."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "What the caller wants checked.",
            },
            "files": {
                "type": "array",
                "maxItems": 8,
                "items": FILE_REFERENCE_SCHEMA,
            },
            "audio": {
                "type": "array",
                "maxItems": 8,
                "items": FILE_REFERENCE_SCHEMA,
            },
        },
        "required": ["message"],
        "additionalProperties": False,
    },
    input_modalities=("text", "audio"),
    chat=True,
    task=True,
    continuable=True,
    interactive=True,
)


def _digest(ctx: NexusRunContext, reference: Mapping[str, Any]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in ctx.files.iter_bytes(reference):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _safe_manifest_entry(
    ctx: NexusRunContext,
    reference: Mapping[str, Any],
    *,
    kind: str,
) -> dict[str, Any]:
    actual_sha256, actual_size = _digest(ctx, reference)
    declared_sha256 = str(reference.get("sha256") or "")
    declared_size = int(reference.get("size_bytes") or 0)
    return {
        "kind": kind,
        "name": str(reference.get("name") or "unnamed"),
        "content_type": str(reference.get("content_type") or "application/octet-stream"),
        "size_bytes": actual_size,
        "sha256": actual_sha256,
        "integrity": (
            (not declared_sha256 or declared_sha256 == actual_sha256)
            and (not declared_size or declared_size == actual_size)
        ),
    }


def inspect_uploads(payload: Mapping[str, Any], ctx: NexusRunContext) -> dict[str, Any]:
    # The payload contains the same safe references used by ctx.input. Reading
    # ctx.input makes the code work identically for Docker and OpenWrt IPv6.
    audio_ids = {
        str(item.get("file_id") or "")
        for item in ctx.input.audio
        if isinstance(item, Mapping)
    }
    files = [
        item
        for item in ctx.input.files
        if str(item.get("file_id") or "") not in audio_ids
    ]
    audio = list(ctx.input.audio)

    ctx.display.title("File and voice inspection")
    ctx.plan.set(
        [
            {"id": "files", "title": "Verify uploaded files", "status": "running"},
            {"id": "audio", "title": "Verify voice recordings", "status": "pending"},
            {"id": "manifest", "title": "Publish private manifest", "status": "pending"},
        ]
    )

    manifest_files = [
        _safe_manifest_entry(ctx, reference, kind="file") for reference in files
    ]
    ctx.plan.update("files", status="completed", detail=f"{len(files)} file(s)")
    ctx.plan.update("audio", status="running")
    manifest_audio = [
        _safe_manifest_entry(ctx, reference, kind="audio") for reference in audio
    ]
    ctx.plan.update(
        "audio", status="completed", detail=f"{len(audio)} recording(s)"
    )

    manifest = {
        "schema_version": 1,
        "run_id": ctx.run_id,
        "request": str(payload.get("message") or ""),
        "files": manifest_files,
        "audio": manifest_audio,
    }
    ctx.plan.update("manifest", status="running")
    with tempfile.TemporaryDirectory(prefix="nexus-input-example-") as directory:
        path = Path(directory) / "input-manifest.json"
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        output = ctx.output.upload_file(path, content_type="application/json")
    ctx.plan.update("manifest", status="completed")

    ctx.chat.say(
        "## Inputs verified\n\n"
        f"- Files: **{len(manifest_files)}**\n"
        f"- Voice recordings: **{len(manifest_audio)}**\n"
        "- Contents remain private to this Run.\n\n"
        "Download `input-manifest.json` from **Run context → Files**."
    )
    return {
        "message": "File and voice inputs verified.",
        "file_count": len(manifest_files),
        "audio_count": len(manifest_audio),
        "output": output,
    }


def build_agent() -> NexusAgent:
    agent_id = os.environ.get("NEXUS_AGENT_ID", DEFAULT_AGENT_ID).strip()
    agent = NexusAgent(
        router=os.environ.get("NEXUS_ROUTER_URL", "auto"),
        tenant=os.environ.get("NEXUS_AGENT_TENANT", "default"),
        agent_id=agent_id or DEFAULT_AGENT_ID,
        listen_host=os.environ.get("NEXUS_AGENT_LISTEN_HOST", "auto"),
        advertise_address=os.environ.get("NEXUS_AGENT_ADVERTISE_ADDRESS", "auto"),
        port=int(os.environ.get("NEXUS_AGENT_PORT", "0")),
        cloud_publish=True,
        cloud_name=os.environ.get(
            "NEXUS_AGENT_CLOUD_NAME", "File and Voice Inspector"
        ),
    )
    agent.capability(TOOL_NAME, public_ipv6=True, tool=TOOL)(inspect_uploads)
    return agent


if __name__ == "__main__":
    build_agent().run()
