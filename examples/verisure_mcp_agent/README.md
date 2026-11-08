# Veri-Sure Nexus MCP adapter

Pinned upstream: xyjoey/Veri-Sure at aab5bed60f04157a4be8581b53f9e913287ba328,
MIT license. The adapter exposes 15 typed tools and six native workflows.

- agent.py: Nexus SDK/OpenWrt Relay registration and SSE request heartbeats.
- service.py: verified caller identity, private workspaces, FIFO job queue,
  idempotency, authorization, cancellation and artifact access.
- runner.py/worker.py: networkless non-root bounded Docker jobs; native TopAgent,
  Verilator judge, BooleanProofer, contract linter and trace/slicing functions.
- broker.py/model_call.py: per-job Unix socket OpenAI transport. The actual
  endpoint/model/secret are selected only from the caller's grant. Native
  AgentScope prompts, tools and orchestration remain intact. Keys never enter
  containers, environment arguments, workspaces or reports. Grant is checked
  before/after each model request and periodically during jobs.
- job_events.py/file_bridge.py: durable Background progress and authenticated
  one-time download redemption with Range, hashes and revocation.

Build the Dockerfile with pinned upstream/ sources. Its base is the existing
AVA EDA image; Yosys and yosys-smtbmc use YoWASP for compatibility with current
SymbiYosys. requirements.lock records resolved dependencies. The compatibility
patch normalizes the inherited SBY launcher to LF and caps native Verilator
builds at two jobs. No verification outcomes are patched.

Run native_acceptance.py first, then create runtime-ready.json after it passes.
Run acceptance.py for ownership/path/chunk/idempotency/job checks. The final
publication gate additionally requires actual caller-authorized generation and
public MCP/download validation. Do not claim all 209 benchmarks were validated.
Model generation: two simulation retries, three debugger trials, 32 model calls,
15-minute outer deadline. Files omitted from the 16 MiB export appear explicitly
in export-manifest.json. Source secrets and data/control must never be published.

The YoWASP machine-code cache is compiled during image construction into
/opt/yowasp-cache, readable from the read-only runtime image. This avoids cold
compilation and preserves the 32 MiB per-file job limit. Native acceptance runs
with the same limit as production. Runtime image: nexus-verisure-mcp:aab5bed60f04-r2.

Client recovery: a transport/Relay reconnect does not cancel background jobs.
Retain job_id and query job_status again; retry submission only with the original
request_id. Never create a new request_id simply because a transport response
was lost. Explicit cancel_job is available.


The systemd unit is a deployment template rooted at `/opt/nexus-agents`.
Adjust paths for your installation and provision credentials separately; no
runtime deployment JSON, credentials or acceptance artifacts are distributed.

The base-image recipe is provided in `../ava_mcp_agent`. Build that image as
`nexus-ava-mcp:d247b29c8399` before building this example. Supply the pinned AVA
and Veri-Sure upstream checkouts and an operator-selected, tested SymbiYosys
checkout (`sby/` in the AVA build context); third-party sources and images are
not bundled with the SDK. Building an example is separate from the live
hardware/model acceptance required before publishing a service.
