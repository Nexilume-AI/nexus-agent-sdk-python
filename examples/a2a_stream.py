"""Stream one routed A2A task with normalized progress events."""

import asyncio
import os

from nexus_agent.a2a import NexusA2AClient


async def main() -> None:
    access_token = os.environ.get("NEXUS_AGENT_TOKEN")
    transaction_token = os.environ.get("NEXUS_AGENT_TRANSACTION_TOKEN")
    if access_token and transaction_token:
        raise RuntimeError("set JWT or Transaction Token, not both")

    async with NexusA2AClient(
        "https://router.example.test",
        card_id="echo-card",
        skill="echo",
        token=access_token,
        transaction_token=transaction_token,
    ) as client:
        async for event in client.stream("verify this design"):
            print(event.kind, event.state, event.text, event.final)


asyncio.run(main())
