# ChampSim MCP Agent

Native trace-driven CPU simulation through Nexus SDK, OpenWrt Relay and Cloud MCP.
See [overview.md](overview.md) for all tools, trace formats and execution limits.

## Reproduce the deployment

1. Build the sibling `ramulator_mcp_agent` image using its pinned source recipe.
   This release intentionally reuses that Ubuntu/Python/C++ image as its base:
   `nexus-ramulator-mcp:c6c7a5de0cef`.
2. Copy this adapter into a deployment directory and run `sh fetch_sources.sh`.
3. Run `docker build -t nexus-champsim-mcp:410cee62d9c4 .`.
4. Run `python3 check.py` on a Linux host with Docker. It runs all three native
   presets and checks stats downloads, ownership, invalid traces and cancellation.
5. Install the Nexus Python SDK. Adjust the paths in the systemd user unit and
   set `CHAMPSIM_BRIDGE_ADDRESS` to an address reachable from your OpenWrt Router.
   The Router endpoint is `http://127.0.0.1:7446`; edit the registration in
   `agent.py` for a different tenant or Router address.
6. Install and enable the unit with user lingering for boot startup. Pair the
   Router with Cloud and publish the synchronized Agent with its overview.

The service uses outbound Relay; no additional public host port is required.
The Docker daemon is available only to the trusted service process. Workspace
containers have no network or host mounts and run without root privileges.

## Dependencies and source preservation

ChampSim is pinned to `410cee62d9c429c043ea611325ce00e6c303be2a`.
The image uses Ubuntu CLI11, fmt, nlohmann-json, bzip2, xz and zlib packages,
not upstream vcpkg. CLI11 is header-only in this distribution. The Dockerfile
removes its linker flag and supplies the include path without `-isystem
/usr/include`, which otherwise conflicts with GCC's `include_next`.
`requirements.lock` records the installed distribution package versions; it is
an inventory, not an apt snapshot guaranteeing bit-for-bit future builds.
Preserve the built image for exact recovery. Upstream Apache-2.0 and all base
image dependency licenses remain in the image.

## Supported workload

Three compiled single-core presets: baseline, next-line, perceptron. Input is
standard 64-byte ChampSim instruction records, optionally gzip/xz/bzip2.
The bundled trace is synthetic and suitable only for demonstrations. This
adapter does not compile user modules or run Pin/CloudSuite tracing.

Jobs return immediately and expose state and logs through `job_status`.
This release has no interactive viewer or pushed background console events.
Download artifacts before workspace expiry (30 minutes idle) or service restart.
