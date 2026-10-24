# Mobilerun Mobile Agent

Run the open-source Mobilerun framework on your own Android phone through Nexus MCP. Give a natural-language goal, approve your vision model and attach your phone. The native Mobilerun agent observes screenshots and performs mobile actions through the Nexus Mobile authorization system.

## Get started

1. Pair your Android phone with your Nexus account and keep Nexus Mobile online with the required accessibility and screen-capture permissions.
2. Use the Marketplace Mobile attachment control to bind your own phone. Use the same authenticated caller for your MCP requests.
3. Authorize your own Nexus Router or OpenAI-compatible vision model. Provider keys stay in the secure authorization flow, not chat or tool arguments.
4. Call `mobilerun_run` from your MCP client, for example with `message: "Press Home, open Android Settings and confirm it is visible."` Call `mobilerun_console` to open its protected Background page to follow progress and screenshots. Retrieve the final result through MCP and cancel the task when needed.

## MCP tools

- **mobilerun_console**: open or renew the caller-owned task console; live state and screenshot previews use the Interactive View SDK. Links are single-use and valid for 10 minutes; views expire after one hour.
- **mobilerun_status**: installed framework version, actions and limits; no model or phone operation.
- **authorize_mobilerun_model**: request or inspect caller-owned model authorization.
- **mobilerun_observe**: read the attached phone UI and capture a protected screenshot without calling a model.
- **mobilerun_run**: execute a native Mobilerun task using `message`, optional `max_steps` (1–20) an existing caller-owned `authorization_id`, and `reasoning_effort` (`default`, `low`, `medium`, `high`). `default` keeps the provider setting; explicit levels require provider support.

Supported actions include screenshot observation, taps, long presses, swipes, text entry, Home/Back and launching an Android package. Authenticated MCP invocation consents to the declared phone capabilities without an additional Cloud Mobile approval roundtrip. Explicit revocations remain enforced. Each Run uses the caller's phone and model authorization. There is no shared server phone or publisher model key. A shared Nexus API key represents one caller.

## Long-running MCP calls

Use a task-capable Nexus MCP client for `mobilerun_run`. Background task calls return a task ID, and retain their final result without requiring a long-lived client connection. Use `tasks/get` for the result and `tasks/cancel` to stop execution. A conventional synchronous MCP client may reach its connection timeout during multi-step work.

This Agent exposes MCP tools and a per-task Background console. It has no standalone Chat or Private Display entry. You can still describe your goal naturally in your MCP client's conversation.

## Limits

The initial Nexus integration supports Android, screenshot-based control and OpenAI-compatible vision models. It does not expose ADB connections, arbitrary shell commands, application installation, personal credential stores or iOS control. Text replacement via `clear=true` is unavailable. The upstream framework's other device backends are not exposed by this integration.

Tasks are limited to 20 steps and 10 minutes, with at most two active tasks and a bounded FIFO queue. Each caller may have one active or queued task. Phone availability and permission changes can interrupt a task. Third-party telemetry and persistent framework trajectories are disabled. This is a community integration, not Mobilerun Cloud.

The Agent is free; your model provider's usage charges apply.

## Responsive execution

Each decision uses one consistent screenshot for vision and UI state; a fresh image is captured after phone actions and for the final observation. Console previews and Background updates run asynchronously with a bounded latest-frame queue, so a slow display upload does not block the next model decision.

Choose `reasoning_effort` when calling `mobilerun_run`, or use **Model reasoning** in the task console. Console changes apply to the next model request, not an in-flight request. Low favors latency; higher levels may improve complex decisions at additional time and cost. Unsupported provider settings fail explicitly. The default preserves your model provider's existing behavior.

Results include total, screenshot and model-request timing, plus the reasoning levels actually requested. Timing varies with model provider load and phone connectivity; transport streaming alone does not make model generation faster.
