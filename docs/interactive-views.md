# Interactive Views

Interactive Views let any Agent create a private web page and receive browser
actions over an outbound SSE connection. They do not require model authorization,
a separate website, an inbound runtime port or an SSH tunnel.

## SDK

```python
from nexus_agent import current_run

def open_counter():
    count = 0

    def increment(arguments, view):
        nonlocal count
        count += arguments["amount"]
        view.set_state({"metrics": [{"label": "Count", "value": count}]})
        return {"count": count}

    view = current_run().views.create(
        title="Counter",
        state={"metrics": [{"label": "Count", "value": 0}]},
        actions={"increment": {
            "label": "Increment",
            "input_schema": {
                "type": "object",
                "properties": {"amount": {"type": "integer", "minimum": 1,
                                          "maximum": 10, "default": 1}},
                "required": ["amount"], "additionalProperties": False,
            },
        }},
        handlers={"increment": increment},
    )
    return view.public_info  # Only public browser metadata, never runtime credentials.
```

The active invocation supplies the verified caller identity. Keep the Agent
process alive after the MCP call returns. The SDK retains the handle through its
listener thread and invokes handlers on that thread. Handlers receive
`(arguments, view)` and should finish within 30 seconds. They have no implicit
`current_run()` context, model credentials or publisher account privileges.
Capture only resources belonging to this caller. Start a background job for long
work; use `view.set_state(...)` to publish updates.

`current_run().aio.views` follows the existing async Run API if using that
facade; `AsyncInteractiveViews` can also wrap `current_run().views`. Creation,
state updates, link renewal and close have async wrappers; action handlers remain
synchronous. Do not pass coroutine handlers.

The built-in `panel` renders optional `summary`, `metrics: [{label,value}]`,
`table: {columns,rows}`, `chart: {label,points}` and `logs` fields. It generates
forms for declared action parameters (strings, numbers, booleans, enums and JSON
objects/arrays). Other state is displayed as formatted JSON. State is replaced,
not implicitly merged, by `set_state`.

## Custom pages

Pass `html=Path("panel.html").read_text(encoding="utf-8")` to `views.create`.
Provide self-contained HTML with inline CSS/JavaScript and data-URL assets. The
first version does not host arbitrary directories, npm dependencies or external
resource URLs: bundle these into the HTML at build time (maximum 256 KiB).

```html
<button id="increment">Increment</button><output id="count">0</output>
<script>
nexus.onState(state => {
  document.getElementById("count").textContent = state.metrics[0].value;
});
document.getElementById("increment").onclick = async () => {
  const result = await nexus.action("increment", {amount: 1});
  console.log(result.count);
};
</script>
```

The platform injects `window.nexus`: `state`, `onState(callback)` (returns an
unsubscribe function), and `action(name, arguments)` (Promise of handler result).
The parent uses a per-frame MessageChannel and validates declared actions.
Custom HTML is served with an opaque-origin `sandbox allow-scripts allow-forms` policy, no
same-origin permission, no parent navigation/popups or network form submission, and a restrictive CSP.
It cannot read the parent's DOM, cookies or Nexus credentials. External fetches
and scripts are blocked. Page state, arguments and results are intentionally
visible to that page; never include secrets in them. CSP is also applied when the
custom URL is opened directly, outside the parent iframe.

## Access and lifecycle

- Existing server-derived `owner_scope` binds each view to the consumer, project
  and Agent. One API Key is one caller across MCP sessions; different Keys do not
  share views. The Personal edition uses its verified owner identity.
- Creation requires a running invocation and its interaction token. A separate
  hashed, view-scoped runtime token permits state delivery and actions after that
  invocation finishes. It cannot access another view or Nexus account APIs.
- Return `view.public_info` through MCP. It contains a fragment ticket valid for
  ten minutes, usable once. Redemption atomically consumes it and sets a scoped
  Secure/HttpOnly/SameSite cookie. Refresh works in that browser. The fragment is
  removed before subsequent requests. A newly redeemed ticket replaces the
  previous browser session.
- Renew through a new authenticated call: `current_run().views.link(view_id)`.
  Close via `view.close()` or `current_run().views.close(view_id)`.
- TTL is 60–3600 seconds, one hour by default. Closing, expiration, revoked caller
  credentials, disabled Agents or hidden Runs invalidate access. SSE checks this
  again on its ten-second heartbeat. No browser capability grants account access.
- A process restart loses its in-memory runtime handle. The page becomes
  disconnected and blocks new actions; reopen through MCP. SDK retries transport
  interruptions with bounded backoff while the process and view remain alive.

## Delivery semantics and limits

Browser actions persist before notification; Redis wakes both outbound streams.
Connections last up to 55 seconds and replay current state/pending actions on
reconnect. The browser uses EventSource, not a polling loop. A runtime heartbeat
older than 35 seconds marks it offline.

Each action has a UUID request ID. Repeated submits with the same ID and arguments
return its existing status. A transactional claim allows only one execution. A
lost claim response never executes the handler; an uncertain execution expires
instead of being automatically retried. Completion retries are idempotent and
do not repeat the handler. This avoids automatic duplicate side effects; it does
not promise exactly-once completion after a crash. Inspect state before manually
retrying. Handler exceptions return a generic error, not secret-bearing messages.

Limits: eight open views per caller/Agent, 32 declared actions, 16 pending/running
actions per view, 256 total actions per view, 128 KiB state, 32 KiB arguments and
results, 8 KiB input schemas. Pending actions expire after 120 seconds; claimed
actions expire after 60 seconds. Schemas cannot contain references or regex
patterns. Runtime updates use revision checks. The Agent must retain its existing
resource authorization and fair queue; view authorization does not bypass them.

## Deployment and Wave integration

Apply `0067_interactive_views`, deploy `interactive_*`, `view_security.py`, updated
Wave imports and callback routes, then restart the web service. Redis and the
existing HTTPS public base URL are required. No new proxy or public runtime port.

Install the matching SDK. The Wave example adds `open_wave_dashboard`, with
`template="panel"` or `"custom"`, using the generic SDK without special backend
code. Its file-list and signal-query actions use the existing caller-bound
workspace and fair queue. Existing Surfer/FST browser routes remain available;
large WASM assets continue through that specialized viewer. Each workspace keeps
at most two dashboards and closes them with the workspace.

Validation covers capability separation, ticket replay, caller isolation,
schema validation, queue limits, leases, completion retries, expiry and iframe
isolation; public end-to-end checks exercise both templates against the Agent.
