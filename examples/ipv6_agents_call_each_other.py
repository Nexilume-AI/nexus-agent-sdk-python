"""Give two Agents distinct IPv6 addresses and let them call each other.

This is an isolated-lab example. It intentionally uses cleartext HTTP without
JWT so the IPv6 addressing and direct-call path remain visible. Use TLS and an
authenticated ServerAuthPolicy outside a trusted lab network.
"""

import json
import os

from nexus_agent import DirectIPv6Agent, NexusAgent


if os.environ.get("NEXUS_IPV6_LAB") != "1":
    raise SystemExit(
        "Set NEXUS_IPV6_LAB=1 only on an isolated lab network before running."
    )

port = int(os.environ.get("NEXUS_AGENT_PORT", "9443"))

agent_a = NexusAgent.public_ipv6(
    "auto",
    address_mode="host-alias",
    auth="none",
    tenant="demo",
    agent_id="agent-a",
    port=port,
)
agent_b = NexusAgent.public_ipv6(
    "auto",
    address_mode="host-alias",
    auth="none",
    tenant="demo",
    agent_id="agent-b",
    port=port,
)


@agent_a.capability("demo.hello")
def hello_a(payload):
    return {"agent": agent_a.origin, "received": payload}


@agent_b.capability("demo.hello")
def hello_b(payload):
    return {"agent": agent_b.origin, "received": payload}


with agent_a.start(), agent_b.start():
    target_b = DirectIPv6Agent.plain_http(
        agent_b.address,
        port=agent_b.endpoint.port,
    )
    a_to_b = target_b.invoke(
        "demo.hello",
        {"message": "hello from Agent A"},
        tenant="demo",
        source_agent=agent_a.origin,
    )

    target_a = DirectIPv6Agent.plain_http(
        agent_a.address,
        port=agent_a.endpoint.port,
    )
    b_to_a = target_a.invoke(
        "demo.hello",
        {"message": "hello from Agent B"},
        tenant="demo",
        source_agent=agent_b.origin,
    )

    print(json.dumps({
        "agent_a": {"ipv6": agent_a.address, "reply": b_to_a},
        "agent_b": {"ipv6": agent_b.address, "reply": a_to_b},
    }, ensure_ascii=False, indent=2))
