# OpenEvolve MCP deployment

Upstream revision: `04a37f191b2eb367daef455057a0470c0ac853d8`.
Host root: `/opt/nexus-agents/openevolve-agent`.

The host Agent verifies caller identity and authorizations. A trusted native
OpenEvolve controller container accesses a per-job Unix broker. The broker
checks authorization before every model request, holds provider secrets only
in memory, limits request counts, and launches separate evaluation containers.
Neither caller code nor generated candidates are imported by the controller.
There is no Docker socket mount or model credential inside either container.

`check.py` verifies the native engine using a deterministic test backend, then
checks evaluator isolation, evaluation timeout, caller boundaries and paths.
This is distinct from the required real-model public MCP acceptance evidence.
Do not advertise a real-model success solely from the deterministic check.

Build with the pinned upstream source in `upstream/`. Install the supplied
systemd user unit only after native acceptance. Preserve image identity,
dependency freeze, source, unit, and sanitized native/public evidence together.
The shared host Nexus SDK is configured in the service unit. Private runtime
directories are `data/` and `control/`; never publish them or their tokens.

The console uses existing BackgroundJobs delivery and cancellation mechanisms.
MCP file links use the existing authenticated outbound-only file bridge.
See [overview.md](overview.md) for capabilities, limits and client workflow.
