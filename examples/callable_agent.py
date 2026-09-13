"""A complete registered Python Agent with JSON and SSE handlers."""

import json
import os

from nexus_agent import (
    CapabilityRegistration,
    NexusAgentClient,
    NexusAgentServer,
    SseEvent,
)


client = NexusAgentClient(
    os.environ.get("NEXUS_ROUTER_URL", "https://router.example.test:7443"),
    token=os.environ.get("NEXUS_AGENT_TOKEN"),
    ca_file=os.environ.get("NEXUS_CA_FILE"),
    cert_file=os.environ.get("NEXUS_CLIENT_CERT"),
    key_file=os.environ.get("NEXUS_CLIENT_KEY"),
)

server = NexusAgentServer(
    "0.0.0.0",
    9443,
    cert_file=os.environ.get("AGENT_SERVER_CERT"),
    key_file=os.environ.get("AGENT_SERVER_KEY"),
    client_ca_file=os.environ.get("AGENT_CLIENT_CA"),
)


@server.handler("demo.echo")
def echo(envelope):
    return {
        "task_id": envelope.task_id,
        "protocol": envelope.protocol,
        "result": envelope.payload,
    }


@server.stream_handler("demo.echo")
def echo_stream(envelope):
    yield SseEvent(data="accepted", event="progress")
    yield SseEvent(
        data=json.dumps({"result": envelope.payload}, separators=(",", ":")),
        event="result",
    )


capability = CapabilityRegistration(
    intent="demo.echo",
    origin="agent://demo/echo-1",
    endpoint=os.environ.get(
        "AGENT_PUBLIC_ENDPOINT", "https://agent.example.test:9443/invoke"
    ),
    tenant="demo",
    lease_seconds=30,
)

# Registers, renews in the background, serves HTTP/SSE, and unregisters when
# serve_forever exits normally.
server.serve_registered(client, [capability])
