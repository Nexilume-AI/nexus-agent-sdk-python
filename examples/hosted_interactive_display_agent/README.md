# Hosted Interactive Display Agent

This Docker example exercises the Nexus 0.27 Display contract:

`MCP Tool → Plan → Shell → protected Browser frame → Chat input → Checkpoint → result`

The Tool is declared with `task=True`, `continuable=True`, and `demo=True`. Recovery is platform-managed by SDK 0.45.0 rather than declared by the Tool.
Authenticated callers can use foreground Streamable HTTP/SSE or the MCP Tasks
extension. Anonymous Marketplace visitors can only start this explicit Demo
Tool; the Demo receives no Workspace, SSH, Memory, Output, billing, or
Marketplace-consumption authority.

Build from the SDK root after creating the local wheel:

```bash
python -m pip wheel --no-deps --no-cache-dir -w dist .
docker build -f examples/hosted_interactive_display_agent/Dockerfile \
  -t nexus-hosted-interactive-display:0.27.0 .
```

The server exposes MCP Streamable HTTP at `/mcp`. Each real call or anonymous
Demo gets a distinct Run, Browser Asset, Chat Interaction, and Checkpoint.
