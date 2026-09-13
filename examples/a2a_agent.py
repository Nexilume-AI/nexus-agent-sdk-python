"""Expose an official A2A AgentExecutor without writing an Agent Card."""

import os

from a2a.helpers import get_message_text, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import Role

from nexus_agent.a2a import NexusA2AAgent


class EchoAgent(AgentExecutor):
    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        text = get_message_text(context.message)
        await event_queue.enqueue_event(
            new_text_message(
                f"echo:{text}",
                role=Role.ROLE_AGENT,
                context_id=context.message.context_id,
            )
        )

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        raise NotImplementedError


agent = NexusA2AAgent(
    EchoAgent(),
    router="https://router.example.test:7443",
    token=os.environ.get("NEXUS_AGENT_TOKEN"),
    identity="agent://demo/a2a-echo-1",
    endpoint="https://a2a-echo-1.example.test:9443/invoke",
    tenant="demo",
    host="0.0.0.0",
    port=9443,
    cert_file="agent-server.crt",
    key_file="agent-server.key",
    card_url="https://router.example.test/a2a/echo-card/echo",
)
agent.expose(skill="echo", intent="demo.a2a.echo")
agent.run()
