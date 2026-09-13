"""Docker-hosted Agent that validates the current caller's attached Android device."""

from __future__ import annotations

from nexus_agent import MOBILE_SCOPES
from nexus_agent.fastmcp import CurrentNexusMCP, NexusMCPServer


server = NexusMCPServer("Nexus hosted caller Mobile validator")


@server.tool(
    name="validate_caller_mobile",
    chat=True,
    mobile_scopes=MOBILE_SCOPES,
)
async def validate_caller_mobile(
    message: str = "",
    nexus=CurrentNexusMCP(),
) -> dict[str, object]:
    """Operate only the Mobile device fixed to this caller-private Run."""

    marker = f"Nexus中文🧪-{nexus.run.run_id}-{message[:32]}"
    await nexus.run.aio.display.title("Caller Mobile validation")
    await nexus.plan.set(
        [
            {"id": "status", "title": "Check attached Mobile", "status": "running"},
            {"id": "open", "title": "Open Android test surface", "status": "pending"},
            {"id": "input", "title": "Approve and enter Unicode marker", "status": "pending"},
            {"id": "verify", "title": "Verify Android state", "status": "pending"},
        ]
    )

    status = await nexus.mobile.status()
    if not status.enabled or not status.available:
        raise RuntimeError("The caller Mobile is not attached and online for this Run")
    await nexus.plan.update("status", status="completed", detail=status.status)

    await nexus.plan.update("open", status="running")
    await nexus.mobile.open_app("com.nexus.mobile")
    await nexus.mobile.tap_text("Open Mobile E2E Surface")
    await nexus.mobile.wait_for_state(text="Nexus Mobile E2E", timeout=30)
    observation = await nexus.mobile.observe()
    if "Nexus Mobile E2E" not in str(observation.data):
        raise RuntimeError("The Nexus Mobile E2E surface was not observable")
    initial = await nexus.mobile.capture_screen()
    await nexus.browser.frame(
        initial.content,
        content_type=initial.content_type,
        title="Caller Mobile · before input",
        text="Protected Android viewport for the current Run.",
        width=initial.width,
        height=initial.height,
    )
    await nexus.plan.update("open", status="completed")

    await nexus.plan.update("input", status="running")
    await nexus.mobile.tap_text("E2E message")
    # type_text is deliberately high risk. Nexus must pause this Run until the
    # caller approves the pending interaction in Private Display or MCP.
    await nexus.mobile.type_text(marker, timeout=300)
    # Android keeps the IME above the debug surface after text entry. Dismiss
    # it before locating Apply so the accessibility action targets a visible
    # control instead of a button covered by the keyboard.
    await nexus.mobile.press_back()
    await nexus.mobile.tap_text("Apply")
    await nexus.plan.update("input", status="completed", detail="Caller approved input")

    await nexus.plan.update("verify", status="running")
    await nexus.mobile.wait_for_state(text="Saved: Nexus中文🧪-", timeout=30)
    await nexus.mobile.swipe(0.5, 0.8, 0.5, 0.25, duration_ms=450)
    await nexus.mobile.wait_for_state(text="End of Mobile E2E Surface", timeout=30)
    final = await nexus.mobile.capture_screen()
    await nexus.browser.frame(
        final.content,
        content_type=final.content_type,
        title="Caller Mobile · validation complete",
        text="Final protected Android viewport for the current Run.",
        width=final.width,
        height=final.height,
    )
    await nexus.mobile.press_back()
    await nexus.plan.update("verify", status="completed")
    await nexus.chat.say(
        "Mobile validation completed. The caller-approved Chinese and Emoji marker "
        "was applied and verified on the attached Android device."
    )
    return {
        "run_id": nexus.run.run_id,
        "mobile_status": status.status,
        "verified": True,
    }


if __name__ == "__main__":
    server.run(transport="streamable-http", host="0.0.0.0", port=8000)
