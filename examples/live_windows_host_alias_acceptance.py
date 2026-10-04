"""Privileged Windows live acceptance for three Agent-owned public /128s."""

import argparse
import asyncio
import ipaddress
import os
import threading
import time
import uuid

from nexus_agent import (
    DirectIPv6Agent,
    HmacJwtServerAuth,
    LocalAddressdClient,
    NexusAgent,
    WindowsAddressBackend,
    WindowsNamedPipeTransport,
)
from nexus_agent.addressd import AddressdApplication
from nexus_agent.host_alias import HostAliasAllocator
from nexus_agent.windows_pipe import AddressdNamedPipeServer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--port", type=int, default=20043)
    parser.add_argument("--native-mcp", action="store_true",
                        help="Also verify real MCP and Agent-to-Agent calls on each /128")
    arguments = parser.parse_args()

    pipe_name = r"\\.\pipe\nexus-addressd-live-" + uuid.uuid4().hex
    backend = WindowsAddressBackend(timeout=10.0)
    allocator = HostAliasAllocator(
        backend=backend,
        interface=arguments.interface,
        prefix=arguments.prefix,
        allocation_secret=os.urandom(32),
        max_addresses=8,
        default_lease_seconds=60,
        reservation_seconds=15,
    )
    if not backend.prefix_ready(arguments.interface, allocator.prefix):
        raise RuntimeError("selected global /64 is not on-link")

    source_agent = "agent://live/test-caller"
    auth = HmacJwtServerAuth(
        os.urandom(32).hex(),
        issuer="urn:nexus:live-acceptance",
        audience="windows-host-alias-test",
        max_lifetime_seconds=120,
    )
    token = auth.issue(
        subject="local-acceptance-caller",
        tenant="live",
        source_agent=source_agent,
        expires_in=120,
    )
    application = AddressdApplication(allocator)
    pipe_server = AddressdNamedPipeServer(
        pipe_name,
        application,
        allowed_group=None,
        sweep_interval=1.0,
    )
    pipe_thread = threading.Thread(target=pipe_server.serve_forever, daemon=True)
    pipe_thread.start()
    client = LocalAddressdClient(
        WindowsNamedPipeTransport(pipe_name, timeout=5.0),
        owner="ignored-by-windows-server",
    )

    agents = []
    handles = []

    def make_echo(number):
        def echo(payload: dict) -> dict:
            return {"served_by": f"windows-agent-{number}", "echo": payload}
        return echo

    def make_peer_call(number):
        async def call_peer(payload: dict) -> dict:
            from fastmcp import Client
            from fastmcp.client.transports import StreamableHttpTransport
            import httpx

            peer = agents[number % 3]
            peer_token = auth.issue(subject=f"windows-agent-{number}", tenant="live",
                                    source_agent=f"agent://live/windows-agent-{number}", expires_in=120)
            transport = StreamableHttpTransport(
                peer.mcp_url, auth=peer_token,
                httpx_client_factory=lambda **kw: httpx.AsyncClient(trust_env=False, **kw),
            )
            async with Client(transport, timeout=10) as remote:
                return (await remote.call_tool("echo", {"payload": payload})).data
        return call_peer

    try:
        for number in range(1, 4):
            echo = make_echo(number)
            mcp = None
            if arguments.native_mcp:
                from nexus_agent.fastmcp import NexusMCPServer
                mcp = NexusMCPServer(f"Windows IPv6 Agent {number}")
                mcp.tool(name="echo")(echo)
                mcp.tool(name="call_peer")(make_peer_call(number))
            agent = NexusAgent.public_ipv6(
                "auto",
                address_mode="host-alias",
                allocator=client,
                interface=arguments.interface,
                prefix=arguments.prefix,
                address_lease_seconds=60,
                auth=auth,
                tenant="live",
                agent_id=f"windows-agent-{number}",
                port=arguments.port,
                mcp=mcp,
            )
            agent.capability("demo.echo")(echo)
            handles.append(agent.start(announce=False))
            agents.append(agent)

        time.sleep(1.0)
        print("LIVE_ADDRESSES", flush=True)
        for agent in agents:
            present = backend.has_address(
                arguments.interface, ipaddress.IPv6Address(agent.address)
            )
            print(
                agent.agent_id,
                agent.endpoint.url,
                "present=" + str(present),
                flush=True,
            )

        print("JWT_INVOKE_RESULTS", flush=True)
        for number, agent in enumerate(agents, 1):
            target = DirectIPv6Agent.plain_http(
                agent.address,
                port=arguments.port,
                token=token,
                timeout=5.0,
            )
            result = target.invoke(
                "demo.echo",
                {"request": number},
                tenant="live",
                source_agent=source_agent,
            )
            print(agent.agent_id, result, flush=True)
        if arguments.native_mcp:
            async def check_native():
                from fastmcp import Client
                from fastmcp.client.transports import StreamableHttpTransport
                import httpx

                async def check_one(number, agent):
                    transport = StreamableHttpTransport(
                        agent.mcp_url, auth=token,
                        httpx_client_factory=lambda **kw: httpx.AsyncClient(trust_env=False, **kw),
                    )
                    async with Client(transport, timeout=10) as caller:
                        names = {tool.name for tool in await caller.list_tools()}
                        assert names == {"echo", "call_peer"}, names
                        direct = await caller.call_tool("echo", {"payload": {"request": number}})
                        assert direct.data["served_by"] == agent.agent_id
                        peer = await caller.call_tool("call_peer", {"payload": {"from": number}})
                        assert peer.data == {"served_by": f"windows-agent-{number % 3 + 1}",
                                             "echo": {"from": number}}, peer.data
                        print("NATIVE_MCP_OK", agent.agent_id, "peer=" + peer.data["served_by"], flush=True)

                await asyncio.gather(*(check_one(i, a) for i, a in enumerate(agents, 1)))

            asyncio.run(check_native())
    finally:
        for handle in reversed(handles):
            try:
                handle.close()
            except Exception as exc:
                print("CLOSE_WARNING", repr(exc), flush=True)
        pipe_server.shutdown()
        pipe_thread.join(timeout=3.0)
        pipe_server.server_close()
        print("CLEANUP leases=" + str(len(allocator.list())), flush=True)
        for agent in agents:
            address = ipaddress.IPv6Address(agent.address)
            print(
                "removed",
                agent.address,
                not backend.has_address(arguments.interface, address),
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
