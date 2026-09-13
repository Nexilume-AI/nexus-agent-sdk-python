from __future__ import annotations

import runpy
from pathlib import Path


# Regression: ISSUE-001 — Edge Mobile Agent diverged from the hosted Agent's Android flow
# Found by /qa on 2026-08-19
# Report: qa-artifacts/openwrt-mobile-private-display/QA-REPORT.md
def test_edge_mobile_example_uses_stable_marker_and_dismisses_ime_before_apply():
    example = (
        Path(__file__).parents[1] / "examples" / "edge_caller_mobile_agent.py"
    )
    source = example.read_text(encoding="utf-8")

    assert 'marker = f"Nexus中文🧪-{ctx.run_id}-{message[:32]}"' in source
    type_index = source.index("ctx.mobile.type_text(marker, timeout=300)")
    dismiss_index = source.index("ctx.mobile.press_back()", type_index)
    settle_index = source.index(
        'ctx.mobile.wait_for_state(text="Apply", timeout=30)', dismiss_index
    )
    apply_index = source.index('ctx.mobile.tap_text("Apply")', settle_index)
    final_capture_index = source.index("final_screen = ctx.mobile.capture_screen()")
    leave_surface_index = source.index("ctx.mobile.press_back()", final_capture_index)

    assert type_index < dismiss_index < settle_index < apply_index
    assert final_capture_index < leave_surface_index


def test_edge_mobile_example_accepts_explicit_test_identity(monkeypatch):
    monkeypatch.setenv("NEXUS_ROUTER_URL", "http://127.0.0.1:7446/")
    example = (
        Path(__file__).parents[1] / "examples" / "edge_caller_mobile_agent.py"
    )
    module = runpy.run_path(str(example))
    assert module["TOOL"].chat is True
    agent = module["build_agent"](
        agent_id="openwrt-mobile-e2e",
        tenant="edge-e2e",
        cloud_name="OpenWrt Mobile E2E",
        listen_host="127.0.0.1",
        advertise_address="192.0.2.10",
    )
    try:
        assert agent.agent_id == "openwrt-mobile-e2e"
        assert agent.tenant == "edge-e2e"
        assert agent.cloud_name == "OpenWrt Mobile E2E"
        assert agent.advertise_address == "192.0.2.10"
    finally:
        agent.server.server_close()
