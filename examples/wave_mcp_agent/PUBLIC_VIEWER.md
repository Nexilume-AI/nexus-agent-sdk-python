# Authenticated public waveform viewer

Wave MCP can return a browser link without exposing worker loopback ports. The
worker stays networkless; the bridge uploads selected FST outputs through the
existing private Run Files channel. Cloud stores a view bound to the server-verified
caller, tenant, project and Agent. One API Key is one caller across MCP sessions.

## Browser flow

1. `open_wave_view` selects one or two workspace waveforms, converts VCD when
   necessary, uploads immutable FST snapshots and returns `url` and `view_id`.
2. The URL fragment contains a random 256-bit ticket. It is valid for ten minutes
   and can be redeemed once. GET/prefetch does not redeem it. The page removes
   the fragment from browser history before POSTing it to the redemption endpoint.
3. Atomic redemption exchanges the ticket for a Secure, HttpOnly, SameSite=Strict
   cookie scoped to that view's URL path. The cookie grants no Nexus account,
   publisher, Router, API key, or unrelated file permissions. No Nexus login is
   needed to open the complete link. Treat the link as a secret until redeemed.
4. The authenticated page loads operator-installed Surfer WASM and the private
   FST. Signal/viewport/marker changes from `update_wave_view` arrive through SSE.
   The canvas reloads on a desired-state revision; intermediate states may be
   coalesced. Browser readiness is reported through `get_view_state`.
5. A browser session lasts at most one hour from view creation. Refresh works in
   that browser. `get_view_state` or `list_wave_views` issues a fresh ten-minute
   ticket within that hour; redeeming it replaces the previous browser session.
   `close_wave_view` or explicit workspace close revokes browser access.

An idle worker expiry/restart does not erase an already-uploaded Cloud snapshot;
its independently bounded one-hour expiry still applies. Revoking its caller API
Key or disabling the Agent also denies browser access. Files retain the existing
Run Files retention policy. The UI download action requires the same view cookie.

## Operator installation

Apply Agent migration `0066_agent_wave_view`. Redis must be available for event
push. Install matching, trusted `surfer.js` and `surfer_bg.wasm` in
`/opt/nexus-wave-viewer` (or configure Django `NEXUS_WAVE_ASSET_ROOT`). Set
`NEXUS_PUBLIC_BASE_URL` to the HTTPS Cloud origin. Keep the Surfer EUPL license,
source revision and build provenance alongside the assets. See the SDK Wave
example's `OPTIONALS.md` for pinned builds. Assets are not supplied by the Agent
and are not included as binaries in the Nexus repository.

Deploy the updated SDK `nexus_agent.wave_views` module and Wave bridge/worker
example, including its private `_nexus_export_wave_view` worker RPC. That RPC must
never appear in the public tool catalog. The five public viewer tools bring the
catalog to 45. Do not remove container isolation or open an inbound worker port.

## Boundaries and limits

- At most two files per view, 256 MiB each; worker storage remains 256 MiB total.
- Four views per workspace, eight unexpired views per caller/Agent scope.
- New view files must be ready output transfers from the authenticated invocation.
- Mutations and fresh links check the caller identity; workspace secrets alone
  do not permit another Key to read, update or close a view.
- No arbitrary proxy URLs, upstream authorization headers or raw executable
  startup commands are accepted. Cloud accepts only a small waveform command set.
- Browser SSE replay subscribes before reading persisted state; disconnects
  reconnect and replay the latest state, with bounded heartbeat expiry checks.
- `get_view_state` reports renderer readiness, not pixel-level proof that every
  requested signal exists. Unsupported signal names return a clear error.
- FSDB still requires the operator's compatible proprietary FsdbReader libraries.

## SDK

```python
from nexus_agent import current_run
from nexus_agent.wave_views import RunWaveViews

run = current_run()
output = run.files.upload('/private/output/result.fst')
viewer = RunWaveViews(run)
view = viewer.create([output['file_id']], {
    'panels': [{'label': 'Counter', 'commands': 'variable_add top.count;zoom_fit'}],
    'annotation': 'Counter waveform',
})
# Return view['url'] to the caller. Do not log its fragment.
viewer.action(view['public_view_id'], 'close')
```
