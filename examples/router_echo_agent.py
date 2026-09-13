"""Run a minimal echo Agent registered with the Nexus OpenWrt router."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from typing import Any, Optional, Sequence

from nexus_agent import McpToolDescriptor, NexusAgent, NexusAgentClient


DEFAULT_ROUTER_URL = "http://192.168.250.1:7446"


def echo(payload: Any) -> Any:
    """Return the request payload unchanged."""

    return payload


def build_agent(*, router: str, auth: str) -> NexusAgent:
    """Build the Agent without starting its listener or registering it."""

    agent = NexusAgent(
        router=router,
        auth=auth,
        tenant="demo",
        agent_id="echo-agentc",
        advertise_address="auto",
        lease_seconds=60,
    )
    tool = McpToolDescriptor(
        name="demo.echo",
        title="Echo",
        description="Return the request payload unchanged.",
        input_schema={"type": "object", "additionalProperties": True},
        demo=True,chat=True
    )
    # Keep this example on the enrolled Cloud Relay; Demo policy is independent.
    agent.capability("demo.echo", public_ipv6=True, tool=tool, )(echo)
    return agent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--router",
        default=os.environ.get("NEXUS_ROUTER_URL", DEFAULT_ROUTER_URL),
        help="Nexus Router Agent Access Proxy URL",
    )
    parser.add_argument(
        "--auth",
        choices=("auto", "none"),
        default="auto",
        help="Use 'auto' for token/OIDC auth; 'none' only for a no-JWT LAN port",
    )
    parser.add_argument(
        "--run-seconds",
        type=float,
        default=0,
        help="Stop after this many seconds; 0 runs until Ctrl+C",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Invoke demo.echo through a second automatically authenticated client",
    )
    parser.add_argument(
        "--refresh-wait-seconds",
        type=float,
        default=0,
        help="With --self-test, wait and invoke again to exercise token refresh",
    )
    parser.add_argument(
        "--wait-for-cloud-seconds",
        type=float,
        default=0,
        help="Wait for the router-managed Cloud Agent and print its standard MCP URL",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.run_seconds < 0:
        raise SystemExit("--run-seconds must be non-negative")
    if args.refresh_wait_seconds < 0:
        raise SystemExit("--refresh-wait-seconds must be non-negative")
    if args.wait_for_cloud_seconds < 0:
        raise SystemExit("--wait-for-cloud-seconds must be non-negative")

    agent = build_agent(router=args.router, auth=args.auth)
    if args.run_seconds == 0 and not args.self_test and args.wait_for_cloud_seconds == 0:
        agent.run()
        return 0

    handle = agent.start()
    if args.wait_for_cloud_seconds > 0:
        cloud = handle.wait_for_cloud(args.wait_for_cloud_seconds)
        print(json.dumps({
            "cloud_agent_id": cloud.agent_id,
            "cloud_runtime_id": cloud.runtime_id,
            "cloud_transport": cloud.transport,
            "cloud_mcp_url": cloud.mcp_url,
        }, ensure_ascii=False))
    if args.self_test:
        try:
            caller = NexusAgentClient(args.router)
            payload = {"message": "hello from Nexus LAN bootstrap"}
            response = caller.invoke_intent(
                "demo.echo",
                payload,
                tenant="demo",
                source_agent="agent://demo/echo-test-client",
                target_agent=agent.origin,
            )
            if response != payload:
                raise RuntimeError(f"echo mismatch: {response!r}")
            print(json.dumps({"route_id": handle.leases[0].route_id,
                              "echo": response}, ensure_ascii=False))
            if args.refresh_wait_seconds > 0:
                time.sleep(args.refresh_wait_seconds)
                refreshed = caller.invoke_intent(
                    "demo.echo",
                    payload,
                    tenant="demo",
                    source_agent="agent://demo/echo-test-client",
                    target_agent=agent.origin,
                )
                if refreshed != payload:
                    raise RuntimeError(f"refreshed echo mismatch: {refreshed!r}")
        except BaseException:
            handle.close()
            raise
        if args.run_seconds == 0:
            handle.close()
            return 0
    timer = threading.Timer(args.run_seconds, handle.close)
    timer.daemon = True
    timer.start()
    try:
        handle.wait(args.run_seconds + 5)
    finally:
        timer.cancel()
        handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
