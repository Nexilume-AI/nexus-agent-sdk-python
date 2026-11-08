# AVA Nexus MCP adapter

Upstream HUNT-001/ai-chip-design-platform at
`d247b29c8399fa51828b184652b0b34c41d5d888` (Apache-2.0).

`agent.py` registers 14 typed tools with Nexus SDK/OpenWrt Relay. `service.py`
validates signed caller identity, manages private workspaces, fair bounded jobs,
authorization resolution and download bridges. `runner.py` launches a non-root
networkless container and imports only bounded artifacts. `worker.py` calls the
native AVA pipeline, comparator, test generator and RTL graph modules.

Model integration replaces only the upstream Ollama chat transport with a
bounded Unix-socket broker. The native semantic prompt and parser remain in use.
No provider secret enters the container, arguments, workspace or report.
Caller grants are checked before and during execution and cleared afterwards.

Build `Dockerfile` with the fixed source in `upstream/`, then run `acceptance.py`.
Copy the SDK into the configured PYTHONPATH and enable `nexus-ava-agent.service`.
Write `runtime-ready.json` only after native checks pass. Model/public acceptance
is a separate release gate. Source, image digest, dependency lock and evidence
must be saved with deployments.

The upstream pipeline can legitimately return no_data with generated harnesses;
do not relabel it as verification success. `simulate` provides actual Verilator
execution for an uploaded testbench named `tb.sv` with top `tb`. `rtl_check`
requires `top` (default demo_core). `compare` accepts canonical JSONL inputs.
`generate` uses a bounded sample of two directed plus two random tests.

No GPU is used. No existing service is replaced. Same API key means one caller;
workspace capabilities alone cannot cross caller boundaries. Protected runtime
state lives in `data/` and `control/` and must not be committed or included in
public source archives. Download required artifacts before expiry or closure.

## Verified toolchain compatibility

The image applies `compatibility_patch.py` to the pinned upstream assembly
builder: RV32IM CSR tests explicitly enable the Zicsr extension in GCC and
Spike. Modern binutils otherwise reject CSR instructions. Native generation
is accepted only when all four ELF files exist and Spike executes each one.

The final image passed the upstream suite: 1,315 passed and 4 skipped. Adapter
acceptance covers positive and deliberately mismatched commit logs, real
Verilator simulation, Yosys checks, GCC/Spike tests and the native pipeline.
A demo smoke test does not establish complete RISC-V compliance.

## Deployed release

Free Marketplace v1 was published on 2026-10-25. All 14 MCP tools are discoverable
through the Cloud endpoint. Public acceptance exercised seven workflows,
including actual caller-authorized gpt-6-sol semantic analysis. JSON, ZIP and ELF
downloads passed SHA256, Range, anonymous rejection, one-time redemption and
revocation checks. Upload retries and cancellation of a real simulation job
passed, with no remaining worker container.

[Marketplace listing](https://cloud.nexilume.com/marketplace/agents/7322879d-71e0-4b67-8c0e-37525383bdf7)


The systemd unit is a deployment template rooted at `/opt/nexus-agents`.
Adjust paths for your installation and provision credentials separately; no
runtime deployment JSON, credentials or acceptance artifacts are distributed.
