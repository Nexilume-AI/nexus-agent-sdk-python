import ast
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "codex_hook_agent.py"


def load_example():
    spec = importlib.util.spec_from_file_location("codex_hook_example_test", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    with patch.dict("os.environ", {"NEXUS_AGENT_RUNTIME_MODE": "hosted"}):
        spec.loader.exec_module(module)
    return module


def contract(result):
    value = result.to_dict()
    assert value["isError"] is False
    assert json.loads(value["content"][0]["text"]) == value["structuredContent"]
    return value["structuredContent"]


def test_hosted_upload_and_tools():
    source = ast.parse(EXAMPLE.read_text(encoding="utf-8"))
    assert any(isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
               and isinstance(node.value.func, ast.Name) and node.value.func.id == "NexusAgent" for node in source.body)
    module = load_example()
    assert module.agent.runtime_mode == "hosted"
    assert set(module.agent.server.handlers) == {"codex.review_patch", "codex.check_command", "codex.check_completion", "codex.lifecycle"}


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "PermissionRequest", "PreCompact",
                                 "PostCompact", "SubagentStart", "SubagentStop", "Interrupt"])
def test_passive_events_never_override_approvals(event):
    assert contract(load_example().lifecycle({"event": event})) == {}


def test_patch_is_feedback_only_and_does_not_echo_private_content():
    value = contract(load_example().review_patch({"patch": "PRIVATE_TEST_INPUT"}))
    assert value["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "NEXUS_HOOK_PATCH_REVIEW" in str(value)
    assert "PRIVATE_TEST_INPUT" not in str(value)
    assert "decision" not in value


def test_command_denial_and_normal_approvals():
    module = load_example()
    blocked = contract(module.check_command({"command": "echo NEXUS_HOOK_DENY_DEMO"}))
    assert blocked["hookSpecificOutput"]["permissionDecision"] == "deny"
    allowed = contract(module.check_command({"command": "echo safe"}))
    assert "permissionDecision" not in allowed["hookSpecificOutput"]


def test_completion_does_not_create_an_infinite_loop():
    module = load_example()
    assert "NEXUS_HOOK_COMPLETION_CHECKED" in str(contract(module.check_completion({"stop_hook_active": False})))
    assert contract(module.check_completion({"stop_hook_active": True})) == {}


@pytest.mark.parametrize("value", [None, 1, [], "x" * 65537], ids=["null", "number", "array", "oversized"])
def test_invalid_text(value):
    with pytest.raises(ValueError):
        load_example().review_patch({"patch": value})


@pytest.mark.parametrize("value", [None, 0, "false"])
def test_invalid_loop_guard(value):
    with pytest.raises(ValueError):
        load_example().check_completion({"stop_hook_active": value})
