"""Agent-owned public IPv6 endpoint with explicit No JWT or HS256 JWT mode."""

import os

from nexus_agent import HmacJwtServerAuth, NexusAgent, SseEvent


address = os.environ["NEXUS_AGENT_PUBLIC_IPV6"]
port = int(os.environ.get("NEXUS_AGENT_PUBLIC_PORT", "9443"))
mode = os.environ.get("NEXUS_AGENT_DIRECT_AUTH", "none")

if mode == "none":
    authentication = "none"
elif mode == "jwt-hs256":
    authentication = HmacJwtServerAuth(
        os.environ["NEXUS_AGENT_JWT_SECRET"],
        issuer=os.environ["NEXUS_AGENT_JWT_ISSUER"],
        audience=os.environ["NEXUS_AGENT_JWT_AUDIENCE"],
    )
else:
    raise ValueError("NEXUS_AGENT_DIRECT_AUTH must be none or jwt-hs256")

agent = NexusAgent.public_ipv6(
    address,
    port=port,
    auth=authentication,
    tenant=os.environ.get("NEXUS_AGENT_TENANT", "demo"),
    agent_id=os.environ.get("NEXUS_AGENT_ID", "public-echo-1"),
)


@agent.capability("demo.echo")
def echo(payload):
    return {"echo": payload}


@agent.stream_capability("demo.progress")
def progress(payload):
    yield SseEvent(data="accepted", event="progress")
    yield SseEvent(data=str({"echo": payload}), event="result")


agent.run()
