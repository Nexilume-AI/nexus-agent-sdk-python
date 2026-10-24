# SST Core MCP Agent

See [overview.md](overview.md) for supported components, 14 tools, examples and runtime limits.

## Build and deploy

1. Build the sibling Ramulator adapter base `nexus-ramulator-mcp:c6c7a5de0cef` (Ubuntu 24.04 and C++/Python toolchain).
2. Copy this directory to an empty deployment folder and run `sh fetch_sources.sh`.
3. `docker build -f Dockerfile.base -t nexus-sst-base:16.0.0 .`
4. `docker build -t nexus-sst-mcp:16.0.0 .`
5. `python3 check.py` on Linux with Docker runs native acceptance and isolation checks.
6. Install the Nexus Python SDK, adapt the user systemd unit paths and `agent.py` Router address/tenant, set `SST_BRIDGE_ADDRESS` if needed, then enable the unit with user lingering.
7. Pair OpenWrt with Cloud, publish the synchronized Agent, and use the authenticated Cloud MCP endpoint over Relay.

Core commit: `ffd71527bc26a1f2a4fb685fdeb2ca0799997d78`.
Elements commit: `242b48a510ead00ac775a6d80c8c5d9bb708aa8d`.
Both are official `v16.0.0_Final`. Native algorithms are unchanged. The build selects six Elements libraries using upstream's `.ignore` mechanism. Examples adapt official scripts to small self-contained workloads and CSV output; their upstream license is included.

`system-packages.lock` records installed packages. Keep the saved Docker image for exact restoration, because apt repositories and the referenced parent image can change. Source archives, SDK, service unit and complete image are backed up separately on the deployment server.

The SDK bridge validates server-provided caller identity and workspace capabilities before forwarding to a sandbox. The bridge's Docker socket is never exposed inside a worker. Do not expose its local callback port directly to the public internet.
