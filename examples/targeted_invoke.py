"""Invoke one exact Agent instance through Nexus capability routing."""

import os

from nexus_agent import NexusAgentClient


client = NexusAgentClient(
    os.environ.get("NEXUS_ROUTER_URL", "http://192.168.1.1:7443"),
    token=os.environ.get("NEXUS_AGENT_TOKEN"),
    ca_file=os.environ.get("NEXUS_CA_FILE"),
    cert_file=os.environ.get("NEXUS_CLIENT_CERT"),
    key_file=os.environ.get("NEXUS_CLIENT_KEY"),
)

result = client.invoke_intent(
    os.environ.get("NEXUS_INTENT", "demo.echo"),
    {"message": "hello from another network"},
    tenant=os.environ.get("NEXUS_TENANT", "demo"),
    source_agent=os.environ.get(
        "NEXUS_SOURCE_AGENT", "agent://demo/caller-1"
    ),
    target_agent=os.environ.get(
        "NEXUS_TARGET_AGENT", "agent://remote/echo-2"
    ),
)
print(result)
