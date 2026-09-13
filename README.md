# nexus-agent-sdk-python

## 安装与开始使用 / Install and start

发行名称为 **nexus-openwrt-agent-sdk**，Python 导入仍是 `nexus_agent`。
PyPI 上的 `nexus-agent-sdk` 属于其他项目，请勿用它安装本 SDK。

从 [GitHub Releases](https://github.com/Nexilume-AI/nexus-agent-sdk-python/releases) 下载 wheel，在虚拟环境中安装：

```sh
python -m venv .venv
# Linux / macOS:
. .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install ./nexus_openwrt_agent_sdk-0.46.2-py3-none-any.whl
python -c "import nexus_agent; print(nexus_agent.__version__)"
```

或从源码安装（构建需要 Python 3.10+）：

```sh
git clone https://github.com/Nexilume-AI/nexus-agent-sdk-python.git
cd nexus-agent-sdk-python
python -m pip install .
python examples/router_echo_agent.py --router http://192.168.246.1:7446 --self-test
```

请将路由器地址替换为你的 Nexus OpenWrt Agent Access Proxy 地址，并先开启 Agent 服务。
默认 `--auth auto` 使用已配置的认证；`--auth none` 仅适用于明确配置的无 JWT 隔离 LAN 入口。
SDK 本身不安装 OpenWrt、Cloud 或 Relay 服务器。Cloud 功能需要路由器完成配对和 Relay 连通。

当前发布渠道为 GitHub Release。PyPI 完成首次发布后，才可使用
`python -m pip install nexus-openwrt-agent-sdk==0.46.2`。
下文涉及 extras 的 PyPI 命令在此之前应改为从此仓库安装，例如 `python -m pip install '.[fastmcp]'`。

Linux 也可用 `sh install-linux.sh --wheel /path/to/downloaded.whl --install-only` 安装 CLI；
省略 `--install-only` 会进入 IPv6 配置流程，需要自行确认网络接口并按提示授权系统更改。

SDK supports OpenWrt edge agents and hosted MCP runtimes. The core wheel is dependency-free;
optional integrations have their own Python and dependency requirements. Install the GitHub wheel
or this source checkout until the PyPI release is available. See the examples below for the APIs.


License: [Apache-2.0](LICENSE). This applies to this SDK package, not the private
Nexus Enterprise distribution. Third-party dependencies retain their own licenses.

## Installation and source builds

The dependency-free core wheel retains Python 3.9+ support. Building this package
from source is a separate requirement: use Python 3.10+ and setuptools 83.x,
as declared in `pyproject.toml`. Do not downgrade the build backend to build on
Python 3.9; use the wheel or a newer build interpreter instead. Optional extras
also have their own Python, platform and external-program requirements.

Release archives contain only this standalone SDK. See [CONTRIBUTING.md](CONTRIBUTING.md) for tests and builds, and [RELEASING.md](RELEASING.md) for publishing.

基础 wheel 仍支持 Python 3.9+，且不增加核心运行依赖。源码构建需 Python 3.10+
及 setuptools 83.x；Python 3.9 用户应使用 wheel，或使用较新的解释器构建，
不要降低构建工具版本。可选扩展的解释器、平台与外部程序要求另行适用。

## One source, OpenWrt and hosted Docker (0.46.0)

[`examples/dual_runtime_agent.py`](examples/dual_runtime_agent.py) is the same
file in both environments. It uses a top-level `NexusAgent`, decorated
capabilities, Plan and interactive Chat. No Router or Cloud credentials belong
in the source.

- **Edge:** install the SDK and run `python dual_runtime_agent.py`. The default
  `runtime="auto"` retains Router discovery, registration and lease renewal.
- **Cloud:** upload that unchanged file in **Agent → Runtime → Upload Python**,
  build, then explicitly deploy the verified version. The trusted launcher sets
  `NEXUS_AGENT_RUNTIME_MODE=hosted` before import, converts the decorated
  capabilities into real FastMCP Streamable HTTP tools at `/mcp`, and never
  discovers/authenticates with a Router or opens an edge listener.

Keep `agent.run()` behind `if __name__ == "__main__":`; module-level code should
only declare the Agent and tools. Factory-only scripts and instances created
inside `main()` must be moved to top level before upload. A script that explicitly
forces `runtime="openwrt"` is edge-only. A failed Router discovery never silently
falls back to hosted mode.

The Cloud Python base profile must contain SDK 0.46.0+ with `[fastmcp]` (Python
3.12 in the managed profile); an older profile produces
`PYTHON_PROFILE_UPGRADE_REQUIRED`. Core/edge installation remains zero mandatory
dependencies and Python 3.9+. For a self-managed MCP process, install `[fastmcp]`,
set hosted mode before importing the file, and use `agent.run()` or
`agent.as_mcp_server()`. Hosting alone does not provide Nexus caller credentials
or grant resource access: Run context comes from the trusted Nexus gateway.

Synchronous/async functions and synchronous/async stream handlers share the same
business handlers and `NexusRunContext`. MCP export preserves explicit schemas,
Task/continuable/interactive policy, execution profiles, input modalities and
resource declarations. A stream's `result` becomes the MCP tool result; use
`ctx.plan`, `ctx.chat`, `ctx.trace` and other reporters for Display feedback.
Verification only initializes MCP and lists tools; it never invokes handlers.

Computer/Mobile declarations become active only when the candidate successfully
deploys. Callers still attach their own resources and explicitly authorize them.
External model credentials, extra packages, browser binaries and other business
dependencies still need deployment configuration. This does not merge an
OpenWrt-managed Agent and a Docker-managed Agent into one Cloud resource or
provide automatic failover between them.

## Private Display execution controls (0.43.0)

Published MCP tools can expose bounded slash commands, raw audio input, and model execution profiles. Nexus validates the caller selection against the signed tool manifest and exposes it on `ctx.execution`.

```python
from nexus_agent import NexusExecutionProfile
from nexus_agent.fastmcp import CurrentNexusMCP, NexusMCPServer

server = NexusMCPServer("Research Agent")

@server.tool(
    task=True,
    slash_command="inspect",
    slash_description="Inspect attached Run files",
    input_modalities=("text", "audio"),
    execution_profiles=(NexusExecutionProfile(
        id="balanced", label="Balanced", model="gpt-5",
        is_default=True,
        reasoning_efforts=("low", "medium", "high"),
        default_reasoning_effort="medium", context_window=400_000,
    ),),
)
async def inspect(content: str, audio: list[dict], nexus=CurrentNexusMCP()):
    result = await call_model(content, profile=nexus.run.execution)
    await nexus.run.aio.usage.report(
        model=result.model,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cached_input_tokens=result.usage.cached_input_tokens,
        reasoning_tokens=result.usage.reasoning_tokens,
        context_window=400_000,
    )
    return result.text
```

Declare at most one `is_default=True` profile. If an older manifest does not
declare one, Nexus keeps backward compatibility by treating the first profile
as the publisher default. An empty `reasoning_efforts` tuple means the model
manages reasoning internally; it does not make the profile unusable.

Run input files, including Computer imports and recordings, are exposed through `ctx.input.files` and `ctx.input.audio`. Usage must come from the real model response; the SDK never estimates tokens.

For a dependency-free OpenWrt example that enables both **Attach file** and
**Voice** in Private Display, verifies the private byte streams, and publishes a
downloadable manifest, run
[`examples/router_file_audio_agent.py`](examples/router_file_audio_agent.py).
It defaults to `router="auto"` and requires no Cloud password in source code.

When the Agent calls the Nexus model gateway, pass
`headers=ctx.usage.gateway_headers()` with the request. Nexus then attributes the
provider-reported usage to this Run automatically. The returned headers contain a
short-lived secret: do not log, persist, or return them. External model clients
should instead call `ctx.usage.report()` with their actual response usage.

## Cooperative follow-up (0.42.0)

Private Display supports Cloud-managed **Queue next turn** for continuable tools.
Opt in to **Guide current turn** on a handler with
`@agent.capability(..., follow_up="steer_and_queue")`. Use a continuable Chat MCP
descriptor and annotate the context parameter as `NexusRunContext`.

At safe points, consume `await ctx.aio.inbox.receive_pending()` and call
`await instruction.acknowledge()` after incorporating each instruction (or
`await instruction.reject()`). Synchronous `ctx.inbox` is also available. The SDK
receives in the background; it does not run a second handler or cancel an already
issued command. No new Agent listener or OpenWrt firmware protocol is needed.
Explicit opt-in requires Cloud inbox protocol v1 and fails clearly on older Cloud.

See `examples/router_follow_up_agent.py`. Cloud migration and the existing Agent
task worker must be deployed before using these controls. Queue messages are
bounded to 20 pending messages and 24 hours. Failed or
cancelled turns do not automatically start queued work. Each queued turn repeats
authorization and billing checks. SDK input acknowledgement is idempotent, but
applications must handle possible redelivery before acknowledgement by message ID.

With the follow-up attachment migration deployed, Queue accepts up to four image
references and eight file references when the tool declares those input fields.
Cloud snapshots images into the same private Run when accepting the message;
expiry of the original temporary upload does not invalidate an accepted queue.
Attachments are passed through the normal tool arguments in the next turn.
Files are marked consumed only when delivered, not when queued.

Guidance attachments require **an additional explicit handler opt-in**:

```python
await ctx.aio.inbox.configure("steer_and_queue", attachments=True)
for instruction in await ctx.aio.inbox.receive_pending():
    # Consume these at an application-defined safe point, not in a receiver thread.
    for reference in instruction.attachments:
        image_bytes = await ctx.aio.media.read_image(reference)
        # Incorporate the image into your application/model input here.
    for reference in instruction.files:
        # ctx.aio.files.download(reference, application_chosen_destination)
        # reads with this Run's delegate and existing Cloud TLS policy.
        pass
    # Acknowledge only after actually incorporating the text AND all attachments.
```

`instruction.attachments` and `instruction.files` are tuples of protected Run
references; they contain no bearer tokens or public storage URLs. Configure
without `attachments=True` to retain text-only guidance. Older Cloud returns an
explicit unsupported error instead of silently discarding attachments. Browser
drafts persist IDs only; retries preserve the original message/attachment envelope.

## Private large files (0.41.0)

Hosted Agents can stream caller inputs with `ctx.files.download(ref, destination)`
and publish immutable output snapshots with `ctx.output.upload_file(path)`.
Async equivalents are available under `ctx.aio.files` and `ctx.aio.output`.
The default per-file maximum is 5 GiB; transfers use 1 MiB hashed chunks,
resumable offsets and bounded-memory downloads, not MCP Base64 messages.

Declare `files` as an array in the tool's input schema and implement its handling.
See [the complete file Agent example](examples/router_file_agent.py), the
[combined file and audio example](examples/router_file_audio_agent.py), and
the connected server’s file API, limits and security policy.
Only the current caller/Run can read input references. Output upload success does
not mean the file is safe to execute or cleared for Data Asset publication.

## Long-running hosted calls (0.40.0)

Use MCP Tasks or Private Display for multi-minute work. Nexus owns the persistent
queue, deadline, lease and billing lifecycle. Check cancellation between external
actions with `ctx.raise_if_cancelled()` or `await ctx.aio.raise_if_cancelled()`;
`ctx.control()` reports the remaining deadline and cancellation state. A cancelled
HTTP transport does not prove that a remote side effect stopped.

Optional crash-recoverable event buffering is enabled with
`NexusReportingConfig(outbox_directory="/private-volume/agui")` or
`NEXUS_AGENT_OUTBOX_DIR`. Mount persistent private storage (and restrict Windows
ACLs); without a volume, container replacement loses these files. The default
capacity is 8 MiB and 10,000 records. Only explicit event metadata and stable event
IDs are stored, never transport tokens. Do not report secrets or sensitive prompts.

Call `ctx.replay_pending()` after connectivity recovers while the **same Run** is
still active; creating a new context for that Run also replays pending files.
`ctx.report().buffered` exposes the backlog. Completed or fenced Runs cannot be
reopened by the outbox, and capacity/flush failures remain fail-open. Operators
must monitor delivery reports and manage outbox retention. Upgrade/redeploy Agent
images to use these APIs; a Cloud server upgrade does not update image SDKs.

## Nexus Computer Runtime (0.39.0)

Install the outbound, caller-owned Computer Runtime with optional local browser support:

```bash
pip install "nexus-openwrt-agent-sdk[computer,browser]"
nexus-computer setup "<pairing-url-from-nexus-cloud>"
nexus-computer status
```

The `computer` extra requires cryptography 50.0.1 or newer within major version
50; the dependency-free Agent core is unchanged. Use a supported 64-bit Python.
Upstream no longer provides Intel macOS wheels for this cryptography line, so
Intel macOS installation needs an independently verified source-build toolchain.
Do not downgrade cryptography to bypass this requirement. This release's Windows
checks do not establish macOS device acceptance.

The Runtime runs as the current operating-system user and opens an outbound WSS connection to Nexus Cloud. No inbound SSH port, public address, Cloud token, or Agent credential is required. Workspace, Terminal, Browser, and Tool Setup remain bounded by the paired Workspace root and the scopes approved by the caller in Marketplace. Completed command results are kept in a bounded, device-key-encrypted journal so reconnect delivery is idempotent without writing terminal or browser data to plaintext logs.

Recoverable Tool Setup advertises `tool_setup.cas.v2`. Its configuration writes
and recovery barriers share a local profile lock and a durable, device-bound
sequence record beside the Codex configuration. Once a barrier is acknowledged,
older fenced writes are refused even after Runtime restart; the record contains
only the device identity and sequence, not credentials or configuration content.
Do not delete this record to retry an unconfirmed write. Recovery must first read
and verify the current file. Legacy Tool Setup operations remain compatible but
do not provide this fencing guarantee. This lock does not prevent a local user
or editor from deliberately replacing files outside the Runtime protocol.

The same Runtime can be paired into multiple Nexus Workspaces or Projects. Run
`nexus-computer setup` once for every pairing link. Each registration receives a
different device key, Workspace root, encrypted command journal and independent
Cloud reconnect loop; adding a registration never replaces an existing one.

```bash
nexus-computer setup "<pairing-link-for-workspace-a>"
nexus-computer setup "<pairing-link-for-workspace-b>"
nexus-computer status
nexus-computer logs --registration <registration-id>
nexus-computer unpair --registration <registration-id>
```

`status` reports the service state plus every registration's `connected`,
`connecting` or `reconnecting` state. A Cloud outage in one Workspace backs off
independently while healthy registrations stay online. `repair` is safe to run
against an already-running service, and `restart` verifies that the background
process actually becomes ready instead of reporting success after only asking the
operating system to start it.

Private or local Nexus Cloud deployments deliver their public CA trust anchor inside the authenticated pairing link. `nexus-computer setup` validates and stores that public certificate automatically in the Runtime's protected configuration directory; users do not download or install a certificate. Public-CA deployments continue to use the operating system trust store. `--ca-file` remains an administrator override for compatibility and controlled recovery.

## Attached Computer browser for Private Display (0.37.0)

An OpenWrt-managed browser Agent can request only `browser.control` and drive
an isolated Chrome or Edge profile on the caller's Attached Computer. The SDK
uses the Run-scoped Cloud delegate and never falls back to Chrome on the Python
Agent host:

```python
agent = NexusAgent(
    router="auto",
    cloud_publish=True,
    computer_requirement="required",
    workspace_capabilities=("browser.control",),
)

@agent.capability("browser.chat", tool=McpToolDescriptor(
    name="browser_chat",
    chat=True,
    task=True,
    interactive=True,
    continuable=True,
))
def browser_chat(payload, ctx):
    browser = ctx.browser.attached_session()
    observation = browser.open(payload["url"])
    ctx.chat.say(f"Opened {observation.title}")
```

The caller grants `browser.control` and selects a real SSH Computer in the
Marketplace before starting Private Display. Cloud exposes CDP only through a
loopback SSH tunnel and publishes protected observations to the Browser panel.
`examples/browser_session_agent.py` provides the Chat entry point plus the
compatible structured MCP tool. With SDK 0.46.0+, the same file can be uploaded
unchanged through **Upload Python**; leave the instance name blank (auto-detected
as `agent`). Its module-level hosted declaration does not discover a Router,
parse command-line arguments or open an edge listener. The edge `main()` and
`build_agent()` still accept the existing Router, port, TLS, lease and ready-file
options, and reuse the same tool and resource declarations. With no Router URL,
the SDK uses auto-discovery / `NEXUS_ROUTER_URL` instead of a fixed LAN address.

Build and explicitly deploy the uploaded version using a Python profile with
SDK 0.46.0+. Then attach an online Caller Computer and grant `browser.control`
before starting Private Display. Chrome remains on the Attached Computer, not
in the Agent container; no Chrome installation in that container is required.
The sample uses deterministic commands such as “打开 https://example.com”,
“Observe”, “Scroll down” and “Click Continue”; it is not a general-purpose LLM
browser planner.

## Caller-owned Mobile on OpenWrt (0.36.0)

OpenWrt Direct IPv6 Agents can declare the Mobile contract that Nexus Cloud
must enforce before creating a private Run. The caller grants the declared
scopes and attaches one of their own paired phones in Private Display; the
Agent developer never binds or receives the phone credential.

```python
from nexus_agent import MOBILE_SCOPES, McpToolDescriptor, NexusAgent

agent = NexusAgent(
    router="auto",
    cloud_publish=True,
    mobile_requirement="required",
    mobile_capabilities=MOBILE_SCOPES,
)

tool = McpToolDescriptor(
    name="mobile.validate",
    task=True,
    interactive=True,
    mobile_scopes=MOBILE_SCOPES,
)
```

See `examples/edge_caller_mobile_agent.py` for Plan, observation, protected
screenshots, mandatory high-risk text confirmation and a user-facing result.
Relay v1 and Router-to-Router Direct Invoke do not receive Caller Mobile.

## Run-isolated Chrome browser loop (0.35.0)

Install the optional Playwright driver on the Python Agent Serving host. The
SDK does not download a browser at Agent startup; it uses
`NEXUS_BROWSER_EXECUTABLE`, system Google Chrome/Chromium, or an already
installed Playwright Chromium, in that order.

```bash
python -m pip install "nexus-openwrt-agent-sdk[browser]"
```

Each Nexus Run receives an isolated Chrome BrowserContext. Re-entering the
same Run reuses its page and cookies, while a different Run receives a clean
context. Every navigation or action returns a fresh observation and
automatically publishes its protected viewport image to Private Display.
Semantic DOM and sanitized HTML remain in the Agent Serving process.

```python
browser = ctx.browser.session(viewport=(1280, 720))
observation = browser.open("https://example.com")

observation = browser.locator("#name").fill("Nexus")
observation = browser.locator("#model").select("gpt-5")
observation = browser.locator("#submit").click()

observation = browser.click(x=420, y=360)
observation = browser.scroll(delta_y=600)
observation = browser.type("Hello")
observation = browser.drag(
    from_x=200, from_y=300,
    to_x=600, to_y=300,
)
```

Model integrations stay outside the SDK. They receive only the local image
and semantic DOM and return a structured `NexusBrowserAction`:

```python
while True:
    action = agent_model_decision(
        image=observation.image,
        dom=observation.dom,
    )
    if action.kind == "done":
        break
    observation = browser.perform(action)
```

Element refs are bound to an observation revision. Reusing a ref after the
page changes raises `NexusBrowserStaleObservation` instead of clicking a
different element. `observation.html()` returns bounded, sanitized HTML with
scripts, styles, event attributes and sensitive form values removed. Browser
exceptions and object representations do not include typed values, DOM text,
cookies, credentials, or tokens. Async handlers use the identical API through
`ctx.aio.browser.session()`.

For a runnable Cloud-connected example, use `examples/browser_session_agent.py`
on OpenWrt or upload it for Docker hosting (SDK 0.46.0+). That example uses
`attached_session()` on the caller's Computer, not the serving-host `session()`
shown above; it requires no Cloud password parameters in the source.

## Platform-managed recovery (0.45.0)

SDK 0.45 journals Nexus-managed side effects for each Run Turn. When a Docker
container or OpenWrt Python process disappears, Nexus fences the old lease and
re-invokes the same handler. Completed journal entries return their saved result;
an external operation with an unknown outcome pauses for the caller instead of
being replayed automatically.

```python
if ctx.recovery.is_replay:
    ctx.chat.say(f"Recovering attempt {ctx.recovery.attempt}")

with ctx.recovery.external_operation(
    "crm.create-ticket",
    {"customer_id": customer_id},
    can_reconcile=False,
) as operation:
    if operation.execute:
        ticket = create_ticket(idempotency_key=operation.idempotency_key)
        operation.complete({"ticket_id": ticket.id})
```

`ctx.recovery.managed` reports the negotiated guarantee. Raw HTTP, direct
database writes, subprocesses and third-party SDK calls remain outside the
guarantee unless wrapped with an idempotency key or a status reconciliation
strategy. This mechanism restores logical execution, not Python stack frames or
local variables. `continuable=True` only enables another Turn after completion.

## Run-only task context (0.44.0)

When a caller keeps the same Private Display Run selected, their next message
re-enters the Agent handler with the same `ctx.run_id`. Nexus preserves the
Run's messages, checkpoint, Files, Computer, Plan, and Output context instead
of creating another history item. `ctx.turn_index` starts at `1`, increments on
each resumed input, and `ctx.is_resumed` reports whether this is a continuation.

```python
@agent.capability("assistant")
def assistant(payload, ctx: NexusRunContext):
    previous = ctx.run.messages() if ctx.is_resumed else []
    ctx.chat.say(
        f"Run {ctx.run_id}, turn {ctx.turn_index}; "
        f"received {payload['message']!r} after {len(previous)} messages. "
        f"Project revision: {ctx.project.revision}."
    )
```

`ctx.run.messages()` and `ctx.aio.run.messages()` only return caller-visible
messages from the current Run. `ctx.project.id`, `name`, `instructions`, and
`revision` expose the immutable Project snapshot captured when the Run started.
Nexus does not append it to a model prompt. These APIs never read another Run or
reintroduce a cross-Run Thread. Reusing a stable `ctx.chat.ask(key=...)` is safe because the
SDK namespaces the interaction key by the current turn while retaining
idempotency for retries of that turn.

## Zero-configuration Cloud Run TLS trust (0.31.0)

An enrolled trusted-LAN Router now returns its Nexus Cloud TLS trust policy in
the source-bound bootstrap response. The SDK keeps a dedicated `SSLContext` in
memory and uses it for Run context exchange plus Plan, Chat, Terminal,
Workspace, Files, Mobile, and event APIs. Private Display Agents therefore do
not need `SSL_CERT_FILE`, do not install a host CA, and never disable certificate
or hostname verification.

Publicly trusted Cloud deployments use the system trust store. Advanced remote
deployments that cannot use LAN bootstrap may pass `cloud_ca_file=` explicitly;
this override takes precedence over router-provided trust. Older Routers remain
compatible and continue to use Python's system/environment trust configuration.

## OpenWrt zero-configuration Cloud registration (0.29.0)

On a trusted LAN, an Agent only declares its router and business identity. It
does not need a LAN token, OIDC client credentials, a Nexus Cloud URL, or a
Cloud token. The OpenWrt Router must first be enrolled once with a pairing code;
the router then keeps the Cloud device identity and publishes lease-bound MCP
tools on the Agent's behalf.

```python
from nexus_agent import NexusAgent

agent = NexusAgent(
    router="http://192.168.250.1:7446",
    tenant="demo",
    agent_id="echo-agent",
    cloud_publish=True,
)

@agent.capability("demo.echo")
def echo(payload: dict) -> dict:
    """Return the input unchanged."""
    return payload

with agent.start() as handle:
    cloud = handle.wait_for_cloud(timeout=120)
    print(cloud.agent_id, cloud.mcp_url, cloud.transport)
    handle.wait()
```

`start()` succeeds as soon as the LAN route is registered. Cloud enrollment or
transport failures remain visible through `handle.cloud_status()` and
`handle.wait_for_cloud()` without taking the LAN Agent down. By default the
router chooses Direct IPv6 when its public endpoint is ready and falls back to
an online enrolled Relay; `public_ipv6=True` and `False` explicitly force or
disable the Direct request. Pass `tool=False` to keep a capability LAN-only, or
provide `McpToolDescriptor` to replace the descriptor inferred from the intent,
function docstring, and annotated payload type.

The resulting Agent is private, unpublished, and callable through
`/api/v1/agents/<agent_id>/mcp/`. Cloud ownership and tenant/project placement
come only from the enrolled router, never from SDK-supplied credentials.

## Caller-private Mobile delegation (0.30.0)

A hosted Agent declares Mobile scopes, while each caller pairs and authorizes
their own Android device in Nexus. Nexus fixes one caller-owned binding to each
Run and gives the container a short-lived delegate token; the Agent never sees
another device, the caller JWT/API key, or the Android pairing token.

```python
from nexus_agent import NexusAgent, NexusRunContext

agent = NexusAgent(...)

@agent.capability(
    "inspect-mobile",
    mobile_scopes=("mobile.observe", "mobile.screen.capture", "mobile.tap"),
)
def inspect_mobile(payload: dict, ctx: NexusRunContext):
    if not ctx.mobile.enabled:
        return {"mobile": "not attached"}
    observation = ctx.mobile.observe()
    screen = ctx.mobile.capture_screen()
    ctx.browser.frame(image=screen.content, title="Caller mobile")
    ctx.mobile.tap(x=420, y=860)  # pixel coordinates; 0..1 is also supported
    return {"node_count": len(observation.data.get("nodes", []))}
```

FastMCP tools declare the same scopes with `@server.tool(mobile_scopes={...})`
and call the async API through `await nexus.mobile.observe()`. High-risk
actions such as text input always require the current caller's approval in the
private Display, MCP Elicitation, or MCP Task, even when the device's direct
control policy is automatic. Mobile content is never copied automatically to
Trace, Memory, Output, or Data Assets.

Docker and Nexus Cloud MCP to OpenWrt IPv6 support this delegation. Relay v1
requires IPv6 for a Mobile-required Agent, and router-to-router Direct Invoke
does not attach a Nexus caller device.

## OpenWrt interactive Invoke and cloud MCP (0.28.0)

The same Plan, Shell, Browser, Chat, Trace, Memory, and Output helpers now run
in Docker, a Nexus-managed OpenWrt IPv6 call, or a private router-to-router
Direct Invoke. Relay v1 remains available for ordinary short calls, but Nexus
rejects an interactive Relay tool with `OPENWRT_IPV6_REQUIRED` before creating
a Display Run or billing record.

| Call path | Plan/Shell/Browser | Synchronous Chat | Asynchronous Chat | Caller-initiated Chat | Trace/Memory/Output |
| --- | --- | --- | --- | --- | --- |
| Nexus MCP to Docker | yes | SSE + Display/MCP input | MCP Task | independent private Run | yes |
| Nexus MCP to OpenWrt IPv6 | yes | SSE + Display/MCP input | MCP Task | independent private Run | yes |
| Router to Router Invoke | local event stream | `invoke-stream` + reply | Direct Task | new Invoke/Task | local events only |
| Nexus MCP to OpenWrt Relay v1 | ordinary calls only | IPv6 required | IPv6 required | no | not a cloud asset path |

Direct Invoke keeps the old JSON endpoint and adds interactive and Task APIs:

```python
for event in agent.invoke_interactive("inspect", {"path": "."}):
    if event.input_required:
        event.reply("continue")

task = agent.invoke_async("inspect", {"path": "."})
for event in task.events(follow=True):
    print(event.event, event.data)
result = task.result(timeout=300)
```

The SDK routes Task status, reply, cancellation, and Browser asset chunks back
to the same target Agent, so they continue to work when the first request was
forwarded by another Router. Task tokens are excluded from repr, result bodies,
and errors. Direct Tasks are bounded in-memory state (128 Tasks, one-hour TTL by
default) and do not survive an Agent or Router restart.

Cloud OpenWrt calls use a 60-second, single-use Run Context exchange. The Edge
only receives that short reference; AG-UI, Interaction, Memory, Workspace, and
Output credentials are redeemed inside the Agent SDK and removed from the
Envelope before business code runs.

Data Assets use the existing governed export path rather than a second
Collection API: a cloud Run reports Trace and caller-scoped Memory, writes
Output to its Run root, and Nexus snapshots it immutably. The owner can then use
`Import Agent Asset -> Collection -> Govern -> Release -> Marketplace`.
Router-to-Router Direct Invoke deliberately does not write cloud assets.

## Interactive Display and Chat (0.27.0)

Docker-hosted Agents can publish a private Plan, Shell log, protected Browser
frame, and streaming Chat directly from the current Run. `chat.ask()` is
fail-closed and only works with Streamable HTTP/SSE, an MCP Task, or an
explicit isolated Demo Run; ordinary JSON calls receive a clear transport
error instead of silently losing the question.

```python
ctx.plan.set([{"id": "inspect", "title": "Inspect", "status": "running"}])
ctx.shell.write("$ python inspect.py", stream="command")
ctx.browser.frame("frame.png", title="Workspace browser")
reply = ctx.chat.ask("Continue?", key="confirm", choices=[
    {"value": "continue", "label": "Continue"},
    {"value": "cancel", "label": "Cancel"},
])
ctx.checkpoint.save(stage="confirmed", data={"selection": reply.value})
ctx.chat.say("Finished")
```

FastMCP tools use `await nexus.plan`, `shell`, `browser`, `chat`, and
`checkpoint`. See `examples/hosted_interactive_display_agent` for the complete
Docker scenario.

## Caller-delegated Workspace, MCP Server, and Memory management (0.26.0)

Nexus-hosted Agents can declare a narrow Workspace capability set. Nexus then
intersects that declaration with the current caller's explicit grant and gives
the Run a short-lived delegate token. The container never receives the caller's
JWT/API key or a stored SSH credential. Passwords and private keys are
write-only; connection results contain only non-secret configuration.

```python
from nexus_agent.fastmcp import CurrentNexusMCP, NexusMCPServer

server = NexusMCPServer("Research Agent")
server.enable_workspace_tools({
    "connection.list",
    "connection.create",
    "connection.test",
    "connection.bind",
    "files.read",
    "files.write",
    "command.execute",
})

@server.tool
async def research(topic: str, nexus=CurrentNexusMCP()):
    await nexus.feedback.progress(0, 3, "Reading workspace")
    source = await nexus.workspace.read_text("input.md")
    await nexus.feedback.progress(1, 3, "Running analysis")
    command = await nexus.terminal.run("python analyze.py", cwd=".", timeout=120)
    await nexus.feedback.log("info", "Analysis completed")
    return {"topic": topic, "source": source, "stdout": command.stdout}

server.run(transport="streamable-http", host="0.0.0.0", port=8000)
```

`NexusMCPServer` uses FastMCP's standard Streamable HTTP implementation at
`/mcp`; install it with `pip install "nexus-openwrt-agent-sdk[fastmcp]"` on Python
3.10+. Legacy HTTP+SSE is disabled by default and can be exposed at `/sse` and
`/messages/` with `legacy_sse=True` during migration.

The same request context provides synchronous APIs for native Agents and
asynchronous APIs for FastMCP tools:

```python
connection = run.computer.connections.create(
    "Research Computer",
    "example.internal",
    "agent",
    auth_mode="private_key",
    private_key=private_key,  # write-only
)
run.computer.connections.test(connection.id)
run.computer.bind(connection.id)
files = run.workspace.list("documents")
text = run.workspace.read_text("documents/input.md")
run.workspace.write_text("outputs/result.md", result)
command = run.terminal.run(
    "python analyze.py",
    cwd=".",
    timeout=120,
    display=True,  # publish command/output to this Run's Private Display Shell
)

# Keep sensitive or noisy command output available only to Agent code:
hidden = run.terminal.run("python internal_check.py", display=False)
assert hidden.displayed is False

# FastMCP/async equivalents:
files = await run.aio.workspace.list("documents")
command = await run.aio.terminal.run(
    "python analyze.py",
    timeout=120,
    display=False,
)
```

`display` defaults to `True` for backward compatibility. When it is `False`,
Nexus still executes the command with the same Run-scoped permission checks and
returns its result to the Agent, but it does not create or append the caller's
Private Display Shell transcript. `CommandResult.displayed` reports the choice
accepted by Nexus.

Connection management, file access, and command execution are fail-closed:
unmanaged Runs, undeclared scopes, denied/revoked grants, path traversal, and
cross-caller resources raise a secret-safe `NexusComputerError`. AG-UI event
delivery remains fail-open so reporting failures never change a Tool result.

## One-command Linux and Windows IPv6 setup (0.23.0)

For a Linux release bundle, run the installer as the normal login user:

```bash
chmod +x install-linux.sh
./install-linux.sh
```

It selects the bundled wheel, installs the SDK under `~/.local`, makes both
`python3 -c 'import nexus_agent'` and the `nexus-agent` CLI available, and
then starts IPv6 setup. It deliberately does not use system `pip`, so it works
on PEP 668 distributions without `--break-system-packages`, `pipx`, or
`python3-venv`. Use `./install-linux.sh --install-only` when system setup
should be performed later. Do not run the installer itself with `sudo`; the
CLI requests scoped elevation when it installs addressd.
The same commands now productize one-Agent-one-IPv6 `/128` mode on Linux
and Windows:

```bash
nexus-agent ipv6 setup
nexus-agent ipv6 doctor
```

## Nexus-hosted private Runs and caller Computers (0.24.0)

Nexus-hosted Agent calls receive an isolated run context. The SDK reads the
run-scoped endpoint and write token from Nexus-injected request headers, emits
AG-UI events without adding a required dependency, and safely becomes a no-op
outside a managed runtime:

```python
from nexus_agent import NexusAgent, NexusRunContext

agent = NexusAgent(router="auto", auth="auto", tenant="demo")

@agent.capability("research")
def research(payload, run: NexusRunContext):
    with run.trace.step("retrieve-sources"):
        with run.trace.tool("search", arguments={"topic": payload["topic"]}) as call:
            results = search(payload["topic"])
            call.result({"hit_count": len(results)})

    run.memory.add(
        "Markdown is the preferred report format.",
        kind="preference",
        confidence=0.95,
        consent="approved",
        license="internal",
        scope="caller",
    )
    run.output.write_text(
        "report.md",
        "# Private report",
        content_type="text/markdown",
        producer_step="compose-report",
        license="internal",
    )
    return {"ok": True}
```

For a FastMCP Docker Agent, install the existing optional integration and use
the request-scoped dependency. The injected parameter is excluded from the MCP
tool schema:

```python
from fastmcp import FastMCP
from nexus_agent import NexusRunContext
from nexus_agent.fastmcp import CurrentNexusRun

mcp = FastMCP("Research Agent")

@mcp.tool
async def research(topic: str, run: NexusRunContext = CurrentNexusRun()) -> str:
    with run.trace.step("research"):
        run.output.created("outputs/report.md", content_type="text/markdown")
    return "done"
```

`run.emit(event)` accepts a JSON mapping or an official AG-UI Pydantic event.
`trace.step` and `trace.tool` use standard AG-UI events; memory and output use
the documented `nexus.memory.item`, `nexus.file.*`, and
`nexus.deliverable.ready` custom events. Delivery is FIFO, bounded, retried,
and fail-open. Call `run.report()` to inspect sent, failed, dropped, and pending
counts without exposing credentials.

When the caller has bound a Computer, the same context exposes a run-scoped
proxy. The Agent never receives the SSH host, user, private key, or password:

```python
if run.computer.enabled:
    result = run.terminal.run("python build_report.py", cwd=".")
    run.workspace.write_text("preferences/format.txt", "markdown")
    previous = run.memory.recall()
    run.output.write_text("report.md", result.get("stdout", ""))
```

Approved caller-scoped Memory can be corrected or removed across that caller's
historical Runs. Every write includes the revision returned by `recall()` so
concurrent Runs cannot silently overwrite each other:

```python
item = run.memory.recall()[0]
updated = run.memory.update(
    item["id"],
    revision=item["revision"],
    text="Prefer concise Markdown reports.",
    confidence=0.98,
)
run.memory.delete(updated["id"], revision=updated["revision"])
```

Invocation code may mutate only the current caller's `caller` Memory in the
current tenant and project. `agent_global` is read-only and `developer_only` is
not exposed. Update and delete are synchronous and fail closed; stale writes
raise `NexusMemoryConflict` with the current revision.

`workspace/` persists for the same caller, Agent, and Computer binding, while
`output.write_text()` always targets the current Run's private output root.
Each invocation receives an independent Terminal and Display. Terminal access
is fail-closed and explicit (unlike event delivery, which remains fail-open).

Events are public by default so that they can drive Agent Display. Pass
`visibility="private"` to any helper for management-only data. Never report
credentials, prompts, private model output, or exception stacks.

On Linux, setup first discovers an owned routed global prefix through iproute2
JSON. If none exists, it actively solicits an upstream Router Advertisement and
accepts only an explicitly advertised Autonomous global `/64`. If that also
fails, it sends a non-committing DHCPv6 IA_NA Solicit and can install a dynamic
backend that requests one server-assigned `/128` per Agent. It never widens an
existing provider `/128` into a guessed prefix. Setup prompts when more than
one network is usable, opens `sudo`, installs a machine-owned runtime under
`/opt/nexus-agent/addressd-runtime`, creates the `nexus-agent` group, installs
and enables a hardened systemd service, and runs a short-lived JWT `/128`
self-test. Requirements are Linux with systemd, iproute2, sudo (or an existing
root shell), and Python 3.9 or newer.

Automation can avoid the network prompt:

```bash
nexus-agent ipv6 setup \
  --interface eth0 \
  --prefix 240e:1234:5678:1200::/64 \
  --port 9443

# Explicit no-PD upstream IPv6 inheritance:
nexus-agent ipv6 setup --mode upstream-relay --interface eth0

# Explicit server-assigned address per Agent (Linux):
nexus-agent ipv6 setup --mode dhcpv6-ia-na --interface eth0
```

After setup, sign out and back in once so the invoking user receives the
`nexus-agent` group used by `/run/nexus-agent/addressd.sock`. Doctor checks
the installed configuration, systemd service, on-link prefix, Unix Socket,
recommended port, and current group membership. No permanent firewall rule
is created or changed.

On Windows, install the optional runtime first:

```powershell
pip install "nexus-openwrt-agent-sdk[windows]"
nexus-agent ipv6 setup
nexus-agent ipv6 doctor
```

Windows setup opens UAC, discovers a usable global `/64`, avoids Hyper-V
reserved ports, stages a LocalSystem runtime under
`C:\ProgramData\Nexus\addressd-runtimes`, and uses a protected Named Pipe.

`address="auto"` does not manufacture public address space. A `/64` must be
on-link/routed to the host, or a DHCPv6 server must grant IA_NA addresses.
Multiple IA_NA requests are standards-compliant, but address count and quota
remain server policy. Host and upstream firewalls must permit the Agent port.

## One Agent, one host IPv6 `/128` (0.20.1)

One physical NIC can now host a separate global IPv6 address for every local
Agent. Application code asks for `"auto"`; the privileged local `addressd`
service adds and leases the `/128`, while the Agent binds its own address and
keeps the lease alive:

```python
from nexus_agent import NexusAgent

agent = NexusAgent.public_ipv6(
    "auto",
    address_mode="host-alias",
    auth="none",
    tenant="demo",
    agent_id="echo-1",
    port=9443,
)

@agent.capability("demo.echo")
def echo(payload):
    return {"echo": payload}

agent.run()
```

The one-command setup above is recommended. For a temporary or manual Linux deployment, an administrator can run:

```bash
sudo nexus-agent-addressd \
  --interface eth0 \
  --prefix 240e:1234:5678:1200::/64

# Or, when this exact /64 is advertised by the upstream router but no PD exists:
sudo nexus-agent-addressd \
  --interface eth0 \
  --prefix 240e:1234:5678:1200::/64 \
  --upstream-relay

# Or, request one DHCPv6 IA_NA binding for each local Agent:
sudo nexus-agent-addressd \
  --interface eth0 \
  --prefix dynamic \
  --dhcpv6-ia-na
```

On Windows, install the optional Named Pipe and Service runtime, then configure
it once from an elevated PowerShell terminal:

```powershell
pip install "nexus-openwrt-agent-sdk[windows]"
nexus-agent-addressd-service configure `
  --interface "Ethernet" `
  --prefix "240e:1234:5678:1200::/64" `
  --allow-user "$env:USERDOMAIN\$env:USERNAME"
nexus-agent-addressd-service install --startup auto
nexus-agent-addressd-service start
```

Sign out and back in after first adding a user to `Nexus Agent Users`.

`address="auto"` does not manufacture public address space. The `/64` must
already be routed/on-link, and the host firewall must permit the selected
port. No router Agent daemon, Directory, Relay, AFIB, SLAAC virtual interface,
or per-Agent NIC is required. See
`docs/P8_24_SDK_HOST_ALIAS_IPV6.md` for deployment and failure semantics.

## Agent-owned public IPv6 direct mode (0.19.0)

An Agent with an existing global IPv6 address can now serve calls without
router registration, AFIB selection or Relay forwarding. The address must
already belong to the host and the port must be explicit:

```python
from nexus_agent import NexusAgent

agent = NexusAgent.public_ipv6(
    "240e:1234:5678::20",
    port=9443,
    auth="none",
    tenant="demo",
    agent_id="echo-1",
)

@agent.capability("demo.echo")
def echo(payload):
    return {"echo": payload}

agent.run()
```

For direct JWT verification, configure the called Agent once and give callers
a short-lived token. The dependency-free policy deliberately supports only
HS256 shared-secret deployments; it does not pretend to validate OIDC/JWKS
RS256 tokens:

```python
import os
from nexus_agent import HmacJwtServerAuth, NexusAgent

auth = HmacJwtServerAuth(
    os.environ["NEXUS_AGENT_JWT_SECRET"],
    issuer="https://issuer.example",
    audience="public-echo-agent",
)
agent = NexusAgent.public_ipv6(
    "240e:1234:5678::20", port=9443, auth=auth,
    tenant="demo", agent_id="echo-1",
)
```

The server validates signature, `iat`/`exp`, issuer, audience,
`agent.invoke` scope, tenant and `source_agent`. Both synchronous and resumable
SSE calls use the same policy. See `docs/P8_23_AGENT_OWNED_PUBLIC_IPV6.md` in
the OpenWrt workspace.

## Automatic LAN Agent Server (0.18.1)

With `agent-gw` 0.20.0 or newer, the high-level `NexusAgent` callback address
is consumed directly by the router. A normal LAN Agent no longer needs a
duplicate `Callable Agent endpoints` row in LuCI:

```python
from nexus_agent import NexusAgent

agent = NexusAgent(
    router="http://192.168.1.1:7445",
    auth="none",
    tenant="local",
)

@agent.capability("demo.echo", public_ipv6=False)
def echo(payload):
    return {"echo": payload}

agent.run()
```

The SDK starts the HTTP/SSE server, prefers an IPv6 ULA or private IPv4
callback address, registers and renews every capability, and unregisters on
clean exit.

## Automatic HTTPS Agent Server (0.22.0)

Passing a server certificate and key also makes the callback mapping
automatic. The SDK reads the first lowercase dotted DNS SAN and leaf
certificate SHA-256, then sends the DNS identity, numeric address, port and CA
label with every registration and renewal:

```python
from nexus_agent import NexusAgent

agent = NexusAgent(
    router="https://router.example.com",
    auth="auto",
    tenant="local",
    cert_file="agent-fullchain.pem",
    key_file="agent-key.pem",
    # Omit for a publicly trusted certificate.
    server_ca_bundle_id="enterprise-agent-ca",
)

@agent.capability("chip.verilog.verify.lint.v1", public_ipv6=False)
def lint(payload):
    return {"ok": True, "source": payload["source"]}

agent.run()
```

The route's endpoint uses the certificate DNS name while its lease-bound
backend metadata carries the actual IPv4/IPv6 address. The target router
creates and renews the mapping in memory and removes it on unregister or lease
expiry. No **Fixed HTTPS endpoint mappings** row is required.

The router must already trust the named CA bundle. Public certificates use
the default `system` bundle. For a private CA, an administrator approves the
PEM bundle once in LuCI and gives it the same label passed as
`server_ca_bundle_id`; an Agent cannot authorize or overwrite its own CA.

## Explicit No JWT mode (0.18.0)

When the router selects **No JWT**, make the same choice explicit in Python.
This suppresses both `Authorization` and `Txn-Token`, even if the process has
an old `NEXUS_AGENT_TOKEN` environment variable:

```python
from nexus_agent import NexusAgent

agent = NexusAgent(router="http://192.168.1.1:7445", auth="none")
result = agent.invoke("demo.echo", {"message": "hello"})
```

The LAN listener can select LAN, peer or cross-NAT Relay routes from the same
AFIB. For an exact public IPv6 `/128` over cleartext HTTP, omit the token:

```python
from nexus_agent import DirectIPv6Agent

target = DirectIPv6Agent.plain_http("2001:db8:100::123")
```

No JWT removes only the Agent caller credential. Relay/router TLS identity,
route policy, hop limits and the public `/128`-to-Agent binding remain active.

## Router-discovered authentication and trusted-LAN bootstrap (0.28.0)

Application code no longer needs issuer, audience, JWKS or refresh logic. Use
one of the following two deployment choices and keep the same Python code:

```python
from nexus_agent import NexusAgent

agent = NexusAgent(router="http://192.168.250.1:7446")
```

Authentication is automatic when no explicit credential is supplied. On the
router's trusted LAN SDK listener, the SDK binds the first registration or
invoke identity (`tenant` plus `origin`), obtains a source-bound 300-second LAN
session, caches it only in memory, refreshes it before expiry, and bootstraps
again after one failed 401. A single automatic client cannot mix identities.

Run the included registration and round-trip test without any Nexus
authentication environment variables:

```powershell
python examples/router_echo_agent.py --self-test
```

Add `--refresh-wait-seconds 275` to keep the same Agent and caller alive across
the refresh window. Closing the process unregisters the route.

Remote deployments retain the existing priority and compatibility paths. For
an already-issued access token, set `NEXUS_AGENT_TOKEN`. For OIDC Client
Credentials, set `NEXUS_AGENT_CLIENT_ID` and `NEXUS_AGENT_CLIENT_SECRET`; the
SDK discovers the issuer's standard token endpoint. If the router explicitly
declares authentication disabled, no token is sent. The SDK never stores a LAN
session, access token, or client secret on disk. `NEXUS_JWT`,
`NEXUS_AGENT_JWT` and `NEXUS_TOKEN` remain deprecated compatibility aliases.

Router and identity-provider trust can be configured independently with
`router_ca_file=` and `auth_ca_file=`. Authentication failures raise
`NexusAuthenticationError`; valid callers lacking a required scope raise
`NexusAuthorizationError`.

## Friendly callable Agent API (0.17.0)

普通 Python Agent 不再需要手写 Server、Envelope、CapabilityRegistration 或租约
清理代码。只需配置一次环境并声明业务函数：

```powershell
$env:NEXUS_ROUTER_URL = "http://[fd00::1]:7443"
$env:NEXUS_AGENT_ADDRESS = "fd00::20"
$env:NEXUS_AGENT_TOKEN = "JWT_WITH_agent.register_AND_agent.invoke"
python examples/friendly_agent.py
```

```python
from nexus_agent import NexusAgent, SseEvent

agent = NexusAgent(tenant="demo", agent_id="echo-server")

@agent.capability("demo.echo", public_ipv6=True)
def echo(payload):
    return {"echo": payload}

@agent.stream_capability("demo.echo", public_ipv6=True)
def echo_stream(payload):
    yield SseEvent(data="accepted", event="progress")
    yield SseEvent(data=str({"echo": payload}), event="result")

agent.run()
```

`run()` 自动完成监听、注册、续租、路由丢失重注册、健康撤路和退出注销。业务函数
默认只接收 `payload`；确实需要路由元数据时设置 `pass_envelope=True`。路由器返回
托管 `/128` 后，启动信息会直接打印可复制的 Public URL 和 HTTP/HTTPS transport。

默认自动发现顺序为：`NEXUS_ROUTER_URL`、`nexus-router.local`、Linux 默认网关；
Windows 上还会在有界范围内检查直连私有网段的 Router 候选地址，因此专用
Router LAN 网卡不需要配置默认网关；
Agent 后端地址依次使用 `NEXUS_AGENT_ADDRESS`、兼容变量 `NEXUS_AGENT_IPV6`、主机
可用地址。无法可靠判断时会给出可操作错误，不会注册不可达占位地址。`listen_host`
默认跟随自动发现地址选择 IPv4 或 IPv6。

同一个 Agent 也可调用其它网段的 Agent，无需重复填写调用者身份：

```python
result = agent.invoke(
    "demo.translate",
    {"text": "hello"},
    target_agent="agent://remote/translator-2",  # 可省略，省略时由 AFIB 选路
)

for event in agent.invoke_stream("demo.progress", {"job": 7}):
    print(event.event, event.data)
```

完整示例见 `examples/friendly_agent.py`。原有 `NexusAgentServer`、
`NexusAgentClient` 和 `CapabilityRegistration` 仍是稳定的专家级低层接口。

### OpenWrt 边缘端 Private Display 示例

`examples/edge_private_run_agent.py` 是一个可直接放到 OpenWrt 边缘主机运行的
单文件 Agent。它使用 Router 自动发现和自动 LAN 身份，不要求在命令行中传入
Cloud 密码、API Key、TLS 证书或私钥：

```sh
python3 examples/edge_private_run_agent.py
```

示例通过 Public IPv6 接受 Cloud MCP 交互调用，并把 Plan、Shell、Browser 和
Chat 显示到当前调用者的 Private Display。Files 与 Shell 只操作当前 Run 中由
调用者绑定并授权的 Computer。调用前需为 Agent 授权 `files.list`、`files.read`、
`files.write` 和 `command.execute`；Relay v1 不支持这类长时交互调用。

## Optional plain IPv6 HTTP (0.15.0)

When the target router explicitly selects **Plain HTTP + JWT (no TLS)** in
LuCI, the caller needs no CA, TLS identity, or client certificate:

```python
from nexus_agent import DirectIPv6Agent

target = DirectIPv6Agent.plain_http("2001:db8:100::123", token=jwt)
```

This sends both the JWT and Agent payload in cleartext. The mode is explicit:
the SDK never retries a failed HTTPS request over HTTP. The router still
verifies the JWT and binds the destination IPv6 `/128` to its owning Agent.

## IPv6 + JWT only (0.14.0)

The application-facing direct-call API now needs only the target IPv6 address
and an access JWT:

```python
from nexus_agent import DirectIPv6Agent

target = DirectIPv6Agent("2001:db8:100::123", token=jwt)
result = target.invoke(
    "demo.echo",
    {"message": "hello"},
    tenant="demo",
    source_agent="agent://demo/caller-a",
)
```

TLS trust and caller mTLS identity remain mandatory, but are provisioned once
outside application code. Copy **IPv6 connection** from LuCI's Local Agents
page, then install it on the caller machine:

```bash
nexus-agent-security install router-b.json \
  --ca-file nexus-ca.pem \
  --client-cert caller.crt \
  --client-key caller.key
```

The profile is stored under the platform user configuration directory (or at
`NEXUS_AGENT_SECURITY_PROFILE`). It maps IPv6 prefixes by longest-prefix match,
selects the TLS certificate name and CA bundle, and loads the caller workload
certificate automatically. JWT is still supplied per call and is never saved
by this command.

## IPv6 Direct Agent Mesh（0.12.0）

调用方已知目标 Agent `/128` 时，可使用 `DirectIPv6Agent` 直接连接目标
OpenWrt，不查询 Directory、Agent Card、Peer、Relay 或 Transit 路由：

```python
from nexus_agent import DirectIPv6Agent

target = DirectIPv6Agent(
    "2001:db8:100::123",
    server_identity="router-b.example.com",
    token="JWT_WITH_agent.invoke",
    ca_file="nexus-ca.pem",
    cert_file="caller.crt",
    key_file="caller.key",
)

result = target.invoke(
    "demo.echo",
    {"message": "hello over IPv6"},
    tenant="demo",
    source_agent="agent://demo/caller-a",
)
```

TCP authority 保持为字面 IPv6；`server_identity` 仅用于 SNI 和证书名称验证，
所以动态 `/128` 不需要逐地址 IP SAN。直连客户端禁用环境 HTTP proxy，目标
不可达时直接失败。目标路由器仍会按目的 `/128` 执行本地强绑定查询，但不会
执行跨路由候选选择。

被调用方 SDK Server 支持原生 IPv6 和显式双栈：

```python
server = NexusAgentServer(
    "::", 9443,
    address_family="ipv6",
    dual_stack=False,
    cert_file="agent-server.crt",
    key_file="agent-server.key",
)
```

目标 OpenWrt 的受控 backend map 使用带方括号的 numeric IPv6，例如
`agent-b.example.com:9443=[2001:db8:200::20]`。完整双端示例见
`examples/ipv6_callable_agent.py` 和 `examples/direct_ipv6_caller.py`。

## Router-managed public IPv6 (0.11.0)

Set `public_ipv6="auto"` on `CapabilityRegistration` to request a `/128` from
the router's configured routed prefix. The assigned address is available as
`lease.public_ipv6`; the SDK never changes the host network configuration.

## Capability 租约自愈（0.10.0）

`NexusAgentServer.serve_registered()`、`FastMCPBridge.serve_registered()` 和
`NexusA2AAgent.run()` 默认完成注册、续租、自愈和注销：

- 路由器重启或 route 丢失导致 `renew` 返回 404 时，SDK 使用原 capability 自动
  `register`，并原子更新 `lease.route_id`；
- 本地 Agent Server、FastMCP Tool loop 或 A2A executor loop 停止时，SDK 在下一
  租约周期立即注销 route 并停止续租；
- 正常退出上下文时仍立即 `unregister`；进程崩溃时由 agentd 租约超时清理；
- 显式 `healthy=False` 的 route 不会触发重新注册。

默认行为无需额外参数。如需关闭路由丢失自愈，可传入
`reregister_on_not_found=False`。`AgentLease.reregister_count`、
`health_check_failures` 和 `last_error` 可用于应用观测。

MCP/A2A capability mapping 本身只是协议到 intent 的静态转换规则，不会创建
ARIB/AFIB route。只有正在运行的 Agent 通过上述 SDK 注册租约后才可被路由；Agent
正常退出或协议服务停止时 SDK 注销，异常退出时由短租约到期清理。因而 mapping 可以
保留，不会在没有实际 Agent 时形成可调用的幽灵路由。

## 断线续传（0.9.0+）

`NexusAgentClient.invoke_stream()` 默认使用稳定 `task_id`、数字事件 ID 和
`Last-Event-ID` 自动恢复短时断线。调用方只需正常迭代：

```python
for event in client.invoke_stream(envelope, max_reconnects=3):
    print(event.event_id, event.event, event.data)
```

`NexusA2AClient.stream()` 使用 Access JWT 时也默认自动恢复；可通过
`resume=False` 关闭，或通过 `last_event_id` 从应用保存的游标继续。重连必须复用
同一任务请求，路由器会把它固定到首次 AFIB route，Agent Server 只回放游标后的
事件且不会重新执行 handler。

一次性 Transaction Token 不能用于重连。历史位于有界内存中；超过保留窗口或
Agent/路由器重启后会收到明确错误，而不是从头静默执行。完整契约和错误码见
the resumable streaming API described below。

Python 3.9+ SDK，用于把普通 LAN Python Agent 接入 Nexus OpenWrt 双平面路由器。
SDK 只使用 Python 标准库，既能注册和调用其他 Agent，也包含可作为被调用方运行的
HTTP/SSE Server。

```python
from nexus_agent import CapabilityRegistration, NexusAgentClient

client = NexusAgentClient(
    "https://router.example.test:7443",
    token="JWT_WITH_agent.register_AND_agent.invoke",
    ca_file="router-ca.pem",
    cert_file="agent.crt",
    key_file="agent.key",
)

registration = CapabilityRegistration(
    intent="chip.verilog.verify.lint",
    origin="agent://tenant-a/linter-1",
    endpoint="https://linter-1.example.test:9443/invoke",
    tenant="tenant-a",
    lease_seconds=30,
    # Optional router-managed /128 from a configured routed prefix.
    public_ipv6="auto",
)

with client.register(registration, auto_renew=True) as lease:
    print("AFIB route:", lease.route_id)
    print("Router-managed public IPv6:", lease.public_ipv6)
    result = client.invoke_intent(
        "chip.verilog.verify.lint",
        {"source": "module top; endmodule"},
        tenant="tenant-a",
        source_agent="agent://tenant-a/caller-1",
        target_agent="agent://tenant-a/linter-1",
    )
    print(result)
```

上下文退出时 SDK 会停止续租并调用 `unregister`。进程崩溃或网络中断时，agentd
仍会在租期到期后撤销该 LOCAL route。若 `jwt_required=1`，注册令牌需要
`agent.register` scope；调用令牌需要 `agent.invoke`（以及网关现有策略要求的
scope）。启用 JWT 时，Proxy 会用认证声明覆盖注册请求的 `tenant` 与 `origin`。

`source_agent` 表示“谁发起调用”，用于身份、策略和审计；它不是目的地址。
`target_agent` 才是可选的精确目的 Agent URI。填写后，路由器仍先按 `intent`、
tenant 和约束查询 AFIB，但只接受 `origin` 与该 URI 完全相同的路由，并在 LAN
Transit、跨 NAT Relay 和多 Relay 转发期间保持该目标。不填写时，仍由 AFIB
按照成本、延迟、信任和负载选择最佳 Agent。

```python
result = client.invoke_intent(
    "demo.echo",
    {"message": "hello remote agent"},
    tenant="demo",
    source_agent="agent://demo/caller-1",
    target_agent="agent://remote/echo-2",
)
```

因此可以使用已注册的 Agent URI 定向调用远端实例，但不能把 URI 当成网络 URL
直接连接；Directory/Relay/Transit 和最终 endpoint 仍由 Nexus 路由平面解析。

## 作为被调用方运行

```python
from nexus_agent import (
    CapabilityRegistration,
    NexusAgentClient,
    NexusAgentServer,
    SseEvent,
)

client = NexusAgentClient("https://router.example.test:7443", token="JWT")
server = NexusAgentServer(
    "0.0.0.0", 9443,
    cert_file="agent-server.crt",
    key_file="agent-server.key",
)

@server.handler("chip.verilog.verify.lint")
def lint(envelope):
    return {"ok": True, "input": envelope.payload}

@server.stream_handler("chip.verilog.verify.lint")
def lint_stream(envelope):
    yield SseEvent(data="checking", event="progress")
    yield SseEvent(data='{"ok":true}', event="result")

capability = CapabilityRegistration(
    intent="chip.verilog.verify.lint",
    origin="agent://tenant-a/linter-1",
    endpoint="https://linter-1.example.test:9443/invoke",
    tenant="tenant-a",
)

server.serve_registered(client, [capability])
```

`serve_registered()` 会注册全部 capability、启动后台续租、提供同步 HTTP 与 SSE
处理，并在服务正常退出时注销路由。传入服务器的 `AgentEnvelope` 已完成有界 JSON、
必填字段和重复键校验；`route_id` 来自路由器的 `X-Nexus-Route-Id`。
`client_ca_file` 是可选的入站 mTLS 模式；直接连接当前 `agent-gw` remote backend
时只配置服务端证书，因为该数据面执行严格的服务端 TLS identity 校验。

如果调用来自 MCP/A2A Adapter，原始协议请求位于
`envelope.protocol_request`，协议和 tool/skill 分别可从 `envelope.protocol` 与
`envelope.selector` 读取。完整运行示例见 `examples/callable_agent.py`。

路由器上的注册、调用、MCP/A2A 和流式开关，以及 MCP/A2A capability mapping，
均可在 LuCI 的 **Agent Routing → Agent APIs & Protocols** 页面配置，无需手写 UCI。

`agent-gw` 本身仍只允许 loopback listener；LAN/WAN Agent 应连接已有的 mTLS
edge 或受控的 LAN reverse proxy。

## FastMCP 集成（可选）

基础 SDK 仍无第三方依赖。FastMCP 集成需要 Python 3.10+，安装：

```bash
python -m pip install "nexus-openwrt-agent-sdk[fastmcp]"
```

业务 Tool 只保留原有 `@mcp.tool`，再提供一条
`Tool -> Nexus capability` 映射即可：

```python
from fastmcp import FastMCP
from nexus_agent import CapabilityRegistration, NexusAgentClient, NexusAgentServer
from nexus_agent.fastmcp import FastMCPBridge

mcp = FastMCP("Verilog Agent")

@mcp.tool
async def lint_verilog(source: str) -> dict:
    return {"ok": "endmodule" in source}

server = NexusAgentServer("0.0.0.0", 9443)
bridge = FastMCPBridge(mcp, server, {
    "lint_verilog": CapabilityRegistration(
        intent="chip.verilog.verify.lint",
        origin="agent://demo/linter-1",
        endpoint="http://192.168.1.20:9443/invoke",
        tenant="demo",
    ),
})

router = NexusAgentClient("http://192.168.1.1:7443", token="JWT")
bridge.serve_registered(router)
```

`FastMCPBridge` 启动时通过 FastMCP Client 枚举完整 Tool catalog，确认所有
映射 Tool 真实存在，然后才安装 Nexus intent handler 并注册 AFIB 路由。
未映射的 FastMCP Tool 不会暴露到 Nexus。路由器 MCP Adapter 传来的
`tools/call` 会自动取出 `params.arguments`；直接 Nexus Invoke 把 Envelope
`payload` 作为参数。异步 Tool 在常驻事件循环中执行，结果会自动转换为
JSON；也可在应用中使用 `await bridge.call_tool(...)`。完整例子见
`examples/fastmcp_agent.py`。
