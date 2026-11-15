"""Trusted startup adapter, copied into the candidate image. Never run on web hosts."""
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request


def load_server(entrypoint):
    # Process-local deployment selection, not a user credential or an inbound
    # request header. Both offline verification and production import use it.
    os.environ["NEXUS_AGENT_RUNTIME_MODE"] = "hosted"
    from nexus_agent import NexusAgent
    from fastmcp import FastMCP
    from nexus_agent.fastmcp import NexusMCPServer
    spec = importlib.util.spec_from_file_location("uploaded_agent", "/opt/nexus-python/agent.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    server = getattr(module, entrypoint)
    if isinstance(server, NexusAgent):
        server = server.as_mcp_server()
    if not isinstance(server, (FastMCP, NexusMCPServer)):
        raise TypeError("Unsupported server")
    return server


def rpc(method, params, headers):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request("http://127.0.0.1:8000/mcp", data=payload, headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers})
    with urllib.request.urlopen(request, timeout=3) as response:
        body = response.read(1024 * 1024)
        session = response.headers.get("Mcp-Session-Id")
        if session:
            headers["Mcp-Session-Id"] = session
        if response.headers.get("Content-Type", "").startswith("text/event-stream"):
            for line in body.decode().splitlines():
                if line.startswith("data:"):
                    event = json.loads(line[5:])
                    if "result" in event or "error" in event:
                        return event
        return json.loads(body)


def verify(entrypoint):
    # Application stdout/stderr is never copied into build logs or control-plane errors.
    process = subprocess.Popen([sys.executable, __file__, "serve", entrypoint], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = {"ok": False, "code": "STARTUP_FAILED"}
    try:
        headers = {"MCP-Protocol-Version": "2025-06-18"}
        for _ in range(80):
            if process.poll() is not None:
                result["code"] = "IMPORT_FAILED" if process.returncode == 42 else "STARTUP_FAILED"
                break
            try:
                initialized = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "nexus-build", "version": "1"}}, headers)
                if "error" in initialized:
                    raise ValueError()
                tools = rpc("tools/list", {}, headers).get("result", {}).get("tools", [])
                if not tools:
                    result = {"ok": False, "code": "NO_TOOLS"}
                else:
                    import importlib.metadata
                    packages = sorted({f"{d.metadata['Name']}=={d.version}" for d in importlib.metadata.distributions()})
                    result = {"ok": True, "tool_count": len(tools), "dependencies": packages, "tools": tools}
                break
            except Exception:
                time.sleep(0.25)
    finally:
        process.kill() if process.poll() is None else None
        process.wait()
    print(json.dumps(result))


if __name__ == "__main__":
    if sys.argv[1] == "verify":
        verify(sys.argv[2])
    else:
        try:
            server = load_server(sys.argv[2])
        except Exception:
            # No source snippets, tokens or exception repr in Docker logs.
            print("Python Agent import failed. Check the source and dependencies.", file=sys.stderr)
            sys.exit(42)
        server.run(transport="streamable-http", host="0.0.0.0", port=8000)
