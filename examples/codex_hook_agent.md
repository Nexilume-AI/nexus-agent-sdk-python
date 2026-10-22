# Use a Nexus Agent as a Codex Hook

Codex is the client and event producer. The Agent is a remote MCP service, not a
replacement Codex harness. `codex_hook_agent.py` is a deterministic, device-free
example; it does not use an LLM, read files, execute commands or provide a real
security scanner.

## Deploy and connect

1. Install `pip install "nexilume[fastmcp]"` if running locally. For Cloud Docker
   hosting, upload `codex_hook_agent.py` with entrypoint `agent` using the normal
   Python upload flow. Use an SDK build containing `CodexHook`.
   Older OpenWrt connectors do not forward Hook declarations; use Cloud Docker
   hosting for this copy-ready flow until the connector supports that metadata.
2. In **Connect Agent**, select **Codex → Codex Hook**. The deployed SDK declarations
   are shown read-only; users do not select events or write JSON. Existing clients' tool-call setup is
   unchanged. Grant only the Agent access needed by the hook; no Computer or
   Mobile grants are needed for this example.
3. Review the declared triggers and data transfer, then copy the complete generated TOML. Merge
   the MCP server and hook entries into your Codex configuration; do not replace
   existing servers or hooks, or append duplicate server tables. Keep API keys
   outside source control. The generator references `NEXUS_API_KEY` by default.
4. Connect the MCP server, then review and trust the hook definition in Codex.
   Installing MCP alone does not enable automatic invocation. Check the actual
   client version supports MCP hooks before relying on them.

## Declare hooks once, in the SDK

The Agent author owns event selection, matchers, timeouts and input mappings:

```python
from nexus_agent import CodexHook, McpToolDescriptor

tool = McpToolDescriptor(
    name="check_completion",
    input_schema={"type": "object", "properties": {"active": {"type": "boolean"}},
                  "required": ["active"]},
    codex_hooks=(CodexHook(
        event="Stop",
        input={"active": "${stop_hook_active}"},
        timeout=30,
    ),),
)
```

For FastMCP-style tools, use `@server.tool(codex_hooks=(CodexHook(...),))`
with `NexusMCPServer` from `nexus_agent.fastmcp`. Redeploy after changing a
declaration. Cloud exports only the active deployment, never a pending build.
Agents without declarations provide ordinary MCP only: the Codex Hook entry is
hidden, with no warning or retry prompt. Invalid published declarations remain
visible as configuration errors.

Supported MCP events: `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
`PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`,
`SubagentStart`, `SubagentStop`, `Stop`, `Interrupt`.
`SessionEnd` is command-only in Codex and is rejected by the SDK.
`Interrupt` defaults to one second and permits at most three seconds.
Other events default to 30 seconds, with a 600-second SDK ceiling.
Do not supply matchers for `UserPromptSubmit`, `Stop` or `Interrupt`.

The example publishes four tools covering all eleven events. Its eight passive
lifecycle hooks send only the event name and return `{}`; they do not grant
permissions or forward prompts. The three functional mappings are:

| Event | Tool | Matcher | SDK input mapping |
|---|---|---|---|
| `PostToolUse` | `codex_review_patch` | apply_patch or the demo command tool | `{"patch":"${tool_input.command}"}` |
| `PreToolUse` | `codex_check_command` | Bash or exec_command | `{"command":"${tool_input.command}"}` |
| `Stop` | `codex_check_completion` | None | `{"stop_hook_active":"${stop_hook_active}"}` |

A complete-value placeholder retains the runtime JSON type: the Stop mapping
receives a boolean, not a string. Only map fields available for the chosen event.
SDK and Cloud check JSON/object shape and required names; the server still
validates actual argument values against the deployed tool schema.

## Acceptance

Use a temporary Codex profile/workspace and synthetic input, not production code.
Confirm the connected tool catalog contains all four example tools and eleven
SDK-declared hooks. Exercise:

- Patch review: a tiny synthetic patch produces `NEXUS_HOOK_PATCH_REVIEW` context.
- Pre-tool check: `echo NEXUS_HOOK_DENY_DEMO` produces a structured denial. A normal
  harmless command provides context without overriding normal Codex approvals.
- Completion: the Stop result includes `NEXUS_HOOK_COMPLETION_CHECKED`; an already
  active Stop hook returns `{}` to avoid a continuation loop.
- Invalid input, invalid/revoked credentials, disconnected server and timeout:
  record the actual errors, do not claim the operation was blocked by an outage.

Checking `tools/call` alone proves MCP connectivity and result shape, **not** that
Codex invoked a hook. Require native `hook/started` and `hook/completed` events (or
equivalent client evidence) for lifecycle acceptance. Remove the temporary hook
configuration and revoke the test key afterwards.

### Verified integration

The example was exercised with official Codex CLI 0.155.1 through an existing
Nexus Cloud instance to a real Docker-hosted SDK Agent. Acceptance verified a
native PreToolUse denial, PostToolUse context after a successful MCP call, Stop
callback, five direct tool-result cases, and rejection of the revoked test key.
The test used a deterministic local Responses transport to drive Codex events:
it did **not** use paid model inference or certify model reasoning quality.
The SDK-declared copy flow was also verified: Cloud exported four tools with
eleven hook declarations, and Codex accepted all eleven without configuration
errors. Native execution was verified for the three functional events above;
the other events' full lifecycle triggers have not all been exercised.

Only the isolated test profile pre-approved the synthetic, device-free tool and
bypassed hook trust for that invocation. The generated production configuration
does neither: users must review and trust their own hooks in Codex. Test accounts
were disabled, the test Agent retired and its container removed afterwards.

## Result contract and boundaries

The example returns `McpToolResult` with the same JSON object in `structuredContent`
and a text block. This preserves Codex's hook output contract across Nexus
transports without an extra business-result wrapper. A generic Chat reply is not
a hook decision. `PostToolUse` cannot undo completed changes.

MCP hooks wait synchronously and use an already-connected server. Connection
failure, timeout or malformed results can fail open in Codex; they are not a
security enforcement boundary. Nexus still enforces caller, organization, tool
and device access. Automatic invocations may incur published Agent fees.
SessionStart may run before the MCP server is connected; do not rely on it for
mandatory initialization. Supporting an event declaration is not proof that
every client version triggers it: verify lifecycle events with your client.

Only explicitly mapped data is transmitted. A path on the Codex host is not a
file upload and does not authorize device access. Do not forward entire
transcripts, secrets or business patches without consent. Keep mappings narrow
and timeout budgets short; avoid repeating external side effects on retry.

Reference: [official Codex Hooks documentation](https://learn.chatgpt.com/docs/hooks).
