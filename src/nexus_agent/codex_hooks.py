"""Publisher-declared Codex MCP hooks. No commands, servers or credentials."""
import json

CODEX_HOOK_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
    "PostToolUse", "PreCompact", "PostCompact", "SubagentStart", "SubagentStop",
    "Stop", "Interrupt",
)
MATCHER_EVENTS = frozenset(CODEX_HOOK_EVENTS) - {"UserPromptSubmit", "Stop", "Interrupt"}


def normalize_codex_hooks(value, schema):
    if not isinstance(value, (list, tuple)) or len(value) > 16:
        raise ValueError("codex_hooks must contain at most 16 declarations")
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {"event", "input", "matcher", "timeout"}:
            raise ValueError("Invalid Codex Hook declaration fields")
        event = item.get("event")
        if not isinstance(event, str) or event not in CODEX_HOOK_EVENTS:
            raise ValueError("Unsupported Codex MCP Hook event (SessionEnd is command-only)")
        payload = item.get("input")
        if not isinstance(payload, dict):
            raise ValueError("Codex Hook input must be a JSON object")
        try:
            encoded = json.dumps(payload, allow_nan=False)
            if len(encoded.encode("utf-8")) > 8192:
                raise ValueError("Codex Hook input exceeds 8192 bytes")
            payload = json.loads(encoded)
        except (TypeError, RecursionError) as exc:
            raise ValueError("Codex Hook input must be bounded JSON") from exc
        if any(key not in payload for key in schema.get("required", [])):
            raise ValueError("Codex Hook input is missing required tool arguments")
        timeout = item.get("timeout", 1 if event == "Interrupt" else 30)
        if type(timeout) is not int or not 1 <= timeout <= (3 if event == "Interrupt" else 600):
            raise ValueError("Invalid Codex Hook timeout")
        matcher = item.get("matcher")
        if matcher is not None and (
            event not in MATCHER_EVENTS or not isinstance(matcher, str) or len(matcher) > 256
        ):
            raise ValueError("Invalid Codex Hook matcher for this event")
        identity = (event, matcher or "*")
        if identity in seen:
            raise ValueError("Duplicate Codex Hook event and matcher")
        seen.add(identity)
        result.append({"event": event, "input": payload, "timeout": timeout,
                       **({"matcher": matcher} if matcher is not None else {})})
    return result

