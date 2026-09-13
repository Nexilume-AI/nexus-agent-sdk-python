"""Minimal callable Agent with registration, renewal and cleanup included."""

from nexus_agent import NexusAgent, SseEvent


# router="auto" uses NEXUS_ROUTER_URL, nexus-router.local, or the LAN gateway.
# advertise_address="auto" uses NEXUS_AGENT_ADDRESS or a usable host address.
# token defaults to NEXUS_AGENT_TOKEN, so no credential needs to appear in source code.
agent = NexusAgent(
    router="auto",
    tenant="demo",
    agent_id="echo-server",
    advertise_address="auto",
)


@agent.capability("demo.echo", public_ipv6=True)
def echo(payload):
    return {"echo": payload}


@agent.stream_capability("demo.echo", public_ipv6=True)
def echo_stream(payload):
    yield SseEvent(data="accepted", event="progress")
    yield SseEvent(data=str({"echo": payload}), event="result")


if __name__ == "__main__":
    agent.run()
