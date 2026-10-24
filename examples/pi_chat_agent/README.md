# Pi Private Display adapter

Native `@earendil-works/pi-coding-agent` 1.4.0 behind Nexus SDK Chat and MCP.
See [overview.md](overview.md) for user-facing behavior and limits.

## Deployment

Build `Dockerfile` as `nexus-pi-chat:1.4.0-computer1`. Install the current Nexus Python SDK on the host. Adapt the paths and OpenWrt registration addresses in `agent.py` and the systemd unit to your installation, then enable the unit.

Two containers are used per active turn: a trusted Pi SDK controller connects to a per-turn Unix model broker; an isolated, offline tool container executes native Pi tools. Only the controller mounts session state and the broker socket. The broker resolves and rechecks caller-owned model grants; provider secrets remain in host memory. Native resource discovery is disabled. Tool containers must never receive host mounts or broker credentials.

Cloud must support streamed Relay task collection. For the initial controlled rollout, add the Agent UUID to `NEXUS_AGENT_RELAY_CHAT_AGENTS` on both web and agent-task workers and to the existing credential-enabled Agent list. Other Agents retain their existing transport policy. Mobile interaction still requires its original supported transport.

Workspace identity uses verified `owner_scope` and Cloud Run ID. Never replace this with a caller-supplied identifier. A shared Nexus API key is one caller. Continuation is through the same Cloud Run, not a new independent MCP invocation.

New conversations request `ProviderCredentials.authorize(..., reuse_existing=True)` without a preset model. Cloud may reuse a valid grant only within the same caller, Agent and workspace scope; expiry and revocation still apply. Approval references are retained per caller/Run for continuation and revalidated by Cloud before resolution. Explicit MCP configuration preauthorization retains the SDK's precedence. An explicitly supplied `authorization_id` must still belong to the verified caller. Pending approval performs no model or code execution.

## Operations

The user service owns Docker resources labeled `nexus.pi=1`. Graceful shutdown removes its tool containers. State directories are private (umask 0077); no model secrets are persisted there. Idle cleanup removes sessions after one hour. Failed/cancelled turns discard their sandbox. Published immutable files are retained by Cloud.

Capture the built image digest and npm package lock when backing up a deployment. Do not include model credentials, authorization links, API keys, private Run state or chat content in source repositories.

## Attached Computer

Registration declares optional Computer access with files.list/read/write, command.execute and browser.control. Each turn receives only its effective scoped computer_* descriptors. The trusted broker routes this fixed tool set through the current NexusRunContext; it never accepts device IDs, delegate URLs or tokens from model input. Cloud validates the Run-bound delegate for each operation. Computer capabilities do not reach the offline code sandbox. Device operations share the per-turn tool budget; commands are limited to 60 seconds, are not retried after ambiguous failures, and appear in Private Display Shell.

With a Runtime advertising `workspace.select.v1`, the caller can browse any accessible absolute directory on their Computer in Run context → Workspace, then explicitly choose **Use folder**. Selection requires an idle Run and applies to its next turn. Cloud validates caller, Computer and current workspace, invalidates stale delegates, and adds the selected root to each signed command. The Runtime applies this root only while executing that command. Directory browsing alone grants no file access; Agent tools cannot select a new root. Operating-system access restrictions still apply. The working directory is not an OS sandbox for shell commands: `command.execute` runs with the Runtime user's permissions.

The six `computer_browser_*` tools support search, open, observe, click, fill and scroll. They use an isolated browser profile bound to the current Run on the attached Computer, without personal browser cookies. Actions use observed element refs and revisions; web content is untrusted. Screenshots appear in the Browser panel. Browser permissions must be granted by the caller. This does not enable desktop mouse or keyboard automation.

Cloud's default file storage must be writable by both the web service and task worker for `computer-runtime/` uploads and `agent-display/` screenshots. Keep these directories private and serve assets through authenticated Run endpoints, never as public static files. On existing deployments with an empty Django `MEDIA_ROOT`, these paths resolve under the service working directory; provision only those data directories with the service user's ownership rather than making the code directory writable.
