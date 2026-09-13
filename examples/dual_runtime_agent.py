"""One file: python dual_runtime_agent.py on the edge, or Upload Python in Cloud.

No Router/Cloud credentials belong in this file. Additional external model
credentials, if needed, belong in deployment secrets and are read inside tools.
"""
from nexus_agent import NexusAgent, NexusRunContext, McpToolDescriptor

agent = NexusAgent(cloud_name="Dual Runtime Assistant")


@agent.capability("assist", tool=McpToolDescriptor(
    name="assist", title="Dual Runtime Assistant",
    description="Show a plan and ask for confirmation using the current private Run.",
    task=True, continuable=True, interactive=True,
    input_schema={"type": "object", "properties": {"content": {"type": "string"}},
                  "required": ["content"]},
))
def assist(payload, ctx: NexusRunContext):
    ctx.plan.set([
        {"id": "review", "title": "Review your request", "status": "running"},
        {"id": "confirm", "title": "Confirm next action", "status": "pending"},
    ])
    ctx.chat.say("Received: " + payload["content"])
    ctx.plan.update("review", status="completed")
    ctx.plan.update("confirm", status="running")
    reply = ctx.chat.ask("Continue?", key="confirm-next-action", choices=[
        {"value": "continue", "label": "Continue"},
        {"value": "cancel", "label": "Cancel"},
    ])
    ctx.plan.update("confirm", status="completed")
    ctx.chat.say("Confirmed." if reply.value == "continue" else "Canceled. No action was taken.")
    return {"message": "Request reviewed", "selection": reply.value}


if __name__ == "__main__":
    agent.run()
