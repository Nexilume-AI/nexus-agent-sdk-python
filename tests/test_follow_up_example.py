import asyncio
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "router_follow_up_agent.py"


def load_example():
    spec = importlib.util.spec_from_file_location("follow_up_example_test", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    with patch.dict("os.environ", {"NEXUS_AGENT_RUNTIME_MODE": "hosted"}):
        spec.loader.exec_module(module)
    return module


def test_upload_has_top_level_agent_and_hosted_import_needs_no_router():
    source = ast.parse(EXAMPLE.read_text(encoding="utf-8"))
    assert any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "agent" for target in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name) and node.value.func.id == "NexusAgent"
        for node in source.body
    )
    module = load_example()
    assert module.agent.runtime_mode == "hosted"
    assert "demo.follow_up" in module.agent.server.handlers


def test_example_acknowledges_only_received_guidance_and_reports_exact_content():
    module = load_example()
    instruction = SimpleNamespace(id="edited-id", content="edited guidance", acknowledge=AsyncMock())
    ctx = SimpleNamespace(turn_index=3, aio=SimpleNamespace(
        raise_if_cancelled=AsyncMock(),
        inbox=SimpleNamespace(receive_pending=AsyncMock(side_effect=[[], [instruction], []])),
        chat=SimpleNamespace(say=AsyncMock()),
    ))
    with patch.object(module.asyncio, "sleep", new_callable=AsyncMock):
        result = asyncio.run(module.follow_up_chat({"message": "original task", "steps": 3}, ctx))
    assert result == {"message": "edited guidance", "steps": 3, "turn_index": 3,
                      "applied": [{"id": "edited-id", "content": "edited guidance"}]}
    instruction.acknowledge.assert_awaited_once()
    assert [call.args[0] for call in ctx.aio.chat.say.await_args_list] == [
        "Step 1/3: original task", "Step 2/3: edited guidance", "Step 3/3: edited guidance",
    ]


@pytest.mark.parametrize("steps", [True, 0, 121, "3", 1.5])
def test_example_rejects_invalid_step_counts(steps):
    with pytest.raises(ValueError, match="steps"):
        asyncio.run(load_example().follow_up_chat({"message": "task", "steps": steps}, None))
