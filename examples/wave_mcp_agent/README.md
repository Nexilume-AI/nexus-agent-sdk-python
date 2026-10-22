# Wave MCP on Nexus OpenWrt

Community integration of Tencent/wave-mcp 1.1.0, commit
`0b8c5fff11c9b4bf1e668787322ec41d08dd9d91`. Upstream Apache-2.0 LICENSE and
third-party notices remain in the deployment's `upstream` directory.

## Architecture

Cloud MCP → existing OpenWrt Relay → Nexus SDK bridge → an isolated official
wave-mcp stdio server per workspace. `agent.py` runs as the existing non-root
deployment user with permission to start Docker containers. Its internal HTTP
listener binds only the Router Docker bridge (`WAVE_BRIDGE_ADDRESS`, default
10.153.89.1), avoiding the firewall-blocked host IPv6 callback path.

Workers default to `nexus-wave-mcp:public-viewer-20261022`; `WAVE_IMAGE`
selects a tested optional image (see `deployment.json` and `OPTIONALS.md`). They run as a non-root UID,
have no network or host mounts, drop all capabilities, use a read-only rootfs and
a 256 MiB `/work` tmpfs. Memory is limited to 768 MiB, CPU to 1 and PIDs to 64.
Four workspaces may run concurrently, with two per verified caller. Access requires
both the Cloud-verified caller identity and a random 256-bit capability; only its
hash is retained by the host bridge. A workspace is not a
durable datastore: download files before its 30-minute idle expiry, explicit close
or service restart. Each call has a 45-second worker timeout; timeout closes that
workspace. Large results can be read in pages without rerunning their tool.

Upstream tool schemas retain argument types and constraints (cosmetic titles are
removed), with required workspace credentials added. Descriptions summarize the
operation within Router metadata limits. `wave_tool_help` provides complete
original descriptions and schemas. Five viewer tools are excluded until the
optional viewer assets and an authenticated remote browser route are available. FSDB requires a separately supplied Verdi
runtime. FST/VCD and static SystemVerilog are ready to use.

See [OPTIONALS.md](OPTIONALS.md) for locally built viewer assets, Verible,
fstdumper, build tools, version pins and remaining runtime requirements.

## Install / restore

1. Check out the pinned upstream as `upstream/` beside the Dockerfile and keep its
   license and third-party notices. Build with
   `docker build -t nexus-wave-mcp:0b8c5fff11c9 .`, or restore the preserved image
   archive with `gzip -dc wave-image.tar.gz | docker load`.
2. Set `PYTHONPATH` to the deployed Nexus SDK `src` directory. The SDK itself has
   no third-party dependencies for this bridge. Set the Router bridge address if
   it differs from this installation.
3. Run `python3 check.py` against Docker to verify real VCD conversion, signal
   values, driver extraction, static-only analysis, upload retries, download
   SHA256, workspace isolation, traversal rejection and result paging.
4. Run `python3 agent.py` as a persistent user service. The installed unit is
   `nexus-wave-agent.service`, enabled under a user with lingering enabled.
5. Publish the discovered `agent://<tenant>/<agent-id>` with the English `overview.md`,
   `marketplace-icon.png`, free pricing and a version. Cloud authentication is
   required for MCP calls; do not put account secrets in this source directory.

## External MCP configuration

Copy the personal Agent credential from the Marketplace Connect panel:

```toml
[mcp_servers.Wave-MCP]
url = "https://cloud.nexilume.com/api/v1/agents/<agent-id>/mcp/"

[mcp_servers.Wave-MCP.http_headers]
X-Nexus-Tenant = "<your tenant from the Connect panel>"
Authorization = "Bearer <your personal Agent API key>"
```

Start with `create_wave_workspace(sample=true)`, then `prepare_session` using the
returned sample paths. Retain the workspace ID/key and session ID for later calls.
Call `close_wave_workspace` when finished. No SSH tunnel or model provider API key
is needed. See `overview.md` for the complete user workflow and limitations.

## Caller isolation and fair scheduling

One API Key is one caller across MCP sessions. Clients sharing it share workspaces,
model authorizations and quotas. Use separate Keys for independent callers.
`X-Nexus-End-User` is a tracing label and cannot change identity or bypass limits.
Logged-in account calls have an identity distinct from their API Keys.

The bridge obtains `current_run().caller_identity()` from the authenticated Cloud
Run context callback. Workspaces bind to caller + tenant + project + Agent;
quotas bind to caller + tenant. Old Cloud versions and direct SDK requests without
verified identity fail closed. Tool arguments cannot supply this identity.

The bridge admits two active calls overall and one per caller. Waiting calls are
FIFO within a caller and round-robin across callers, bounded to eight pending per
caller and 32 overall. A ten-second queue deadline returns an explicit error;
clients should back off before retrying. Worker RPCs have a separate 45-second
deadline. Streaming sends heartbeats while waiting. Workspace allocation rejects
requests exceeding two per caller or four overall; close unused workspaces first.
These limits apply to one bridge process; run one instance for this deployment.

Run `python3 -m unittest test_isolation -v` for deterministic isolation, quota,
scheduling, timeout, shutdown and cleanup tests.

## Public browser viewer

The five viewer tools are enabled (45 total tools). Deploy matching Cloud viewer
routes, migration 0066, Redis and trusted Surfer WASM assets before enabling this
bridge. See `PUBLIC_VIEWER.md` for the capability flow, limits and deployment.

Set `NEXUS_TENANT` and `NEXUS_AGENT_ID` for your Router. The example user service expects files below `%h/nexus`; adapt paths before installing.
