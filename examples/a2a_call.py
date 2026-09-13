"""Call a Nexus-routed A2A Agent with one friendly method."""

import asyncio
import os

from nexus_agent.a2a import NexusA2AClient


async def main() -> None:
    async with NexusA2AClient(
        "https://router.example.test",
        card_id="echo-card",
        skill="echo",
        token=os.environ.get("NEXUS_AGENT_TOKEN"),
        transaction_token=os.environ.get("NEXUS_AGENT_TRANSACTION_TOKEN"),
    ) as client:
        result = await client.send("hello from Python")
        print(result.text)


asyncio.run(main())
