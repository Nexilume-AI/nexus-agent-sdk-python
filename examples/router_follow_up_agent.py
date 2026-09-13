"""Private Display Queue/Steer demo; Cloud must support inbox protocol v1.

Run through an enrolled router, then send another message during the ten steps.
This example changes its subsequent echo text, not already-executed operations.
"""
import asyncio
import argparse

from nexus_agent import McpToolDescriptor, NexusAgent, NexusRunContext


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--router", default="http://192.168.250.1:7446/")
    args = parser.parse_args()
    agent = NexusAgent(router=args.router, tenant="default", agent_id="follow-up-demo",
        cloud_publish=True, cloud_name="Cooperative follow-up demo", advertise_address="auto")

    @agent.capability("demo.follow_up", follow_up="steer_and_queue", tool=McpToolDescriptor(
        name="follow_up_chat", chat=True, task=True, continuable=True,
        description="Ten-step echo with cooperative follow-up. No Computer access.",
        input_schema={"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
    ))
    async def follow_up_chat(data, ctx: NexusRunContext):
        text = data["message"]
        for index in range(10):
            await ctx.aio.raise_if_cancelled()
            for instruction in await ctx.aio.inbox.receive_pending():
                # Safe point: update the next step before acknowledging receipt.
                text = instruction.content
                await instruction.acknowledge()
            await ctx.aio.chat.say(f"Step {index + 1}/10: {text}")
            await asyncio.sleep(1)
        return {"message": text, "steps": 10, "turn_index": ctx.turn_index}

    agent.run()


if __name__ == "__main__":
    main()
