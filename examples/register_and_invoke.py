import os
import time

from nexus_agent import CapabilityRegistration, NexusAgentClient


client = NexusAgentClient(
    os.environ.get("NEXUS_ROUTER_URL", "http://127.0.0.1:7788"),
    token=os.environ.get("NEXUS_AGENT_TOKEN"),
)

capability = CapabilityRegistration(
    intent="demo.echo",
    origin="agent://demo/echo-1",
    endpoint="http://127.0.0.1:9001/invoke",
    tenant="demo",
    lease_seconds=30,
)

with client.register(capability, auto_renew=True) as lease:
    print(f"registered route_id={lease.route_id}")
    while True:
        time.sleep(5)
        if lease.last_error:
            print(f"last renewal error: {lease.last_error}")
