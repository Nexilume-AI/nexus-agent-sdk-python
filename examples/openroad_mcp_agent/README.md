# OpenROAD MCP Agent

Bridge OpenROAD MCP into a Nexus OpenWrt Router, including tool descriptions,
JSON schemas, native MCP results and streaming progress. The example adds
asynchronous EDA jobs, chunked input uploads, persistent logs, artifact manifests
and verified downloads. It discovers upstream tools at startup instead of keeping
a manually shortened tool list.

## Deploy

Use a Linux Docker host with the Nexus Router running on a Docker network. The
Router must be reachable as `router:7446` on that network. Clone this SDK repository
and run from `examples/openroad_mcp_agent`:

```sh
export OPENROAD_UID=$(id -u)
export OPENROAD_GID=$(id -g)
export NEXUS_DOCKER_NETWORK=nexus-agent-network
mkdir -p ipc results work/eda work/orfs/{logs,reports,results,objects}
docker compose up -d
docker compose logs --tail 100 agent mcp
```

Set the network name to the existing Router network; Compose does not create it.
The directories must be writable by the chosen UID/GID. The bridge mounts the SDK
source from this checkout. OpenROAD MCP uses a pinned image digest; the Python
bridge uses `python:3.12-slim`. Review and pin that image for reproducible production
deployments.

The MCP container has no network, a read-only root filesystem and a Unix socket
shared with the bridge. Work directories persist across container restarts.
The agent registers as `agent://eda/openroad` with `cloud_publish=True`. This asks
the paired Router to publish the agent to Cloud; Marketplace publication remains
a separate owner action. Configure the Router to use Relay when inbound access
is unavailable. No SSH tunnel is required for MCP calls.

The command allowlist includes OpenROAD, Yosys, yosys-abc, STA, KLayout, SBY, EQY,
Kepler Formal, Naja and Yosys helper tools (see `compose.yaml`). Availability is
determined by the pinned image. A command allowlist is not a security boundary
for interactive interpreters; use separate deployments for mutually untrusted
designers.

## Verify registration and schemas

Copy `check.py` into the running bridge and run the independent SDK client:

```sh
docker compose cp check.py agent:/tmp/check.py
docker compose exec agent python3 /tmp/check.py
```

This creates a session, queries OpenROAD help, executes a Tcl assignment and
terminates the session. At startup the bridge logs the discovered tool names.
The pinned fixture covers 15 upstream tools, eight EDA job tools, a result reader
and an artifact exporter. Tool input schemas and descriptions are retained in
the registered capabilities and exposed through Cloud MCP `tools/list`.

## Remote EDA workflow

Set `NEXUS_MCP_URL`, `NEXUS_TENANT_ID` and `NEXUS_MCP_TOKEN` in your local process
environment using the credentials for your Cloud agent. Do not store tokens in
the repository. The client supports JSON and SSE responses.

```sh
python eda_client.py platforms
python eda_client.py submit --platform nangate45 --design gcd --through finish
python eda_client.py watch JOB_ID
python eda_client.py artifacts JOB_ID
python eda_client.py download JOB_ID --artifact artifacts.zip --output ./artifacts.zip
```

Choose platform/design names returned by `platforms`. For custom RTL use
`submit --platform PLATFORM --top TOP_MODULE --inputs ./inputs`; supported files
are uploaded in chunks. Keep the returned project, request and job identifiers.
Long flows return a job ID promptly; `watch` fetches new log ranges and stage
status. A disconnected client can resume watching the same job. Do not resubmit
an already running command to recover a disconnected stream.

Downloads default to authenticated Cloud private file export. Add `--mcp` to
download through bounded MCP chunks. Both paths verify size and SHA-256 before
exposing the final file. Artifacts remain unavailable until finalization finishes.

## Interactive calls and result limits

Use Yosys in a Yosys session for synthesis; OpenROAD sessions execute OpenROAD
Tcl. Read each tool's description and session type before calling it. Keep Tcl
statements intact: splitting `synth -top` and the top module name across a newline
changes the command. Prefer asynchronous EDA jobs for long synthesis/physical
design flows instead of a single blocking interactive call.

The Compose response budget is 960 KiB, leaving room inside the 1 MiB transport
limit. Oversized results are retained in a bounded local result store and returned
as references for paginated retrieval. Larger artifacts use chunked or Cloud
file downloads. Relay streaming requires matching Cloud and OpenWrt versions;
it does not remove per-event limits or client deadlines. Preserve `work`, `ipc`
configuration and `results` when updating, and back up design inputs and required
artifacts separately.

See [Agent training data](../../docs/agent-training-data.md) for explicit client
trajectory collection. MCP tool logs alone do not contain a complete conversation.
