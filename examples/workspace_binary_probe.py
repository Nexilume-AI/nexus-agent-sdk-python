"""Same binary Workspace workload for OpenWrt and hosted Docker acceptance.

Only the caller's Run-scoped Computer is used. Results contain hashes, not file
contents or host paths. This is a development acceptance example, not a new API.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import signal
import tempfile
import threading
from pathlib import Path

from nexus_agent import McpToolDescriptor, NexusAgent, NexusRunContext

TOOL = McpToolDescriptor(
    name="binary_probe", title="Attached Computer binary verification",
    description="Verify binary and streaming transfers inside the caller Workspace.",
    input_schema={"type": "object", "properties": {"marker": {"type": "string", "maxLength": 64}},
                  "required": ["marker"], "additionalProperties": False},
    task=True,
)


def binary_probe(payload: dict, nexus: NexusRunContext) -> dict:
    marker = str(payload["marker"])
    if not marker or not all(c.isalnum() or c == "-" for c in marker):
        raise ValueError("A safe acceptance marker is required")
    content = bytes(range(256)) * 2050 + marker.encode("ascii")
    expected = hashlib.sha256(content).hexdigest()
    path = "二进制-" + marker + ".bin"
    nexus.plan.set([
        {"id": "bytes", "title": "Binary memory round trip", "status": "running"},
        {"id": "stream", "title": "Chunked file round trip", "status": "pending"},
        {"id": "guard", "title": "Path, integrity and interrupted-write protection", "status": "pending"},
    ])
    written = nexus.workspace.write_bytes(path, content)
    assert written["sha256"] == expected
    assert nexus.workspace.read_bytes(path) == content
    nexus.workspace.write_bytes("empty-" + marker + ".bin", b"")
    assert nexus.workspace.read_bytes("empty-" + marker + ".bin") == b""
    nexus.plan.update("bytes", status="completed")
    nexus.plan.update("stream", status="running")
    with tempfile.TemporaryDirectory(prefix="nexus-binary-agent-") as directory:
        source, target = Path(directory) / "input.bin", Path(directory) / "copy.bin"
        source.write_bytes(content)
        uploaded = nexus.workspace.upload(source, "stream-" + marker + ".bin")
        downloaded = nexus.workspace.download("stream-" + marker + ".bin", target)
        assert uploaded["sha256"] == downloaded["sha256"] == expected
        assert target.read_bytes() == content
        async def check_async():
            await nexus.aio.workspace.write_bytes("async-" + marker + ".bin", content)
            assert await nexus.aio.workspace.read_bytes("async-" + marker + ".bin") == content
            await nexus.aio.workspace.upload(source, "async-stream-" + marker + ".bin")
            async_result = await nexus.aio.workspace.download("async-stream-" + marker + ".bin", target)
            assert async_result["sha256"] == expected and target.read_bytes() == content
        asyncio.run(check_async())
    nexus.plan.update("stream", status="completed")
    nexus.plan.update("guard", status="running")
    rejected = []
    for unsafe in ("../outside.bin", "/outside.bin"):
        try:
            nexus.workspace.write_bytes(unsafe, b"must-not-write")
        except Exception:
            rejected.append("parent" if unsafe.startswith("..") else "absolute")
        else:
            raise AssertionError("An unsafe Workspace path was accepted")
    # Exercise the wire protocol deliberately: a corrupt chunk and incomplete
    # commit must leave the existing destination untouched, then abort the handle.
    opened = nexus.workspace._transfer("write_open", path=path, size_bytes=6,
                                       sha256=hashlib.sha256(b"newbad").hexdigest())
    handle = opened["transfer_id"]
    try:
        try:
            nexus.workspace._transfer("write_chunk", transfer_id=handle, offset=0,
                content_base64=base64.b64encode(b"new").decode("ascii"), sha256="0" * 64)
        except Exception:
            rejected.append("chunk_digest")
        else:
            raise AssertionError("A corrupt chunk was accepted")
        nexus.workspace._transfer("write_chunk", transfer_id=handle, offset=0,
            content_base64=base64.b64encode(b"new").decode("ascii"),
            sha256=hashlib.sha256(b"new").hexdigest())
        try:
            nexus.workspace._transfer("write_commit", transfer_id=handle)
        except Exception:
            rejected.append("incomplete_commit")
        else:
            raise AssertionError("An incomplete transfer was committed")
        assert nexus.workspace.read_bytes(path) == content
    finally:
        try:
            nexus.workspace._transfer("write_abort", transfer_id=handle)
        except Exception:
            # A rejected commit already discards its temporary file and handle.
            # The harness separately checks that no transfer files remain.
            pass
    assert len(rejected) == 4
    nexus.plan.update("guard", status="completed")
    result = {"status": "PASS", "run_id": nexus.run_id, "size_bytes": len(content),
              "sha256": expected, "memory_roundtrip": True, "empty_roundtrip": True,
              "stream_roundtrip": True, "atomic_original_preserved": True,
              "async_roundtrip": True,
              "rejected": rejected, "marker": marker}
    nexus.chat.say("Binary and streaming verification completed; all digests match.")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hosted", action="store_true")
    parser.add_argument("--router", default="auto", help="Trusted LAN router URL, or auto discovery")
    parser.add_argument("--agent-id", default="binary-workspace-probe")
    parser.add_argument("--port", type=int, default=29447)
    parser.add_argument("--ready-file", type=Path)
    args = parser.parse_args()
    agent = NexusAgent(router=args.router, tenant="binary-e2e", agent_id=args.agent_id,
        port=args.port, computer_requirement="required",
        workspace_capabilities=("files.read", "files.write"),
        runtime="hosted" if args.hosted else "openwrt")
    agent.capability("nexus.e2e.binary_probe", tool=TOOL)(binary_probe)
    if args.hosted:
        agent.run(transport="streamable-http", host="0.0.0.0", port=8000)
        return
    handle = agent.start(announce=False)
    def stop(*_):
        threading.Thread(target=handle.close, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        cloud = handle.wait_for_cloud(timeout=180)
        ready = {"agent_id": cloud.agent_id, "runtime_id": cloud.runtime_id,
                 "transport": cloud.transport, "route_id": handle.published[0].route_id}
        if args.ready_file:
            args.ready_file.write_text(json.dumps(ready), encoding="utf-8")
        print(json.dumps(ready), flush=True)
        handle.wait()
    finally:
        handle.close()


if __name__ == "__main__":
    main()
