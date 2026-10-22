# Ramulator MCP Agent

Caller-isolated Ramulator 2.1 simulation through Nexus OpenWrt Relay and Cloud MCP.
The hosted service requires no model API key. Users authenticate with their own
Nexus Agent API key; the same key is one caller across MCP sessions.

## Components

- `agent.py`: Nexus SDK registration, 12 typed MCP tools and Relay streaming responses.
- `service.py`, `fair_queue.py`: verified caller ownership, workspace capabilities,
  bounded request queues, per-caller limits and container lifecycle.
- `worker.py`: narrow JSON-RPC worker, validated trace upload, background jobs,
  cancellation, artifact paging and checksum verification.
- `runner.py`: native Ramulator Python bindings; JSON parameters only.
- `patch_completion.py`: documented fix for LoadStoreTrace stopping at enqueue
  rather than request completion. Writes retain upstream command-issue semantics.
- `check.py`: actual runs on all 11 exposed DRAM standards; completion, latency,
  artifact integrity, invalid paths, caller separation, cancellation and expiry.

## Build

Use `fetch_sources.sh` in an empty deployment directory, then:

```sh
docker build -t nexus-ramulator-mcp:c6c7a5de0cef .
python3 check.py
```

Copy the adapter files to that directory before building. The image retains the
upstream MIT license and dependency licenses. Official source is pinned to
`c6c7a5de0cef35935d6fad7369e6271406798865`; dependencies are yaml-cpp 0.9.0,
fmt 10.2.1, nanobind 2.4.0 and its pinned robin-map submodule.

## Run

Install the Nexus Python SDK and set `PYTHONPATH` to its `src` directory. This
deployment registers `agent://eda/ramulator-11` with the existing Router at
`http://127.0.0.1:7446`, advertising only its reachable Docker bridge address
`10.153.89.1`. Override with `RAMULATOR_BRIDGE_ADDRESS` when needed.

Install `nexus-ramulator-agent.service` as a systemd user unit after adapting
its absolute paths. Enable user lingering for boot startup. No public listener,
SSH tunnel or additional inbound firewall rule is needed; Cloud uses Relay.

## Usage and isolation

See [overview.md](overview.md) for tools, trace format and limitations. Workspace
IDs and capabilities are private and must not be shared with other callers.
The server checks both the capability and the verified owner scope. No client
supplied user ID is trusted. Workers have no host mounts, Docker socket, network,
model credentials or publisher credentials. Files are temporary tmpfs data.

The wrapper does not execute user shell commands or Python source. Supported
simulation controls are DRAM presets, channel count and numeric DRAM overrides.
The upstream gem5 integration, custom plugins and browser visualizer are not
part of this release.
