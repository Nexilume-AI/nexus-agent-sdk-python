"""Nexus-hosted FastMCP Agent that inspects the current caller's Computer.

The Agent deliberately reports directory metadata, never file contents. Nexus
injects a caller- and Run-scoped context into every real ``tools/call``.
"""

from __future__ import annotations

import hashlib
import json

from nexus_agent.fastmcp import CurrentNexusMCP, NexusMCPServer


server = NexusMCPServer("Nexus hosted workspace inspector")


def _entry_payload(entry) -> dict[str, object]:
    return {
        "name": entry.name,
        "path": entry.path,
        "kind": entry.kind,
        "size": entry.size,
        "modified_at": entry.modified_at,
    }


@server.tool
async def inspect_workspace(nexus=CurrentNexusMCP()) -> dict[str, object]:
    """List the caller workspace and create a private Run inventory."""

    if not nexus.run.computer_enabled:
        raise RuntimeError("A caller-owned Nexus Computer is required")

    await nexus.feedback.progress(0, 4, "Inspecting the authorized workspace")
    with nexus.trace.step("inspect-caller-workspace"):
        with nexus.trace.tool("workspace.list", arguments={"path": "."}) as call:
            entries = sorted(
                await nexus.workspace.list("."),
                key=lambda item: (item.kind, item.name.casefold()),
            )
            manifest_entries = [_entry_payload(entry) for entry in entries]
            call.result({"entry_count": len(manifest_entries)})

        await nexus.feedback.progress(1, 4, "Reading the scenario probe")
        with nexus.trace.tool(
            "workspace.read_text",
            arguments={"path": "nexus-scenario.txt"},
        ) as call:
            probe = await nexus.workspace.read_text("nexus-scenario.txt")
            probe_sha256 = hashlib.sha256(probe.encode("utf-8")).hexdigest()
            call.result({"length": len(probe), "sha256": probe_sha256})

        await nexus.feedback.progress(2, 4, "Opening the read-only Agent Terminal")
        with nexus.trace.tool("terminal.run", arguments={"command": "ls -la"}) as call:
            command = await nexus.terminal.run("ls -la -- .", cwd=".", timeout=30)
            call.result({"exit_code": command.exit_code, "duration_ms": command.duration_ms})
        if command.exit_code != 0:
            raise RuntimeError("The caller workspace directory could not be listed")

        manifest = {
            "schema_version": 1,
            "run_id": nexus.run.run_id,
            "workspace_path": ".",
            "entries": manifest_entries,
            "probe": {
                "path": "nexus-scenario.txt",
                "length": len(probe),
                "sha256": probe_sha256,
            },
        }
        canonical_manifest = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        manifest_sha256 = hashlib.sha256(canonical_manifest.encode("utf-8")).hexdigest()

        nexus.memory.add(
            f"Inspected {len(manifest_entries)} entries in the caller workspace.",
            data={
                "entry_count": len(manifest_entries),
                "manifest_sha256": manifest_sha256,
            },
            kind="workflow",
            confidence=1.0,
            sensitivity="internal",
            consent="approved",
            license="internal",
            scope="caller",
        )
        nexus.output.write_text(
            "directory-manifest.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            content_type="application/json",
            producer_step="inspect-caller-workspace",
            license="internal",
        )
        nexus.output.ready(
            {
                "path": "directory-manifest.json",
                "sha256": manifest_sha256,
            }
        )

    await nexus.feedback.progress(4, 4, "Workspace inspection completed")
    await nexus.feedback.log(
        "info",
        f"Inspected {len(manifest_entries)} caller workspace entries",
    )
    return {
        "run_id": nexus.run.run_id,
        "workspace_path": ".",
        "entries": manifest_entries,
        "probe": manifest["probe"],
        "manifest_sha256": manifest_sha256,
        "output_path": "directory-manifest.json",
        "terminal_exit_code": command.exit_code,
    }


if __name__ == "__main__":
    server.run(transport="streamable-http", host="0.0.0.0", port=8000)
