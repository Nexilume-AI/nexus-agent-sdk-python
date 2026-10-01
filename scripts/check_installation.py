"""Smoke-test an installed wheel, not the source tree. No pairing or host mutations."""
import argparse
import asyncio
from importlib.metadata import metadata
import os
import sys


async def check_mcp(tasks=False):
    from fastmcp import Client, FastMCP
    from nexus_agent import NexusAgent
    from nexus_agent.fastmcp import CurrentNexusRun
    import jsonschema
    import mcp.types
    import pydantic
    import uvicorn

    assert CurrentNexusRun() is not None
    agent = NexusAgent(runtime="hosted", cloud_name="Dependency check")

    @agent.capability("audit.echo")
    def echo(payload):
        return {"echo": payload}

    server = agent.as_mcp_server()
    async with Client(server.fastmcp) as client:
        tools = await client.list_tools()
        assert any(tool.name == "audit.echo" for tool in tools)
        result = await client.call_tool("audit.echo", {"message": "dependency-check"})
        assert not result.is_error, result
    if tasks:
        # FastMCP native Tasks are separate from Nexus task=True policy metadata.
        import docket
        native = FastMCP("Native Tasks dependency check")

        @native.tool(task=True)
        async def task_echo(message: str) -> str:
            return message

        assert (await native.list_tools())[0].name == "task_echo"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--extra", default="core",
                        choices=["core", "computer", "browser", "fastmcp", "fastmcp-tasks", "a2a", "windows"])
    args = parser.parse_args()
    import nexus_agent
    assert nexus_agent.__version__ == metadata("nexilume")["Version"]
    if args.extra == "computer":
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from python_socks.async_.asyncio import Proxy
        import websockets
        from nexus_agent.computer_tool_config import tomllib
        assert tomllib.loads('model = "中文"') == {"model": "中文"}
        key = Ed25519PrivateKey.generate()
        key.public_key().verify(key.sign(b"dependency-check"), b"dependency-check")
    elif args.extra == "browser":
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            assert playwright.chromium.name == "chromium"
        # No browser download/launch here: binaries and OS libraries are separate prerequisites.
    elif args.extra in {"fastmcp", "fastmcp-tasks"}:
        asyncio.run(check_mcp(tasks=args.extra == "fastmcp-tasks"))
    elif args.extra == "a2a":
        from nexus_agent.a2a import _load_a2a
        assert callable(_load_a2a()["create_client"])
    elif args.extra == "windows":
        assert os.name == "nt", "Windows helpers must be verified on Windows"
        from nexus_agent.windows_pipe import _pywin32
        from nexus_agent.windows_service import _require_windows_modules
        assert _pywin32() and _require_windows_modules()
    else:
        for optional in ("fastmcp", "cryptography", "websockets", "playwright", "a2a"):
            assert optional not in sys.modules, "core imported optional dependency: " + optional
    print("PASS installed wheel:", args.extra, "Python", sys.version.split()[0])


if __name__ == "__main__":
    main()
