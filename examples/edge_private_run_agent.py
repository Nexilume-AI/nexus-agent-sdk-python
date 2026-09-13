"""Single-file OpenWrt edge Agent for the Nexus Private Display.

Run this file directly on the OpenWrt edge host.  NexusAgent discovers the
local Router and its credential automatically, so no Cloud password, API key,
TLS certificate, or private-key command-line argument is required.

The Workspace and Terminal APIs operate the current caller's authorized
Computer.  They do not expose another caller's files or the developer's host.
Interactive Cloud calls use public IPv6 because Relay v1 is intentionally not
used for chat/elicitation.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import zlib
from typing import Any, Mapping

from nexus_agent import McpToolDescriptor, NexusAgent, NexusRunContext


TOOL_NAME = "edge.operate_workspace"
NOTE_PATH = "nexus-example-note.txt"
OUTPUT_PATH = "workspace-summary.json"
CHAT_EXIT_WORDS = {"done", "exit", "quit", "stop", "完成", "退出", "结束", "停止"}
DEMO_FRAME_WIDTH = 640
DEMO_FRAME_HEIGHT = 360


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _demo_browser_frame(width: int = 640, height: int = 360) -> bytes:
    """Build a dependency-free Nexilume-style Browser preview PNG."""

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            color = (247, 247, 244)
            if y < 52:
                color = (27, 27, 26)
            elif 28 < x < 178 and 80 < y < 330:
                color = (235, 235, 230)
            elif 205 < x < 610 and 82 < y < 142:
                color = (226, 255, 196)
            elif 205 < x < 610 and 164 < y < 310:
                color = (255, 255, 255)
            if (x - 28) ** 2 + (y - 26) ** 2 < 9 ** 2:
                color = (189, 252, 115)
            if 228 < x < 570 and y in {194, 226, 258, 290}:
                color = (199, 199, 193)
            rows.extend(color)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + _png_chunk(b"IEND", b"")


DEMO_FRAME = _demo_browser_frame(DEMO_FRAME_WIDTH, DEMO_FRAME_HEIGHT)

TOOL = McpToolDescriptor(
    name=TOOL_NAME,
    title="Edge workspace assistant",
    description=(
        "Inspect the caller-authorized Workspace, run a shell command, ask "
        "for confirmation, and publish a protected result."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "What the caller wants the Agent to do.",
            }
        },
        "required": ["message"],
        "additionalProperties": False,
    },
    task=True,
    interactive=True,
)


def _message(payload: Any) -> str:
    if isinstance(payload, Mapping):
        return str(payload.get("message") or "").strip()
    return str(payload or "").strip()


def _entry_summary(entries: list[Any]) -> str:
    if not entries:
        return "The Workspace is empty."
    names = [f"- `{entry.name}` ({entry.kind})" for entry in entries[:20]]
    suffix = f"\n- …and {len(entries) - 20} more" if len(entries) > 20 else ""
    return "Workspace entries:\n" + "\n".join(names) + suffix


def _chat_turn(message: str, ctx: NexusRunContext) -> bool:
    """Handle one dependency-free demo turn; return False to end the Run."""

    text = str(message or "").strip()
    normalized = text.casefold()
    if normalized in CHAT_EXIT_WORDS:
        ctx.chat.say("Conversation finished. This Run is now complete.")
        return False
    if normalized in {"list", "ls", "files", "文件", "列出文件"}:
        entries = sorted(
            ctx.workspace.list("."),
            key=lambda entry: (entry.kind, entry.name.casefold()),
        )
        ctx.chat.say(_entry_summary(entries))
        return True
    if normalized in {"pwd", "cwd", "where", "当前目录", "目录"}:
        command = ctx.terminal.run("pwd", cwd=".", timeout=30)
        if command.exit_code == 0:
            location = str(command.stdout or "").strip()
            ctx.chat.say(f"Current Workspace: `{location}`")
        else:
            ctx.chat.say(f"The Computer returned exit code {command.exit_code}.")
        return True
    if normalized in {"browser", "frame", "浏览器", "截图"}:
        ctx.browser.frame(
            DEMO_FRAME,
            content_type="image/png",
            title="Edge chat frame",
            text="A protected Browser frame published by the current Run.",
            width=DEMO_FRAME_WIDTH,
            height=DEMO_FRAME_HEIGHT,
        )
        ctx.chat.say("A protected frame was added to Browser.")
        return True
    if normalized.startswith("write ") or text.startswith("写入 "):
        content = text.split(" ", 1)[1].strip()
        if not content:
            ctx.chat.say("Add text after `write` or `写入`.")
            return True
        ctx.workspace.write_text(NOTE_PATH, content + "\n")
        ctx.chat.say(f"Saved the text to `{NOTE_PATH}`.")
        return True
    ctx.chat.say(
        "I received your message. This dependency-free example supports "
        "`list`, `pwd`, `write <text>`, `browser`, and `done` "
        "(也支持：列出文件、当前目录、写入、浏览器、退出。)"
    )
    return True


def operate_workspace(payload: Any, ctx: NexusRunContext) -> dict[str, Any]:
    """Exercise Plan, Files, Shell, Browser and interactive Chat in one Run."""

    instruction = _message(payload)
    if not ctx.computer.enabled:
        raise RuntimeError(
            "This Agent needs a caller-owned Computer with files and command access"
        )

    # A completed Private Display Run can be entered again with the same
    # ``ctx.run_id``. Nexus has already appended this turn's caller message,
    # so the handler can use the Run-scoped history/checkpoint without a
    # process-local conversation cache or a cross-Run Thread.
    if ctx.is_resumed:
        history = ctx.run.messages()
        ctx.plan.set(
            [{"id": "reply", "title": "Continue current Run", "status": "running"}]
        )
        continuing = _chat_turn(instruction, ctx)
        ctx.plan.update("reply", status="completed")
        return {
            "run_id": ctx.run_id,
            "turn_index": ctx.turn_index,
            "message_count": len(history),
            "continuing": continuing,
        }

    ctx.display.title("Edge workspace operation")
    ctx.plan.set(
        [
            {"id": "inspect", "title": "Inspect workspace", "status": "running"},
            {"id": "shell", "title": "Run shell command", "status": "pending"},
            {"id": "confirm", "title": "Confirm changes", "status": "pending"},
            {"id": "write", "title": "Write result files", "status": "pending"},
            {"id": "chat", "title": "Interactive chat", "status": "pending"},
        ]
    )
    ctx.chat.say("I am inspecting the Computer authorized for this Run.")

    entries = sorted(
        ctx.workspace.list("."),
        key=lambda entry: (entry.kind, entry.name.casefold()),
    )
    visible_entries = [
        {"name": entry.name, "kind": entry.kind, "size": entry.size}
        for entry in entries[:100]
    ]
    ctx.plan.update(
        "inspect", status="completed", detail=f"Found {len(entries)} entries"
    )

    ctx.plan.update("shell", status="running")
    # ``pwd`` is available in POSIX shells and as a PowerShell alias. The
    # directory listing itself comes from the shell-independent Workspace API.
    command_text = "pwd"
    command = ctx.terminal.run(command_text, cwd=".", timeout=30)
    if command.exit_code != 0:
        raise RuntimeError("The workspace listing command failed")
    ctx.plan.update("shell", status="completed")

    # This protected frame proves the Browser surface is wired.  Replace it
    # with bytes from your browser automation when the Agent has one.
    ctx.browser.frame(
        DEMO_FRAME,
        content_type="image/png",
        title="Workspace inspection",
        text=f"The edge Agent inspected {len(entries)} caller-visible entries.",
        width=DEMO_FRAME_WIDTH,
        height=DEMO_FRAME_HEIGHT,
    )

    ctx.plan.update("confirm", status="running")
    reply = ctx.chat.ask(
        f"Create {NOTE_PATH} and {OUTPUT_PATH}?",
        key="create-edge-example-files",
        kind="confirm",
        choices=[
            {"value": "continue", "label": "Create files"},
            {"value": "cancel", "label": "Cancel"},
        ],
        timeout=300,
    )
    ctx.plan.update("confirm", status="completed", detail=reply.value)
    if reply.value != "continue":
        note_sha256 = ""
        output_sha256 = ""
        ctx.plan.update("write", status="completed", detail="Skipped by caller")
        ctx.chat.say("File creation skipped. You can still continue chatting.")
    else:
        ctx.plan.update("write", status="running")
        note = f"Instruction: {instruction}\nEntries inspected: {len(entries)}\n"
        ctx.workspace.write_text(NOTE_PATH, note)
        verified_note = ctx.workspace.read_text(NOTE_PATH)
        note_sha256 = hashlib.sha256(verified_note.encode("utf-8")).hexdigest()
        summary = {
            "schema_version": 1,
            "run_id": ctx.run_id,
            "instruction": instruction,
            "entries": visible_entries,
            "entry_count": len(entries),
            "terminal_exit_code": command.exit_code,
            "note": {"path": NOTE_PATH, "sha256": note_sha256},
        }
        rendered = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        output_sha256 = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        ctx.output.write_text(
            OUTPUT_PATH,
            rendered,
            content_type="application/json",
            producer_step="write",
            license="internal",
        )
        ctx.output.ready({"path": OUTPUT_PATH, "sha256": output_sha256})
        ctx.plan.update("write", status="completed", detail=output_sha256)
        ctx.chat.say(f"Done. {OUTPUT_PATH} is available in Files.")

    ctx.plan.update("chat", status="running")
    ctx.display.title("Edge interactive workspace chat")
    ctx.chat.say(
        "You can keep chatting in this Run. Try `list`, `pwd`, "
        "`write hello`, or `browser`. Send `done` to finish."
    )
    chat_turn = 1
    while True:
        follow_up = ctx.chat.ask(
            "What would you like me to do next?",
            key=f"edge-chat-turn-{chat_turn}",
            kind="text",
            timeout=900,
        )
        if not _chat_turn(follow_up.text or follow_up.value, ctx):
            break
        chat_turn += 1
    ctx.plan.update("chat", status="completed", detail=f"{chat_turn} chat turn(s)")
    ctx.display.title("Edge interactive workspace chat complete")
    return {
        "status": "completed",
        "run_id": ctx.run_id,
        "entry_count": len(entries),
        "note_sha256": note_sha256,
        "output_path": OUTPUT_PATH,
        "chat_turns": chat_turn,
    }


def build_agent() -> NexusAgent:
    """Create the edge Agent using only automatic/local discovery defaults."""

    agent = NexusAgent(
        router="http://192.168.250.1:7446/",
        tenant=os.getenv("NEXUS_TENANT", "default"),
        agent_id=os.getenv("NEXUS_AGENT_ID", "edgate-run-agent"),
        advertise_address="auto",
        cloud_publish=True,
        cloud_name="Edge Private Run Agent",
        computer_requirement="required",
        workspace_capabilities=(
            "files.list",
            "files.read",
            "files.write",
            "command.execute",
        ),
    )
    agent.capability(TOOL_NAME, public_ipv6=True, tool=TOOL)(operate_workspace)
    return agent


if __name__ == "__main__":
    build_agent().run()
