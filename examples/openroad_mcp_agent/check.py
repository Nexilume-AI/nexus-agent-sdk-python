"""Verify OpenROAD MCP through an independent Nexus SDK client."""

import json

from nexus_agent import NexusAgentClient


client = NexusAgentClient("http://router:7446")
common = {
    "tenant": "eda",
    "source_agent": "agent://eda/openroad-check",
    "target_agent": "agent://eda/openroad",
}
def invoke(name, payload):
    value = client.invoke_intent(name, payload, **common)
    if value.get("nexus_mcp_result_version") == 1:
        result = value["result"]
        assert not result.get("isError"), result
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])
    return value


session = invoke("openroad.create_interactive_session", {})
session_id = session["session_id"]
try:
    result = invoke(
        "openroad.interactive_openroad_query",
        {"command": "help report_wns", "session_id": session_id},
    )
    assert result["error"] is None, result
    assert "report_wns" in result["output"], result
    changed = invoke(
        "openroad.interactive_openroad_exec",
        {"command": "set nexus_check 42", "session_id": session_id},
    )
    assert changed["error"] is None, changed
    print(f"passed: agent://eda/openroad session={session_id} query and exec")
finally:
    invoke(
        "openroad.terminate_interactive_session",
        {"session_id": session_id},
    )
