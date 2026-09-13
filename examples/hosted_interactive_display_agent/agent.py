"""Docker-hosted Nexus Agent exercising Plan, Shell, Browser and Chat."""

from __future__ import annotations

import base64

from nexus_agent.fastmcp import CurrentNexusMCP, NexusMCPServer


server = NexusMCPServer("Nexus interactive Display Agent")

# A tiny valid PNG keeps the example deterministic and below the 2 MiB limit.
DEMO_FRAME = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@server.tool(task=True, continuable=True, demo=True)
async def interactive_display(nexus=CurrentNexusMCP()) -> dict[str, object]:
    """Publish all Display surfaces and wait for one caller confirmation."""

    checkpoint = await nexus.checkpoint.load()
    if checkpoint and checkpoint.stage == "finalizing":
        selection = str(checkpoint.data.get("selection") or "cancel")
        await nexus.plan.set([
            {"id": "inspect", "title": "Inspect request", "status": "completed"},
            {"id": "confirm", "title": "Confirm operation", "status": "completed"},
            {"id": "finish", "title": "Finish response", "status": "completed"},
        ])
        return {
            "status": "cancelled" if selection == "cancel" else "completed",
            "run_id": nexus.run.run_id,
            "selection": selection,
        }
    if checkpoint and checkpoint.stage == "confirmed":
        selection = str(checkpoint.data.get("selection") or "cancel")
        await nexus.plan.set([
            {"id": "inspect", "title": "Inspect request", "status": "completed"},
            {"id": "confirm", "title": "Confirm operation", "status": "completed"},
            {"id": "finish", "title": "Finish response", "status": "running"},
        ])
    else:
        await nexus.plan.set([
            {"id": "inspect", "title": "Inspect request", "status": "running"},
            {"id": "confirm", "title": "Confirm operation", "status": "pending"},
            {"id": "finish", "title": "Finish response", "status": "pending"},
        ])
        await nexus.shell.write("$ python interactive_display.py", stream="command")
        await nexus.shell.write("Preparing a protected Browser frame", stream="stdout")
        await nexus.browser.frame(
            DEMO_FRAME,
            content_type="image/png",
            url="https://demo.invalid/nexus-display",
            title="Nexus interactive Display",
            text="The Browser frame belongs only to this Run.",
            width=1,
            height=1,
        )
        await nexus.plan.update("inspect", status="completed")
        await nexus.plan.update("confirm", status="running")
        reply = await nexus.chat.ask(
            "Continue and finish this private Run?",
            key="confirm-finish",
            choices=[
                {"value": "continue", "label": "Continue"},
                {"value": "cancel", "label": "Cancel"},
            ],
            timeout=300,
        )
        selection = reply.value
        await nexus.checkpoint.save(stage="confirmed", data={"selection": selection})
        await nexus.plan.update("confirm", status="completed")
        await nexus.plan.update("finish", status="running")

    if selection == "cancel":
        await nexus.checkpoint.save(stage="finalizing", data={"selection": selection})
        await nexus.chat.say("The caller cancelled the operation.")
        await nexus.plan.update("finish", status="completed", detail="Cancelled by caller")
        return {"status": "cancelled", "run_id": nexus.run.run_id}
    await nexus.checkpoint.save(stage="finalizing", data={"selection": selection})
    await nexus.shell.write("Confirmation received", stream="system")
    await nexus.chat.say("Result saved successfully.")
    await nexus.plan.update("finish", status="completed")
    return {"status": "completed", "run_id": nexus.run.run_id, "selection": selection}


if __name__ == "__main__":
    server.run(transport="streamable-http", host="0.0.0.0", port=8000)
