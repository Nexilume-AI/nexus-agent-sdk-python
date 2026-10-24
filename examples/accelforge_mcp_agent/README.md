# AccelForge MCP Agent

Hosted AccelForge analysis with caller-bound, networkless Docker workspaces.
See [overview.md](overview.md) for the 13 typed tools, examples, supported modes,
units, artifact formats and limits.

## Build and run

1. Build the sibling Ramulator adapter's base image
   `nexus-ramulator-mcp:c6c7a5de0cef` (Ubuntu24.04 + Python/C++ toolchain).
2. Copy these adapter files into an empty deployment directory. Run
   `sh fetch_sources.sh`; the upstream archive is pinned and SHA256-checked.
3. `docker build -f Dockerfile.base -t nexus-accelforge-base:7312a7d91562 .`
4. `docker build -t nexus-accelforge-mcp:7312a7d91562 .`
5. `python3 check.py` on Linux with Docker performs real native acceptance.
6. Install the Nexus SDK and adapt the absolute paths in the user systemd unit.
   Set `ACCELFORGE_BRIDGE_ADDRESS` to an address reachable from your OpenWrt Router.
   Edit `agent.py` for a Router address/tenant other than localhost:7446 and eda.
7. Enable the systemd user service with lingering; pair Router to Cloud and
   publish the synchronized Agent. No new public port or SSH tunnel is needed.

The base installs upstream-declared dependencies and pytest. Jupyter notebooks
are source examples; a public Jupyter server is not started. The optional dev
package `islpy-barvinok` is not installed. Core mapping acceptance uses the
installed islpy. `requirements.lock` records pip versions; `system-packages.lock`
records distribution packages. These inventories are not immutable package-index
snapshots; retain the complete built image for exact restoration.

## Implementation

- agent.py: typed SDK registration and streaming Relay replies/heartbeats.
- service.py / fair_queue.py: verified caller identity, workspace capabilities,
  bounded caller queues and Docker lifecycle (four workers, two per caller).
- worker.py: upload boundaries, frozen job inputs, asynchronous native process,
  timeout/cancellation and artifact download/checksums.
- runner.py: native Spec parsing, expression validation, component costing,
  Fast and Fusiest mapping, evaluation and portable result export.

Workers run non-root without network, host mounts, Docker socket or secrets,
with a read-only image,2CPUs,8GiBmemory and512MiBtemporary data. Model YAML and
Jinja expressions must be treated as untrusted; all parsing and calculations
occur inside this boundary. No explicit Python execution or custom plugin tool
is exposed. Scientific package import requires USER/LOGNAME for the numeric UID.

Upstream serialization of complete Specs includes non-YAML enum objects; those
reports are exported as JSON. Mapping export excludes transient component_object
references, uses native node tags, and normalizes empty Storage binding to a
mapping as required by its schema. Exported mappings are parsed back before a
job succeeds. Numerical algorithms are unchanged. Native version metadata from
the source archive uses setuptools-scm's fallback1.0; identify the deployment by
its source commit7312a7d915625552bf6eb1db577941f8317b6ac7, not an invented release.

Outputs include JSON,CSV,mappingYAML,SVG and logs. There is no live web viewer or
background console event stream in this release. Do not infer physical accuracy
from synthetic default example costs. Upstream MIT and third-party licenses
are retained in the image; follow upstream citation guidance for research use.

CACTI and NeuroSim package-local scratch directories are symlinked at image build
time to per-container /tmp directories. This permits native execution while the
package code and root filesystem remain read-only.
