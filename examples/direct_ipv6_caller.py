"""Invoke one configured IPv6 Agent without discovery, Relay or Transit."""

import json
import os

from nexus_agent import DirectIPv6Agent


target = DirectIPv6Agent(
    os.environ["NEXUS_TARGET_IPV6"],
    token=os.environ["NEXUS_INVOKE_JWT"],
)

result = target.invoke(
    "demo.echo",
    {"message": "hello over direct IPv6"},
    tenant="demo",
    source_agent="agent://demo/ipv6-caller-a",
)
print(json.dumps(result, ensure_ascii=False, indent=2))
