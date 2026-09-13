# Hosted Workspace Inspector

This scenario Agent proves the caller-private Nexus hosting chain:

`MCP tools/call -> caller Computer -> private Display -> caller Memory -> Run Output`

It lists the current caller's authorized Workspace, reads only the controlled
`nexus-scenario.txt` probe, opens a read-only Agent Terminal, and writes
`directory-manifest.json` into the current Run output root. File contents,
Computer credentials, and Nexus internal tokens are never returned or stored.

Configure the Nexus Agent with:

- `computer_requirement`: `required`
- `workspace_capabilities`: `files.list`, `files.read`, `files.write`,
  `command.execute`

The caller must create and test a Computer, bind it to this Agent, and grant the
four declared scopes before calling `inspect_workspace`. The image does not
expose Connection-management tools.

Build from the Python SDK root so the image installs the local SDK wheel rather
than a published package:

```bash
python -m pip wheel --no-deps --no-cache-dir -w dist .
docker build \
  -f examples/hosted_workspace_inspector/Dockerfile \
  -t nexus-hosted-workspace-inspector:0.26.0 \
  .
```

The container serves MCP Streamable HTTP at `http://0.0.0.0:8000/mcp`.
Full server-side dual-caller acceptance additionally requires a separate Nexus Server deployment; it is not part of this standalone SDK repository.
