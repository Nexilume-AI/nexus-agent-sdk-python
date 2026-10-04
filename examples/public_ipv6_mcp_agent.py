"""Native MCP plus Nexus Invoke on one host-owned IPv6 address.

Install nexilume[fastmcp]. 'auto' requires the existing local addressd service.
Use TLS and a dedicated per-Agent JWT audience for any untrusted network.
"""

import argparse
from pathlib import Path

from nexus_agent import HmacJwtServerAuth, NexusAgent
from nexus_agent.fastmcp import NexusMCPServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="auto")
    parser.add_argument("--agent-id", default="native-mcp-agent")
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--port", type=int, default=9443)
    parser.add_argument("--mcp-port", type=int)
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--jwt-secret-file", type=Path, help="Private file containing a dedicated 32+ byte secret")
    auth.add_argument("--insecure-no-auth", action="store_true", help="Controlled test networks only")
    parser.add_argument("--issuer", default="urn:nexus:local")
    parser.add_argument("--cert-file")
    parser.add_argument("--key-file")
    parser.add_argument("--tls-server-name")
    parser.add_argument("--ca-bundle-id")
    args = parser.parse_args()

    policy = "none" if args.insecure_no_auth else HmacJwtServerAuth(
        args.jwt_secret_file.read_text(encoding="utf-8").strip(),
        issuer=args.issuer, audience=f"agent://{args.tenant}/{args.agent_id}",
    )
    mcp = NexusMCPServer("Native IPv6 Agent")

    @mcp.tool
    def echo(message: str) -> dict:
        """Echo a message without external side effects."""
        return {"echo": message}

    @mcp.resource("agent://info")
    def info() -> str:
        return f"Agent: {args.agent_id}"

    @mcp.prompt
    def greeting(name: str) -> str:
        return f"Greet {name} using the echo tool."

    agent = NexusAgent.public_ipv6(
        args.address, auth=policy, tenant=args.tenant, agent_id=args.agent_id,
        port=args.port, mcp=mcp, mcp_port=args.mcp_port,
        cert_file=args.cert_file, key_file=args.key_file,
        tls_server_name=args.tls_server_name, ca_bundle_id=args.ca_bundle_id,
    )

    @agent.capability("demo.echo")
    def invoke_echo(payload):
        # Explicit reuse, not automatic exposure of every MCP component.
        return echo(**payload)

    agent.run()


if __name__ == "__main__":
    main()
