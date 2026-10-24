# Mobile access through MCP

Declare `mobile_requirement` and `mobile_capabilities` when creating your Agent,
and use each MCP tool's `mobile_scopes` to restrict its phone operations. Use the
Run context's Mobile interface; never accept device tokens, device addresses or
caller identity claims in tool arguments.

A compatible Relay carries the same signed Run context and Mobile delegation as
Direct IPv6. Cloud checks the registered node's `relay`, `runtime_context_v1`,
`invoke_interactions_v1`, `mcp_stream_v1` and `caller_mobile_v1` capabilities;
task tools also require `mcp_tasks_v1`. No per-Agent Mobile allowlist is needed.
Upgrade older connectors that advertise these capabilities only for Direct IPv6.

The caller pairs and selects their own Nexus phone. An authenticated MCP tool
invocation authorizes that Run's declared scopes without another Cloud Mobile
grant dialog or per-command approval. Explicit restrictions and revoked grants
remain effective. Device ownership, organization/project boundaries, expiry,
exclusive phone leases and cancellation are checked by the server.

A personal MCP key exported for this Agent can use its owner's existing phone
binding in the same organization/project. Cloud verifies the private credential
vault association; generic keys and external-user identities cannot inherit a
user's phone. Sharing that personal key also shares its phone access. This Mobile
association does not share the owner's workspace or model authorization. Key
revocation and both owner/key Mobile restrictions remain enforced.

Android permissions must already be enabled on the phone. Model API credentials
are authorized separately through the existing secure model flow. Private Display
and direct device MCP keep their existing approval policy.

Use MCP Tasks for long-running operations and an Interactive View for progress,
screenshots and cancellation. Do not return raw delegate tokens or phone pairing
credentials to the model or browser.
