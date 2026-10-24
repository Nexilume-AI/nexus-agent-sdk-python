# Nexus Mobilerun Agent

Community MCP integration of [droidrun/mobilerun](https://github.com/droidrun/mobilerun), pinned to `3b62bd767131db32bce2632d55f3372b62fff015` (0.7.1, MIT).

## Architecture

Nexus Cloud authenticates the caller and snapshots their granted phone binding. The streaming Relay carries signed Run context to this adapter. `NexusMobileDriver` translates native Mobilerun actions to the Run-bound Nexus Mobile delegate. No device address, shell, plugin or Python code is accepted from the caller.

A per-Run loopback model broker resolves the caller-approved authorization before each request. Provider credentials never enter the native LLM object, tool arguments or logs. HTTPS endpoints are validated and resolved to public addresses, pinned per connection; redirects and ambient proxies are disabled. Revocation, expiry, cancellation and request budgets are enforced.

Two active tasks, eight waiting tasks, one active/queued task per caller, 20 steps and 600 seconds maximum. Nexus retains device leases, ownership checks and revocation. Authenticated MCP invocation authorizes the bound phone for its declared scopes without extra Cloud Mobile approvals. Same API key means the same caller. Telemetry and framework trajectory recording are disabled; screenshots go to authenticated Run display storage.

## Build and deploy

Use Python 3.12. Docker build context needs `upstream/` at the pinned revision, Nexus Python SDK source as `sdk/`, this directory as `adapter/`, and the included Dockerfile at the context root. Build `nexus-mobilerun:3b62bd767131`. The included service unit is a deployment example for the 11 host; change paths and router address for another installation.

Run as an unprivileged container user with read-only root, no Linux capabilities or Docker socket, limited memory/processes, and writable temporary storage. Host networking is used to reach the existing local OpenWrt gateway and advertise its bridge address. The adapter is mounted read-only.

Register and review all five tools, set free pricing and English overview. Cloud deployments that intentionally support Mobile over streaming Relay must explicitly opt this Agent into the credential, Relay Chat and Relay Mobile allowlists on both web and task workers. Device ownership and per-action authorization are still checked by Cloud.

## Verification

`python -m unittest test_driver test_native -v` checks caller queuing, offline rejection, coordinate bounds, cancellation and a real native Mobilerun workflow using a simulated phone/model. This is a contract test, not proof of real-device success. Release acceptance separately requires a caller-approved vision model, online Android phone and successful public MCP execution. Keep screenshots and capability links out of public evidence and source control.

See `overview.md` for user-facing tools, supported actions and limitations.

## Nexus task client

The deployed Cloud supports MCP version `2026-07-28` with its `io.modelcontextprotocol/tasks` extension. Include `_meta: {"io.modelcontextprotocol/clientCapabilities": {"extensions": {"io.modelcontextprotocol/tasks": {}}}}` in `tools/call` parameters for `mobilerun_run`. The response contains `resultType: task` and `taskId`; use `tasks/get` with the same capability metadata and authenticated caller. `tasks/cancel` requests cancellation. After starting a task, call `mobilerun_console` to receive its protected, single-use Interactive View URL. The custom HTML uses `nexus.onState` and `nexus.action`; the Agent pushes progress and a bounded phone preview with `view.set_state`. The cancel handler calls `view.cancel_task()` to revoke the originating task delegates through Cloud. No dedicated Mobilerun Cloud page or route is required. This Agent does not declare a Chat tool or expose a Private Display entry. This extension must be supported by the calling client; do not assume all MCP clients implement it. Ordinary synchronous multi-step calls can outlast proxy/client connection limits.

## Interrupted synchronous calls

Synchronous streams own a local cancellation signal. Closing the stream stops
new model/phone operations; terminal Cloud Run states also stop the worker.
The caller slot remains held until the worker finishes cleanup, so a replacement
task cannot overlap an in-flight phone action. `mobilerun_status` reports only
the authenticated caller's current Run ID. Background MCP Tasks retain their
independent lifetime when a client disconnects; cancel those through Tasks or
the Background console.

The workflow deadline is 600 seconds. The deployed streaming Relay path has no
fixed total stream duration; its idle timeout and bounded buffer limits remain independent.
Clients or other proxies may still impose shorter limits. A broken upstream
stream is returned as `MCP_STREAM_INTERRUPTED`; do not automatically replay
phone actions whose outcome may be unknown.


### Latency controls

`mobilerun_run` accepts optional `reasoning_effort`: `default`, `low`, `medium`, or `high`. The per-task Interactive View exposes the same selector and applies changes to subsequent model requests. Default omits the upstream parameter. Other choices are forwarded as OpenAI-compatible `reasoning_effort` without silent fallback.

`DecisionStateProvider` consumes the exact decision screenshot once rather than taking another image. Phone action attempts invalidate it, including actions that fail with uncertain outcome; the next decision always captures anew. The final observation also shares its own fresh image.

`MobileConsole` publishes on one background worker with one latest pending frame; intermediate preview frames may be coalesced. Model inputs, commands and results are not dropped. UI failures do not block phone control, and cancellation remains synchronous and caller-scoped. Task execution and stream-disconnect cleanup remain unchanged.

The model bridge preserves complete JSON responses, including opaque provider metadata. Responses larger than 1 MiB spool to temporary storage while reading, with a 64 MiB hard limit. Invalid JSON and local response/context/budget limits return HTTP 422 so the native model retry loop does not regenerate the same paid request. Transient transport failures remain retryable; the per-Run request budget still applies.

Task timing includes failed screenshot waits, capture attempts, failed captures, every model attempt, response byte counts, DNS/TCP/TLS phases and whether consecutive request payloads match. It never records credentials, prompts or model response content. Model request headers timing for non-streaming calls includes generation; it is not a queue measurement. Cloud Gateway timing separately records Router preflight, capacity checking and provider transport. Streaming probes can distinguish time to first text and subsequent output duration, but upstream queueing, prefill and hidden reasoning remain combined unless the provider exposes those measurements.

Run `python -m unittest test_driver test_native test_performance` in the built image. These tests include a complete 4 MiB response round trip, a bounded-response rejection, the real native retry loop rejecting HTTP 422 without another call, and accounting for failed screenshot waits. Real phone acceptance is separate.
