"""Private Display Queue/Steer demo; Cloud must support inbox protocol v1.

Run through an enrolled router, or upload this Python file with entrypoint agent.
Send another message while the steps are running.
This example changes its subsequent echo text, not already-executed operations.
"""
import asyncio
import argparse

from nexus_agent import McpToolDescriptor, NexusAgent, NexusRunContext


router = "auto"
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--router", default="auto", help="Optional enrolled Router URL; otherwise use SDK discovery")
    router = parser.parse_args().router

# A top-level constructor lets Cloud's static upload inspector find this Agent.
# The trusted hosted launcher selects hosted mode before importing the file.
agent = NexusAgent(router=router, tenant="default", agent_id="follow-up-demo",
    cloud_publish=True, cloud_name="Cooperative follow-up demo", advertise_address="auto")

@agent.capability("demo.follow_up", follow_up="steer_and_queue", tool=McpToolDescriptor(
        name="follow_up_chat", chat=True, task=True, continuable=True,
        description="Step-by-step echo with cooperative follow-up. No Computer access.",
        input_schema={"type": "object", "properties": {
            "message": {"type": "string"},
            "steps": {"type": "integer", "minimum": 1, "maximum": 120, "default": 30},
        }, "required": ["message"]},
))
async def follow_up_chat(data, ctx: NexusRunContext):
    text = data["message"]
    steps = data.get("steps", 30)
    if type(steps) is not int or not 1 <= steps <= 120:
        raise ValueError("steps must be an integer between 1 and 120")
    applied = []
    for index in range(steps):
        await ctx.aio.raise_if_cancelled()
        for instruction in await ctx.aio.inbox.receive_pending():
            # Safe point: update the next step before acknowledging receipt.
            text = instruction.content
            await instruction.acknowledge()
            applied.append({"id": instruction.id, "content": text})
        await ctx.aio.chat.say(f"Step {index + 1}/{steps}: {text}")
        await asyncio.sleep(1)
    return {"message": text, "steps": steps, "turn_index": ctx.turn_index, "applied": applied}


if __name__ == "__main__":
    agent.run()
