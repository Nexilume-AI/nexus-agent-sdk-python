"""Callable Agent with an IPv6 backend and router-managed public `/128`."""

import json
import os

from nexus_agent import (
    CapabilityRegistration,
    NexusAgentClient,
    NexusAgentServer,
    SseEvent,
)


backend_port = int(os.environ.get("NEXUS_AGENT_BACKEND_PORT", "9443"))
backend_identity = os.environ.get(
    "NEXUS_AGENT_BACKEND_IDENTITY", "agent-b.example.com"
)

router = NexusAgentClient(
    os.environ["NEXUS_LOCAL_ROUTER_URL"],
    token=os.environ["NEXUS_REGISTER_JWT"],
    ca_file=os.environ["NEXUS_ROUTER_CA"],
    cert_file=os.environ["NEXUS_REGISTER_CERT"],
    key_file=os.environ["NEXUS_REGISTER_KEY"],
)

server = NexusAgentServer(
    "::",
    backend_port,
    address_family="ipv6",
    dual_stack=False,
    cert_file=os.environ["NEXUS_BACKEND_CERT"],
    key_file=os.environ["NEXUS_BACKEND_KEY"],
)


@server.handler("demo.echo")
def echo(envelope):
    return {
        "ok": True,
        "served_by": "agent://demo/ipv6-agent-b",
        "payload": envelope.payload,
    }


@server.stream_handler("demo.echo")
def echo_stream(envelope):
    yield SseEvent(data="accepted", event="progress")
    yield SseEvent(
        data=json.dumps({"ok": True, "payload": envelope.payload}),
        event="result",
    )


capability = CapabilityRegistration(
    intent="demo.echo",
    origin="agent://demo/ipv6-agent-b",
    # The OpenWrt backend allowlist maps this TLS identity to a numeric IPv6.
    endpoint=f"https://{backend_identity}:{backend_port}/invoke",
    tenant="demo",
    lease_seconds=300,
    public_ipv6="auto",
)

with server.registered(router, [capability]) as leases:
    print("PUBLIC_AGENT_IPV6=" + str(leases[0].public_ipv6), flush=True)
    server.serve_forever()
