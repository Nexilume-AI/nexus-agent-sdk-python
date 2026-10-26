# Reuse a model connection across Agents

Hosted Nexus Cloud supports account-owned model connections and explicit batch Agent permissions at `/api/v1/model-connections/page/`. The page is also linked from Connect Agent and model authorization pages.

Sign in, save your own Nexus Router/model or external provider endpoint/model/key, choose expiry and total request allowance, then select Agents and explicitly confirm their per-Agent allowances. Saving a connection alone grants no Agent access. New tools and conversations for an approved Agent reuse its permission until expiry, quota exhaustion or revocation. Newly published Agents are never automatically included.

```python
grant = context.credentials.authorize("openai", wait_seconds=0, reuse_existing=True)
if grant["status"] != "authorized":
    return grant  # safe reference and approval URL; no secrets

# Trusted runtime only; never return or log this dictionary.
credentials = context.credentials.resolve_for_runtime(grant["authorization_id"])
# Initialize your provider client with the returned provider, base_url, model
# and secret. Do not hard-code the original provider URL.
```

For reusable connections, `secret` is a scoped Cloud proxy capability. The original upstream key stays in Cloud. OpenAI chat completions / responses and Anthropic messages support streaming through the proxy. Ordinary one-time browser authorization and explicit MCP-config preauthorization remain available; explicit MCP configuration takes priority.

Use `reuse_existing=True` without an explicit endpoint/model to accept the user's approved compatible configuration. Explicit endpoint/model requests must match the saved connection. Reuse never changes the server-verified owner or grants access to an unselected Agent. Personal Agent keys are mapped through their verified ownership binding; sharing the same key shares one caller identity.

Lifetimes are up to 30 days, bounded by the connection. Each dispatched model request consumes one total and one per-Agent request allowance, including upstream failures. Stream chunks do not consume additional requests. These limits are request counts, not monetary or token budgets. Permission edits retain usage counters. Revoke an individual Agent or the entire connection from the management page; old tokens never revive after reapproval. Already-dispatched upstream work may still incur charges.

A runtime capability lasts at most 24 hours. Call `authorize` again for later operations: Cloud can issue a fresh capability under the still-valid permission without asking the user again. Do not persist resolved credentials or expose them in prompts, logs, tool results, URLs or artifacts.

Connection revocation does not revoke independent older one-time grants. Manage those on their original authorization pages. This feature does not authorize phone/computer actions or broaden other Nexus permissions.
