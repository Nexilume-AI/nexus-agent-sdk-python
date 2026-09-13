"""OpenWrt Direct IPv6 Agent that validates a caller-owned Android phone.

Run this single file on the edge Agent Serving host. ``router="auto"`` uses
the local OpenWrt Gateway; no Cloud password or device credential is embedded.
The caller grants and attaches a paired phone from Private Display.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from nexus_agent import MOBILE_SCOPES, McpToolDescriptor, NexusAgent, NexusRunContext


TOOL_NAME = "mobile.validate_private_display"
TOOL = McpToolDescriptor(
    name=TOOL_NAME,
    title="Validate caller Mobile",
    description="Operate the caller-authorized Nexus Mobile debug surface.",
    input_schema={
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "additionalProperties": False,
    },
    task=True,
    interactive=True,
    chat=True,
    mobile_scopes=MOBILE_SCOPES,
)


def validate_mobile(payload: Any, ctx: NexusRunContext) -> dict[str, Any]:
    message = str(payload.get("message") or "") if isinstance(payload, Mapping) else str(payload or "")
    marker = f"Nexus中文🧪-{ctx.run_id}-{message[:32]}"
    ctx.display.title("Caller Mobile validation")
    ctx.plan.set([
        {"id": "status", "title": "Check attached Mobile", "status": "running"},
        {"id": "open", "title": "Open debug surface", "status": "pending"},
        {"id": "input", "title": "Confirm and enter Run marker", "status": "pending"},
        {"id": "verify", "title": "Verify Android state", "status": "pending"},
    ])

    status = ctx.mobile.status()
    if not status.enabled or not status.available:
        raise RuntimeError("The caller Mobile is not attached and online for this Run")
    ctx.plan.update("status", status="completed", detail=status.status)

    ctx.plan.update("open", status="running")
    ctx.mobile.open_app("com.nexus.mobile")
    ctx.mobile.tap_text("Open Mobile E2E Surface")
    ctx.mobile.wait_for_state(text="Nexus Mobile E2E", timeout=30)
    observation = ctx.mobile.observe()
    if "Nexus Mobile E2E" not in str(observation.data):
        raise RuntimeError("The Nexus Mobile debug E2E surface was not observable")
    first_screen = ctx.mobile.capture_screen()
    ctx.browser.frame(
        first_screen.content,
        content_type=first_screen.content_type,
        title="Caller Mobile · before input",
        text="Protected screenshot from the current Run.",
        width=first_screen.width,
        height=first_screen.height,
    )
    ctx.plan.update("open", status="completed")

    ctx.plan.update("input", status="running")
    ctx.mobile.tap_text("E2E message")
    # Nexus Cloud classifies type_text as high risk. The command remains
    # pending until this Run's caller approves it in Private Display/MCP.
    ctx.mobile.type_text(marker, timeout=300)
    # The Android IME covers Apply after typing. Dismiss it before resolving
    # the accessibility target, matching the Docker-hosted Agent behavior.
    ctx.mobile.press_back()
    # Back returns before the IME inset animation has necessarily completed.
    # Wait until Apply is visible again so a real device/AVD does not race the
    # layout and report TARGET_NOT_FOUND.
    ctx.mobile.wait_for_state(text="Apply", timeout=30)
    ctx.mobile.tap_text("Apply")
    ctx.plan.update("input", status="completed", detail="Caller approved input")

    ctx.plan.update("verify", status="running")
    # Observation text is privacy-redacted, so wait on the stable visible
    # prefix instead of the numeric Run identifier.
    ctx.mobile.wait_for_state(text="Saved: Nexus中文🧪-", timeout=30)
    ctx.mobile.swipe(0.5, 0.8, 0.5, 0.25, duration_ms=450)
    ctx.mobile.wait_for_state(text="End of Mobile E2E Surface", timeout=30)
    final_observation = ctx.mobile.observe()
    final_screen = ctx.mobile.capture_screen()
    ctx.browser.frame(
        final_screen.content,
        content_type=final_screen.content_type,
        title="Caller Mobile · validation complete",
        text="Final protected screenshot from the current Run.",
        width=final_screen.width,
        height=final_screen.height,
    )
    ctx.mobile.press_back()
    ctx.plan.update("verify", status="completed")
    ctx.chat.say("Mobile validation completed. The caller-approved Unicode marker was applied and verified on the attached phone.")
    return {
        "run_id": ctx.run_id,
        "mobile_status": status.status,
        "verified": True,
        "final_observation_available": bool(final_observation.data),
    }


def build_agent(
    *,
    router: str = "auto",
    tenant: str = "default",
    agent_id: str | None = None,
    cloud_name: str = "Caller Mobile Validator",
    listen_host: str = "auto",
    advertise_address: str = "auto",
    port: int = 0,
) -> NexusAgent:
    agent = NexusAgent(
        router=router,
        tenant=tenant,
        agent_id=agent_id,
        listen_host=listen_host,
        advertise_address=advertise_address,
        port=port,
        cloud_publish=True,
        cloud_name=cloud_name,
        mobile_requirement="required",
        mobile_capabilities=MOBILE_SCOPES,
    )
    agent.capability(TOOL_NAME, public_ipv6=True, tool=TOOL)(validate_mobile)
    return agent


if __name__ == "__main__":
    build_agent(
        router=os.environ.get("NEXUS_ROUTER_URL", "auto"),
        tenant=os.environ.get("NEXUS_AGENT_TENANT", "default"),
        agent_id=os.environ.get("NEXUS_AGENT_ID") or None,
        cloud_name=os.environ.get(
            "NEXUS_AGENT_CLOUD_NAME", "Caller Mobile Validator"
        ),
        listen_host=os.environ.get("NEXUS_AGENT_LISTEN_HOST", "auto"),
        advertise_address=os.environ.get(
            "NEXUS_AGENT_ADVERTISE_ADDRESS", "auto"
        ),
        port=int(os.environ.get("NEXUS_AGENT_PORT", "0")),
    ).run()
