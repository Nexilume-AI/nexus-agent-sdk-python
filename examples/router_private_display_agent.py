"""OpenWrt-managed Agent exercising the complete caller Private Display.

The Agent receives no Cloud URL or Cloud credential.  It registers through a
trusted-LAN OpenWrt router, then operates only the caller-authorized Computer
and Workspace exposed in its run-scoped Nexus context.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import signal
import threading
from pathlib import Path
from typing import Any, Mapping

from nexus_agent import McpToolDescriptor, NexusAgent, NexusRunContext


INTENT = "nexus.e2e.private_display"
SENTINEL_PATH = "private-display-input.txt"
RESULT_PATH = "private-display-result.json"
RECOVERY_PROBE_MESSAGE = "Start the Private Display disconnect recovery probe."
RECOVERY_CHECKPOINT_MESSAGE = "Disconnect checkpoint reached. Waiting for the Agent process to be stopped."
DEMO_FRAME = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PRIVATE_DISPLAY_TOOL = McpToolDescriptor(
    name="private_display",
    title="Private Display Computer assistant",
    description="Read the caller workspace, run PowerShell, ask for input, and publish a protected result.",
    input_schema={
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "additionalProperties": False,
    },
    task=True,
    continuable=True,
    chat=True,
    interactive=True,
)


def _message(payload: Any) -> str:
    if isinstance(payload, Mapping):
        return str(payload.get("message") or "").strip()
    return str(payload or "").strip()


def _wait_for_disconnect() -> None:
    """Keep the E2E invocation open until the harness terminates this process."""

    threading.Event().wait()


def private_display(payload: Any, nexus: NexusRunContext) -> dict[str, Any]:
    """Exercise Plan, Chat, Shell, Browser, Terminal, Workspace and Files."""

    prompt = _message(payload)
    nexus.display.title("Private workspace inspection")

    if prompt == RECOVERY_PROBE_MESSAGE:
        nexus.plan.set([
            {
                "id": "disconnect",
                "title": "Wait for the deterministic Agent disconnect",
                "status": "running",
            },
        ])
        nexus.chat.say(RECOVERY_CHECKPOINT_MESSAGE)
        _wait_for_disconnect()
        raise RuntimeError("disconnect recovery probe unexpectedly resumed")

    history = list(nexus.run.messages(limit=100))

    nexus.plan.set([
        {"id": "folder", "title": "Use the caller-selected Workspace folder", "status": "running"},
        {"id": "inspect", "title": "Read the authorized Workspace", "status": "pending"},
        {"id": "powershell", "title": "Run PowerShell on the caller Computer", "status": "pending"},
        {"id": "output", "title": "Write and publish the result file", "status": "pending"},
    ])
    nexus.chat.say("Choose the Workspace folder for this run before I access its files.")
    confirmation = nexus.chat.ask(
        "Select the requested Workspace folder in Computer, then confirm to continue.",
        key="workspace-folder-confirmation",
        kind="confirm",
        choices=[
            {"value": "confirm", "label": "Confirm"},
            {"value": "cancel", "label": "Cancel"},
        ],
        timeout=300,
    )
    if confirmation.value != "confirm":
        nexus.plan.update("folder", status="completed", detail="Declined by caller")
        nexus.chat.say("No Workspace operation was performed because you declined the confirmation.")
        return {"status": "cancelled", "turn_index": getattr(nexus, "turn_index", 1)}
    nexus.plan.update("folder", status="completed", detail="Caller confirmed the selected folder")

    nexus.plan.update("inspect", status="running")
    nexus.chat.say("I am inspecting only the Workspace folder you authorized for this run.")
    entries = nexus.workspace.list(".")
    sentinel = nexus.workspace.read_text(SENTINEL_PATH)
    sentinel_sha256 = hashlib.sha256(sentinel.encode("utf-8")).hexdigest()
    nexus.plan.update("inspect", status="completed", detail=f"Read {SENTINEL_PATH}")

    nexus.plan.update("powershell", status="running")
    # The authorized Windows SSH runner already executes commands inside
    # PowerShell.  Avoid a nested shell so the outer PowerShell cannot expand
    # the version expression before the child process receives it.
    command = "$PSVersionTable.PSVersion.ToString()"
    nexus.shell.write(f"$ {command}", stream="command")
    terminal = nexus.terminal.run(command, cwd=".", timeout=30)
    nexus.shell.write(
        f"PowerShell completed with exit code {terminal.exit_code}",
        stream="system" if terminal.exit_code == 0 else "stderr",
    )
    if terminal.exit_code != 0:
        raise RuntimeError("PowerShell fixture command failed")
    nexus.browser.frame(
        DEMO_FRAME,
        content_type="image/png",
        url="https://private-display.example.invalid/computer-check",
        title="Caller Computer verification",
        text="This protected frame is visible only to the caller who started the Run.",
        width=1,
        height=1,
    )
    nexus.plan.update("powershell", status="completed")

    format_reply = nexus.chat.ask(
        "Choose the result detail level.",
        key="result-detail-level",
        kind="select",
        choices=[
            {"value": "summary", "label": "Summary"},
            {"value": "detailed", "label": "Detailed"},
        ],
        timeout=300,
    )

    nexus.plan.update("output", status="running")
    result = {
        "schema_version": 1,
        "run_id": nexus.run_id,
        "message": prompt,
        "turn_index": getattr(nexus, "turn_index", 1),
        "history_messages": [
            {
                "role": str(item.get("role") or ""),
                "content": str(item.get("content") or ""),
            }
            for item in history
            if isinstance(item, Mapping) and str(item.get("role") or "") == "user"
        ],
        "detail_level": format_reply.value,
        "sentinel_path": SENTINEL_PATH,
        "sentinel": sentinel,
        "sentinel_sha256": sentinel_sha256,
        "workspace_entries": [entry.name for entry in entries],
        "powershell": {
            "exit_code": terminal.exit_code,
            "stdout": terminal.stdout.strip(),
        },
    }
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    file_sha256 = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    nexus.workspace.write_text(RESULT_PATH, rendered)
    nexus.output.write_text(
        RESULT_PATH,
        rendered,
        content_type="application/json",
        producer_step="output",
        license="internal",
    )
    nexus.output.ready({"path": RESULT_PATH, "sha256": file_sha256})
    nexus.plan.update("output", status="completed", detail=f"SHA-256 {file_sha256}")
    nexus.chat.say(
        f"Completed. {RESULT_PATH} is ready to download with SHA-256 {file_sha256}."
    )
    return {**result, "status": "completed", "output_sha256": file_sha256}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--router-url", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--cloud-name")
    parser.add_argument("--listen", required=True)
    parser.add_argument("--advertise-address", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--cert", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--tls-server-name", required=True)
    parser.add_argument("--ca-bundle-id", required=True)
    parser.add_argument("--router-ca", type=Path)
    parser.add_argument("--lease-seconds", type=int, default=120)
    parser.add_argument("--cloud-timeout", type=float, default=180)
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()

    agent = NexusAgent(
        router=args.router_url,
        tenant=args.tenant,
        agent_id=args.agent_id,
        listen_host=args.listen,
        advertise_address=args.advertise_address,
        port=args.port,
        router_ca_file=str(args.router_ca) if args.router_ca else None,
        cert_file=str(args.cert),
        key_file=str(args.key),
        server_tls_name=args.tls_server_name,
        server_ca_bundle_id=args.ca_bundle_id,
        lease_seconds=args.lease_seconds,
        cloud_publish=True,
        cloud_name=args.cloud_name,
        computer_requirement="required",
        workspace_capabilities=(
            "files.list", "files.read", "files.write", "command.execute"
        ),
    )
    agent.capability(INTENT, tool=PRIVATE_DISPLAY_TOOL)(private_display)
    handle_ref = [None]

    def stop(*_args: Any) -> None:
        if handle_ref[0] is not None:
            threading.Thread(target=handle_ref[0].close, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    handle = agent.start(auto_renew=True, renew_fraction=0.2, announce=False)
    handle_ref[0] = handle
    cloud = handle.wait_for_cloud(timeout=args.cloud_timeout, poll_interval=1)
    ready = {
        "status": "ready",
        "origin": agent.origin,
        "route_id": handle.published[0].route_id,
        "cloud_agent_id": cloud.agent_id,
        "cloud_runtime_id": cloud.runtime_id,
        "cloud_transport": cloud.transport,
        "manifest_digest": cloud.manifest_digest,
    }
    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.ready_file.with_suffix(args.ready_file.suffix + ".tmp")
    temporary.write_text(json.dumps(ready, indent=2), encoding="utf-8")
    temporary.replace(args.ready_file)
    print(
        f"NEXUS_PRIVATE_DISPLAY_AGENT_READY origin={agent.origin} cloud_agent={cloud.agent_id}",
        flush=True,
    )
    try:
        handle.wait()
    finally:
        handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
