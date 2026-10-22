import pytest
from nexus_agent import CodexHook, McpToolDescriptor
from nexus_agent.codex_hooks import CODEX_HOOK_EVENTS


@pytest.mark.parametrize("event", CODEX_HOOK_EVENTS)
def test_all_mcp_events_serialize(event):
    descriptor = McpToolDescriptor(name="hook", codex_hooks=(
        CodexHook(event=event, input={"event": "${hook_event_name}"}),))
    hook = descriptor.to_dict(intent="test", intent_version=1)["codex_hooks"][0]
    assert hook["event"] == event
    assert hook["timeout"] == (1 if event == "Interrupt" else 30)


@pytest.mark.parametrize("kwargs", [
    {"event": "SessionEnd"}, {"event": "Fake"},
    {"event": "Interrupt", "timeout": 4}, {"event": "Stop", "matcher": "*"},
    {"event": "PreToolUse", "timeout": True}, {"event": "Stop", "input": {"x": float("nan")}},
])
def test_invalid_declarations_rejected(kwargs):
    with pytest.raises(ValueError):
        CodexHook(**kwargs)


def test_required_arguments_and_duplicates():
    hook = CodexHook(event="Stop", input={})
    with pytest.raises(ValueError, match="required"):
        McpToolDescriptor(name="hook", input_schema={"required": ["stop_hook_active"]}, codex_hooks=(hook,))
    with pytest.raises(ValueError, match="Duplicate"):
        McpToolDescriptor(name="hook", codex_hooks=(hook, hook))


def test_fastmcp_metadata():
    from nexus_agent.fastmcp import NexusMCPServer
    server = NexusMCPServer("hooks")
    @server.tool(codex_hooks=(CodexHook(event="UserPromptSubmit", input={"event": "${hook_event_name}"}),))
    def hook(event: str) -> dict:
        return {}
    assert server.nexus_tool_policy["hook"]["codex_hooks"][0]["event"] == "UserPromptSubmit"
