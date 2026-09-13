"""One Agent, one global IPv6 /128 on a shared host NIC."""

from nexus_agent import NexusAgent


agent = NexusAgent.public_ipv6(
    "auto",
    address_mode="host-alias",
    auth="none",
    tenant="demo",
    agent_id="echo-1",
    port=9443,
)


@agent.capability("demo.echo")
def echo(payload):
    return {"echo": payload}


if __name__ == "__main__":
    agent.run()
