"""Run two Agents on separate hosts and call each other through OpenWrt."""

import argparse
import json

from nexus_agent import NexusAgent, NexusAgentError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", choices=("agent-a", "agent-b"))
    parser.add_argument("--port", type=int, default=9443)
    args = parser.parse_args()
    peer = "agent-b" if args.name == "agent-a" else "agent-a"

    agent = NexusAgent(
        runtime="openwrt",
        router="auto",  # Reads NEXUS_ROUTER_URL.
        tenant="demo",
        agent_id=args.name,
        port=args.port,
        cloud_publish=False,
    )

    @agent.capability("demo.hello", public_ipv6=False)
    def hello(payload):
        return {"agent": args.name, "reply": f"Hello, {payload['from']}!"}

    try:
        with agent.start():
            print(f"{agent.origin} is ready. Start {peer} before calling it.")
            while True:
                input(f"Press Enter to call {peer}; Ctrl+C to stop: ")
                try:
                    reply = agent.invoke(
                        "demo.hello",
                        {"from": args.name},
                        target_agent=f"agent://demo/{peer}",
                    )
                    print(json.dumps(reply))
                except NexusAgentError as error:
                    print(f"Call failed: {error}. Check the peer and router, then retry.")
    except (KeyboardInterrupt, EOFError):
        pass  # Exiting the context unregisters this Agent and stops its listener.


if __name__ == "__main__":
    main()
