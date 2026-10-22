"""Deterministic, device-free MCP tools for Codex lifecycle hooks.

Upload this file to Nexus Cloud (entrypoint: agent), or run with an enrolled
router. No model key is needed. The demonstration check is NOT a security scan.
Only explicitly supplied arguments are inspected; local paths are never opened.
"""
import json

from nexus_agent import CodexHook, McpToolDescriptor, McpToolResult, NexusAgent


agent = NexusAgent(router="auto", tenant="default", agent_id="codex-hook-demo",
                   cloud_publish=True, cloud_name="Codex Hook demo", advertise_address="auto")


def hook_result(value):
    # Both representations carry the identical Codex output contract. Do not
    # wrap it in {result: ...}, ordinary Chat prose, or a Markdown code fence.
    return McpToolResult(content=[{"type": "text", "text": json.dumps(value)}],
                         structured_content=value)


def bounded_text(data, key, limit=65536):
    value = data.get(key)
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{key} must be a string of at most {limit} characters")
    return value


@agent.capability("codex.review_patch", tool=McpToolDescriptor(
    name="codex_review_patch", description="Codex PostToolUse demo: summarize patch size, without reading files.",
    input_schema={"type": "object", "properties": {"patch": {"type": "string", "maxLength": 65536}},
                  "required": ["patch"], "additionalProperties": False},
    codex_hooks=(CodexHook(event="PostToolUse", matcher="^(apply_patch|mcp__.*__codex_check_command)$",
                          input={"patch": "${tool_input.command}"}),),
))
def review_patch(data):
    patch = bounded_text(data, "patch")
    return hook_result({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": f"NEXUS_HOOK_PATCH_REVIEW: received {len(patch)} characters. "
                             "Demo only: no files opened, no security verdict, no changes made.",
    }})


@agent.capability("codex.check_command", tool=McpToolDescriptor(
    name="codex_check_command", description="Codex PreToolUse demo: deny only the NEXUS_HOOK_DENY_DEMO marker. Not a security filter.",
    input_schema={"type": "object", "properties": {"command": {"type": "string", "maxLength": 65536}},
                  "required": ["command"], "additionalProperties": False},
    codex_hooks=(CodexHook(event="PreToolUse", matcher="^(Bash|exec_command)$",
                          input={"command": "${tool_input.command}"}),),
))
def check_command(data):
    command = bounded_text(data, "command")
    output = {"hookEventName": "PreToolUse"}
    if "NEXUS_HOOK_DENY_DEMO" in command:
        output.update(permissionDecision="deny", permissionDecisionReason="NEXUS_HOOK_DEMO_DENIED: demonstration marker detected.")
    else:
        # No blanket 'allow': preserve Codex's normal approval policy.
        output["additionalContext"] = "NEXUS_HOOK_COMMAND_CHECKED: demonstration check only; normal approvals still apply."
    return hook_result({"hookSpecificOutput": output})


@agent.capability("codex.check_completion", tool=McpToolDescriptor(
    name="codex_check_completion", description="Codex Stop demo: completion feedback with a continuation-loop guard.",
    input_schema={"type": "object", "properties": {"stop_hook_active": {"type": "boolean"}},
                  "required": ["stop_hook_active"], "additionalProperties": False},
    codex_hooks=(CodexHook(event="Stop", input={"stop_hook_active": "${stop_hook_active}"}),),
))
def check_completion(data):
    if type(data.get("stop_hook_active")) is not bool:
        raise ValueError("stop_hook_active must be a boolean")
    if data["stop_hook_active"]:
        return hook_result({})
    return hook_result({"systemMessage": "NEXUS_HOOK_COMPLETION_CHECKED: demo finished without requesting another turn."})


PASSIVE_EVENTS = ("SessionStart", "UserPromptSubmit", "PermissionRequest", "PreCompact",
                  "PostCompact", "SubagentStart", "SubagentStop", "Interrupt")


@agent.capability("codex.lifecycle", tool=McpToolDescriptor(
    name="codex_lifecycle", description="Observe only the lifecycle event name; no prompts, files or approval decisions.",
    input_schema={"type": "object", "properties": {"event": {"type": "string", "enum": list(PASSIVE_EVENTS)}},
                  "required": ["event"], "additionalProperties": False},
    codex_hooks=tuple(CodexHook(event=event, input={"event": "${hook_event_name}"}) for event in PASSIVE_EVENTS),
))
def lifecycle(data):
    if data.get("event") not in PASSIVE_EVENTS:
        raise ValueError("Unsupported lifecycle event")
    # Passive acknowledgement; never bypass permission prompts or request more turns.
    return hook_result({})


if __name__ == "__main__":
    agent.run()
