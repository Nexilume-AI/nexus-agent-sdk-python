from __future__ import annotations

import runpy
from pathlib import Path


def test_edge_caller_mobile_example_publishes_required_contract(monkeypatch):
    monkeypatch.setenv("NEXUS_ROUTER_URL", "http://127.0.0.1:7446/")
    monkeypatch.setenv("NEXUS_AGENT_ADDRESS", "192.0.2.20")  # No host-network discovery in contract tests.
    example = Path(__file__).parents[1] / "examples" / "edge_caller_mobile_agent.py"
    module = runpy.run_path(str(example))
    agent = module["build_agent"]()
    try:
        registration = agent.registrations()[0].to_dict()
        cloud = registration["cloud"]
        assert cloud["mobile"]["requirement"] == "required"
        assert len(cloud["mobile"]["mobile_capabilities"]) == 8
        assert cloud["tool"]["mobile_scopes"] == cloud["mobile"]["mobile_capabilities"]
        assert cloud["tool"]["task"] is True
        assert cloud["tool"]["interactive"] is True
        assert registration["public_ipv6"] == "auto"
    finally:
        agent.server.server_close()
