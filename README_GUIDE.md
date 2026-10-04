# Nexus Agent SDK for Python

Build callable Python agents, connect them through Nexus OpenWrt, expose them as MCP tools, or give an authorized Nexus agent access to your computer.

The core SDK has no third-party runtime dependencies. Browser, Computer Runtime, MCP and A2A support are optional installations.

- **Distribution:** [`nexilume`](https://pypi.org/project/nexilume/) (since 0.47.0)
- **Python import:** `nexus_agent`
- **Source and downloads:** [GitHub](https://github.com/Nexilume-AI/nexus-agent-sdk-python) · [Releases](https://github.com/Nexilume-AI/nexus-agent-sdk-python/releases)
- **License:** [Apache License 2.0 (modified)](LICENSE)

## Choose your starting point

| I want to... | Start here |
| --- | --- |
| Try a Python agent without a router or Cloud account | [Run your first agent](#run-your-first-agent) |
| Register an agent with Nexus OpenWrt | [Connect to OpenWrt](#connect-to-openwrt) |
| Make distributed Agents call each other | [Two-Agent example](#example-two-distributed-agents-calling-each-other) |
| Serve an agent as MCP tools | [Use a hosted MCP runtime](#use-a-hosted-mcp-runtime) |
| Connect my computer to Nexus Cloud | [Set up Computer Runtime](#set-up-computer-runtime) |
| Give agents their own IPv6 addresses | [Configure Agent IPv6 on Linux](#configure-agent-ipv6-on-linux) |
| Add streaming, files, browser tools or A2A | [Explore the examples](#explore-the-examples) |

The SDK does not install an OpenWrt router, Nexus Cloud or a Relay server. Features that use those services require a configured deployment.

## Install

Use **Python 3.12** for the easiest path through the optional integrations. The dependency-free core wheel supports Python 3.9+. Building from source requires Python 3.10+; optional dependencies can require newer Python versions.

### 1. Create a virtual environment

Linux or macOS:

```sh
python3 -m venv .venv
. .venv/bin/activate
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
```

On Ubuntu, install `python3-venv` if creating the environment reports that `ensurepip` is unavailable.

### 2. Install from PyPI

Install the 0.48.0 release from [PyPI](https://pypi.org/project/nexilume/0.48.0/):

```sh
python -m pip install nexilume==0.48.0
python -c "import nexus_agent; print(nexus_agent.__version__)"
```

Use `python -m pip install --upgrade nexilume` for the latest release. The distribution name is `nexilume`; Python imports remain `nexus_agent`, and commands remain `nexus-computer` and `nexus-agent`. The PyPI package named `nexus-agent-sdk` belongs to a different project.

For optional features, install only the extras you need:

```sh
python -m pip install "nexilume[computer,browser,fastmcp,a2a]==0.48.0"
```

Migrating from our older `nexus-openwrt-agent-sdk` wheel? In the same environment, run `python -m pip uninstall nexus-openwrt-agent-sdk` **before** installing `nexilume`. Do not keep both distributions installed: they share the same import directory. Keep your Runtime configuration and device keys. See [Computer Runtime upgrade](#upgrade-an-existing-computer-runtime) before restarting an existing service.

For offline installation, download the wheel from [PyPI release files](https://pypi.org/project/nexilume/0.48.0/#files) and use `python -m pip install ./nexilume-0.48.0-py3-none-any.whl`. Optional dependencies must also be available offline. Older wheels remain in [GitHub Releases](https://github.com/Nexilume-AI/nexus-agent-sdk-python/releases) for historical use.

| Extra | Enables |
| --- | --- |
| `computer` | The outbound Computer Runtime connection to Nexus Cloud |
| `browser` | Browser automation through Playwright; a browser binary is also required |
| `fastmcp` | Hosted MCP tools and the FastMCP bridge |
| `a2a` | Integration with the official A2A SDK |
| `fastmcp-tasks` | Optional FastMCP Tasks integration |
| `windows` | Windows service helpers (Windows only) |

### Install from source instead

Use this option to run the repository examples or work with local changes:

```sh
git clone https://github.com/Nexilume-AI/nexus-agent-sdk-python.git
cd nexus-agent-sdk-python
python -m pip install ".[computer,browser,fastmcp,a2a]"
```

Use `python -m pip install .` for the core only. Commands below that reference `examples/` run from this repository directory; the wheel does not install the example files into your working directory.

### Dependency compatibility

Version 0.48.0 includes the dependency fixes below. Earlier release files are
unchanged; upgrade the appropriate extra in your own virtual environment.

| Installation | Python / prerequisites |
| --- | --- |
| Core wheel | Python 3.9+; no third-party runtime dependencies |
| `computer` | Python 3.9+; installs `tomli` below 3.11 for Tool Setup |
| `browser` | Python 3.10+; Playwright 1.63+; Python 3.12 recommended |
| `fastmcp`, `fastmcp-tasks`, `a2a` | Python 3.10+; directly imported libraries are declared in each extra |
| `windows` | Windows only; pywin32 is for address/service helpers, not ordinary Computer Runtime pairing |
| Source build | Python 3.10+ for the build backend; the resulting core wheel also runs on 3.9 |

`fastmcp-tasks` remains a Nexus extra name. It now installs official
`fastmcp[tasks]`, not the unresolvable `fastmcp-tasks>=0.1,<1`
requirement. Native FastMCP Tasks are separate from Nexus `task=True` policy.

Pip installs Python packages, not Docker, Provider images, Chrome, or operating
system libraries. Browser users still need a supported local browser or
`python -m playwright install chromium`; Linux may also require
`python -m playwright install-deps chromium`. These are separate explicit
installation steps, not actions run automatically by the SDK.

For Windows address/service helpers, install `nexilume[windows]` explicitly.
Do not add every optional dependency to the core. Libraries use compatible
version ranges; deployments should lock their resolved environment separately.

From a source checkout, after installing the chosen extra:

```sh
python -m pip check
python -I scripts/check_installation.py --extra computer
```

Replace `computer` with `core`, `browser`, `fastmcp`, `fastmcp-tasks`,
`a2a` or `windows`. This checks the installed wheel and does not pair a device,
install a service, launch a browser or modify user configuration. The Browser
check starts only Playwright's driver; real browser/OS acceptance is separate.

## Run your first agent

This example runs entirely on your computer. Save it as `hello_agent.py`:

```python
from nexus_agent import NexusAgentServer

server = NexusAgentServer("127.0.0.1", 9443)

@server.handler("demo.echo")
def echo(envelope):
    return {"message": envelope.payload["message"]}

if __name__ == "__main__":
    try:
        server.serve_forever()
    finally:
        server.server_close()
```

Start it:

```sh
python hello_agent.py
```

In a second terminal using the same virtual environment, save and run `call_agent.py`:

```python
import json
from urllib.request import Request, urlopen

request = Request(
    "http://127.0.0.1:9443/invoke",
    data=json.dumps({
        "version": "1.0",
        "intent": "demo.echo",
        "intent_version": 1,
        "task_id": "hello-1",
        "source_agent": "agent://demo/caller",
        "tenant": "demo",
        "hop_limit": 7,
        "payload": {"message": "Hello, Nexus!"},
    }).encode(),
    headers={"Content-Type": "application/vnd.nexus.agent-envelope+json"},
)
with urlopen(request, timeout=10) as response:
    print(json.load(response))
```

Expected output: `{'message': 'Hello, Nexus!'}`. Press Ctrl+C in the server terminal to stop it. This example binds only to loopback; configure authentication and transport security before exposing an agent to other machines.

## Connect to OpenWrt

You need a reachable Nexus OpenWrt **Agent Access Proxy**, with Agent services enabled. From the source checkout, run the registration and round-trip example:

```sh
python examples/router_echo_agent.py --router http://192.168.246.1:7446 --self-test
```

Replace the URL with your router's configured Agent Access Proxy address. The example registers an agent, calls it and cleans up its registration. Authentication defaults to `auto`, using the router's supported LAN authentication or your configured credentials. Use `--auth none` only for an isolated LAN entry point explicitly configured without JWT.

For your own application, the higher-level API handles registration, lease renewal and shutdown cleanup:

```python
from nexus_agent import NexusAgent

agent = NexusAgent(router="auto", tenant="demo", agent_id="echo-server")

@agent.capability("demo.echo")
def echo(payload):
    return {"echo": payload}

if __name__ == "__main__":
    agent.run()
```

Set `NEXUS_ROUTER_URL` if discovery cannot find your router. Set `NEXUS_AGENT_ADDRESS` when the host has multiple interfaces and the automatically selected address is not reachable from the router. Use the configured proxy entry point, not an internal loopback gateway listener.

Credentials belong in your environment or deployment configuration. Supported options include `NEXUS_AGENT_TOKEN`, or `NEXUS_AGENT_CLIENT_ID` and `NEXUS_AGENT_CLIENT_SECRET` for configured OIDC authentication. Never put credentials in uploaded Python files.

### Example: two distributed Agents calling each other

Run Agent A on one computer and Agent B on another. Each registers the same `demo.hello` capability with Nexus OpenWrt and can call the other by its unique Agent identity:

```text
Computer A (agent-a)  <-->  Nexus OpenWrt  <-->  Computer B (agent-b)
```

**Before you start:** install the core SDK on both computers, enable Agent services on your router, and use its Agent Access Proxy URL. The router must be able to reach each computer's advertised address and listening port. Configure credentials as described above when your router requires them. This example uses router-mediated calls and does not require a Cloud account or per-agent public IPv6 addresses.

Save the following as `distributed_agents.py` on both computers, or use [the ready-to-run source example](examples/distributed_agents.py):

```python
"""Run two Agents on separate hosts and call each other through OpenWrt."""

import argparse
import json

from nexus_agent import NexusAgent, NexusAgentError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", choices=("agent-a", "agent-b"))
    parser.add_argument("--port", type=int, default=9443)
    args = parser.parse_args()
    peer = "agent-b" if args.name == "agent-a" else "agent-a"

    agent = NexusAgent(
        runtime="openwrt",
        router="auto",  # Reads NEXUS_ROUTER_URL.
        tenant="demo",
        agent_id=args.name,
        port=args.port,
        cloud_publish=False,
    )

    @agent.capability("demo.hello", public_ipv6=False)
    def hello(payload):
        return {"agent": args.name, "reply": f"Hello, {payload['from']}!"}

    try:
        with agent.start():
            print(f"{agent.origin} is ready. Start {peer} before calling it.")
            while True:
                input(f"Press Enter to call {peer}; Ctrl+C to stop: ")
                try:
                    reply = agent.invoke(
                        "demo.hello",
                        {"from": args.name},
                        target_agent=f"agent://demo/{peer}",
                    )
                    print(json.dumps(reply))
                except NexusAgentError as error:
                    print(f"Call failed: {error}. Check the peer and router, then retry.")
    except (KeyboardInterrupt, EOFError):
        pass  # Exiting the context unregisters this Agent and stops its listener.


if __name__ == "__main__":
    main()
```

**1. Start Agent A on Computer A.** In a terminal with the SDK environment activated, replace these sample addresses with your router and Computer A's reachable LAN address:

```sh
export NEXUS_ROUTER_URL=http://192.168.246.1:7446
export NEXUS_AGENT_ADDRESS=192.168.246.10
python distributed_agents.py agent-a
```

**2. Start Agent B on Computer B.** Use the same router and Computer B's own address:

```sh
export NEXUS_ROUTER_URL=http://192.168.246.1:7446
export NEXUS_AGENT_ADDRESS=192.168.246.11
python distributed_agents.py agent-b
```

In Windows PowerShell, set each variable with `$env:NAME = "value"` instead of `export NAME=value`; the Python commands are the same. If running from the source checkout, use `python examples/distributed_agents.py ...`.

**3. Call in both directions.** Wait until both terminals print `is ready`, then press Enter in Agent A's terminal:

```json
{"agent": "agent-b", "reply": "Hello, agent-a!"}
```

Press Enter in Agent B's terminal to call A:

```json
{"agent": "agent-a", "reply": "Hello, agent-b!"}
```

`agent.invoke()` supplies the caller's `source_agent` identity automatically. `target_agent` selects the exact peer, even though both publish `demo.hello`. The receiving handler returns a reply without calling back, so the example cannot form a recursive call loop. The SDK renews registrations while the processes run and unregisters each Agent when you stop it with Ctrl+C.

To try both processes on one computer, use that computer's reachable address in both terminals and start B with `--port 9444`. If registration succeeds but calls fail, check the peer process, router-to-host reachability, host firewall, and whether the configured credentials permit the call.

**Across two OpenWrt routers:** point each Agent's `NEXUS_ROUTER_URL` at its local router and keep the two Agent identities distinct. The routers must already have cross-router capability routing and a working transport configured, with access allowed for the selected tenant and target. Changing the URLs alone does not establish connectivity through NAT. The Python registration and invocation code stays the same.

## Use a hosted MCP runtime

Install the `fastmcp` extra. Set `runtime="hosted"` to run without OpenWrt discovery or registration:

```python
from nexus_agent import NexusAgent

agent = NexusAgent(runtime="hosted", cloud_name="Echo Agent")

@agent.capability("demo.echo")
def echo(payload):
    return {"echo": payload}

if __name__ == "__main__":
    agent.run()
```

For a Nexus Cloud deployment, use [dual_runtime_agent.py](examples/dual_runtime_agent.py): upload the file through **Agent > Runtime > Upload Python**, build it, and deploy the verified version. The Cloud launcher selects hosted mode and exports the tools over MCP. Keep the agent at module scope and `agent.run()` behind the `__main__` guard.

The deployment's Python profile must include SDK 0.46.0+ and the `fastmcp` extra. Model credentials, additional dependencies and resource permissions must be configured in that deployment. Hosted mode alone does not grant access to a caller's files or computer.

### Native MCP tool results

Return `McpToolResult` when an edge capability needs native text, image, audio,
resource or resource-link blocks, structured content, or a business `isError`.
Ordinary dictionaries keep the existing JSON/text behavior.

```python
from nexus_agent import McpToolResult

@agent.capability("demo.result")
def result(arguments):
    return McpToolResult(
        content=[{"type": "text", "text": "Completed"}],
        structured_content={"ok": True},
        is_error=False,
    )
```

This source feature requires `agent-adapter 0.6.0-r9` or newer for Direct IPv6,
and an updated Cloud Relay proxy for Relay. Upgrade the router/Cloud before the
SDK; it is not yet in the published 0.48.0 wheel. FastMCPBridge preserves native
results on MCP requests while retaining its direct-Invoke JSON interface.
`nexus_mcp_result_version` is an internal reserved response field, not an
application JSON key. Results remain subject to existing response/event limits.
OpenWrt's default request limit is 65,536 bytes, including protocol overhead;
use file references for large data instead of increasing unbounded inline input.

## Set up Computer Runtime

Computer Runtime runs as your operating-system user and connects outbound to Nexus Cloud over WSS. It does not require an inbound SSH port or a public IP address.

1. Install the `computer` extra, plus `browser` if you want browser control.
2. Create a Computer pairing link in your Nexus Cloud workspace.
3. Run the following commands as your normal user:

```sh
nexus-computer setup "<pairing-url-from-nexus-cloud>"
nexus-computer status
```

Wait for the registration to report `connected`. Attach the Computer to a Run and grant the required scopes before an agent uses its files, terminal or browser. Each registration has its own workspace root and identity.

Useful commands:

```sh
nexus-computer logs
nexus-computer restart
nexus-computer repair
nexus-computer unpair --registration <registration-id>
```

`repair` repairs autostart for an existing pairing. `unpair` revokes the selected registration. You can pair the same computer with more than one workspace by running `setup` for each pairing link.

When a terminal uses `shell="auto"`, Computer Runtime selects zsh on macOS, bash (with sh fallback) on Linux, and PowerShell on Windows. Explicit bash or sh selections on macOS are preserved. SDK 0.46.5 uses a POSIX pseudo-terminal (PTY) with an interactive shell on macOS/Linux, including prompts, input echo, Ctrl+C and terminal resizing. SDK 0.46.4 and earlier use pipes and do not include this PTY fix. Windows retains its existing PowerShell transport. This default requires SDK 0.46.4 or newer and a Cloud server that preserves automatic Runtime shell selection. The 0.46.3 wheel predates this change.

### Attached Computer binary files

SDK 0.48.0 supports binary Workspace files. Both Cloud and Computer Runtime
must support `workspace.binary.v1` before use; upgrading the SDK alone does not
add server-side protocol support.

```python
data = ctx.workspace.read_bytes("images/input.png")
ctx.workspace.write_bytes("images/result.png", data)

# Stream larger files between the Agent host and the Attached Computer.
ctx.workspace.upload("/agent-local/result.zip", "exports/result.zip")
ctx.workspace.download("exports/result.zip", "/agent-local/download.zip")

# Async counterparts use the same authorization and Cloud TLS context.
data = await ctx.aio.workspace.read_bytes("images/input.png")
await ctx.aio.workspace.write_bytes("images/result.png", data)
```

Grant `files.read` / `files.write` and Attach a Computer first. Relative paths
use the Run's current Workspace folder when a transfer starts; that transfer
keeps its original directory if the folder changes later. In-memory methods
are limited to 16 MiB; streaming methods to 1 GiB per file, with 256 KiB chunks.
SHA-256 is checked before an atomic replacement. Incomplete transfers preserve
existing files and expire after 15 idle minutes. If the commit response is lost,
check the target digest before retrying; writes are not automatically replayed.
After a Runtime restart, start a new transfer.

`ctx.workspace` accesses the Attached Computer. `ctx.files` instead uploads or
downloads Cloud Run inputs/outputs; Workspace files are not automatically
published as Run outputs.

### Upgrade an existing Computer Runtime

Activate the **same virtual environment used to install Runtime**. If it contains the older `nexus-openwrt-agent-sdk` distribution, uninstall that package first; do not run `nexus-computer unpair` or delete device keys. Then install the current package and restart:

```sh
python -m pip install --upgrade "nexilume[computer,browser]==0.48.0"
nexus-computer restart
nexus-computer status
```

Keep your existing Runtime configuration and device keys. Upgrading in the same environment preserves pairings; you do not need a new pairing link. Installation still comes before pairing for new computers.

Version 0.46.3 includes the SOCKS dependency used by WebSocket system-proxy support, identifies Runtime HTTP/WebSocket requests with `Nexus-Computer/0.46.3`, and fixes browser Enter input for macOS/Linux pipe-based terminals. Proxy availability and Cloud firewall rules still determine connectivity. That release still uses a pipe-based shell; SDK 0.46.5 adds POSIX PTY support as described above.

### Linux browser setup

Install a supported Chrome/Chromium browser, or download Chromium with Playwright:

```sh
python -m playwright install chromium
```

On a minimal Linux installation, Playwright may also need system libraries; `python -m playwright install-deps chromium` installs them and can request administrator privileges.

For a Playwright-downloaded browser or a custom browser location, configure the Runtime service explicitly:

```sh
systemctl --user edit nexus-computer.service
```

Add the following, replacing the executable path with the actual installed browser path:

```ini
[Service]
Environment="NEXUS_BROWSER_EXECUTABLE=/absolute/path/to/chrome"
Environment="NEXUS_BROWSER_HEADLESS=true"
```

Then restart:

```sh
systemctl --user daemon-reload
nexus-computer restart
```

Installing the Playwright Python package alone does not install a browser. An environment variable set only in an interactive shell does not update an already running systemd service.

## Configure Agent IPv6 on Linux

Two paths are available: OpenWrt can manage an agent's address, or a Linux host can run the SDK address service to allocate per-agent `/128` addresses on a suitable IPv6 network.

For host-managed addresses, Linux needs systemd, `iproute2`, administrator permission for initial setup, and a network that supports the selected addressing mode. An IPv6 address by itself does not establish inbound Internet routing.

```sh
nexus-agent ipv6 setup
nexus-agent ipv6 doctor
```

Setup discovers supported configurations and requests elevation for system changes. Supported modes include `routed-prefix`, `upstream-relay` and `dhcpv6-ia-na`; use `nexus-agent ipv6 setup --help` to select an interface or mode explicitly. Only specify a prefix routed or delegated to your deployment.

The source checkout also includes a user-local installer:

```sh
sh install-linux.sh --wheel /path/to/downloaded.whl --install-only
```

Omit `--install-only` to continue into IPv6 setup. Run the installer as your normal user, without `sudo`.

**Known issue in the published 0.46.2 wheel:** the generated Linux address service can fail to assign its Unix socket group. Version 0.46.3 includes the fix (`Group=nexus-agent`) for newly generated services. For an affected installation, add this service override:

```sh
sudo systemctl edit nexus-agent-addressd.service
```

```ini
[Service]
Group=nexus-agent
```

```sh
sudo systemctl daemon-reload
sudo systemctl restart nexus-agent-addressd.service
nexus-agent ipv6 doctor
```

This applies after setup has created the service and group. Keep the existing capability restrictions intact. If setup added your user to a group, start a new login session before using the address service as that user.

## Linux validation

Local acceptance on **Ubuntu 24.04, Python 3.12, WSL2 with systemd** verified:

- HTTP Agent Serving and SSE streaming.
- Hosted MCP tool discovery and invocation.
- Real headless Chromium navigation, clicking and screenshots.
- Computer Runtime enrollment, WSS transport, file operations and command execution through a local Community Cloud.
- Non-root IPv6 allocation, JWT invocation and address release in an isolated network namespace.
- Automatic service recovery, identity retention and Cloud reconnection after restarting the Linux environment.

The SDK regression suite reported **354 passed and 17 skipped**. IPv6 acceptance used the service group fix above. Cloud screenshot acceptance also required the Community server's Django `MEDIA_ROOT` to point to its configured writable media storage directory.

Public Internet IPv6 ingress, real upstream DHCPv6 and bare-metal Linux acceptance remain separate checks. These results do not certify every Linux distribution or every optional integration. macOS has not received equivalent end-to-end acceptance; Intel macOS users also need to account for the `computer` extra's cryptography source-build requirements.

## Explore the examples

### Native MCP on per-Agent IPv6 addresses

The source SDK can serve a real FastMCP Streamable HTTP endpoint alongside the
existing Nexus Invoke endpoint. Install `nexilume[fastmcp]` (FastMCP 3.4.7–3.x)
and pass a `FastMCP` or `NexusMCPServer` instance:

```python
from nexus_agent import HmacJwtServerAuth, NexusAgent
from nexus_agent.fastmcp import NexusMCPServer

mcp = NexusMCPServer("IPv6 Agent")

@mcp.tool
def echo(message: str) -> dict:
    return {"echo": message}

# Load the dedicated secret from your private configuration, not source control.
auth = HmacJwtServerAuth(secret, issuer="urn:example:agents",
                         audience="agent://demo/echo")
agent = NexusAgent.public_ipv6(
    "auto", agent_id="echo", tenant="demo", auth=auth,
    mcp=mcp, port=9443, mcp_port=9444,
    cert_file="agent.crt", key_file="agent.key",
    tls_server_name="echo.example.com", ca_bundle_id="example-ca",
)
agent.run()
```

`auto` uses the existing local addressd service to allocate a host-owned `/128`.
Both listeners bind **only** that IPv6 address, share the lease, and shut down
together. `mcp_port` defaults to the Invoke port plus one; omit `mcp` to retain
the dependency-free, Invoke-only behavior. MCP-only Agents do not need a dummy
Invoke capability. No Cloud enrollment or OpenWrt forwarding is implied.

- Nexus clients: the unchanged `/agent/v1/invoke` and `/agent/v1/invoke-stream`.
- Standard FastMCP clients: `agent.mcp_url`, ending in `/mcp`. TLS uses the
  configured certificate DNS name, which must resolve to the Agent IPv6 address.
- The native endpoint serves tools, resources, prompts and client callbacks
  through FastMCP, not `FastMCPBridge`. No tools are implicitly mapped to Invoke.
- `HmacJwtServerAuth` is reused, including signature, issuer, audience, expiry,
  scope and listener-tenant checks. Signed source identity identifies the caller.
  Use a distinct audience per Agent. Session IDs are bound to the authenticated
  caller and listener; another caller cannot reuse or delete the session.
- To use FastMCP's own OAuth/auth provider, configure it on the FastMCP instance
  and explicitly use `auth="none"` for Invoke. This **does not secure Invoke**;
  expose no Invoke capabilities unless unauthenticated access is intentional.
  Combining that provider with inherited Nexus JWT is rejected as ambiguous.
- TLS/client-CA settings apply to both listeners. Clients must trust the CA and
  validate the hostname. `ca_bundle_id` is an identifier, not automatic CA delivery.
- MCP request bodies are bounded by `max_request_bytes`. Native responses are
  streamed by FastMCP; Nexus Invoke's response/event limits and replay store do
  not apply to MCP. A new connection works after disconnect, but restoring an
  interrupted response or replaying side effects is not automatic.

This is **direct IPv6 MCP**, not full MCP forwarding through Open Mesh. Mesh
capability discovery and the existing tool bridge remain separate; resources,
prompts and bidirectional MCP callbacks are not added to Mesh by this option.
Native FastMCP Tasks require its task dependencies and backend and are not
certified by the tests below. This source addition is not in the 0.48.0 release.

Reproducible loopback compatibility checks (Python 3.10+, IPv6 enabled):

```bash
python -m pip install -r tests/requirements-native-mcp.txt
python -m unittest discover -s tests -p 'test_public_ipv6_mcp.py' -v
```

These checks exercise real HTTP, callbacks, mutual Agent calls, JWT isolation,
cancellation, active-stream shutdown and TLS/mTLS. Native MCP requires
`mcp>=1.30,<2`, which includes the upstream SSE memory-stream cleanup fix;
older installed stacks are rejected before opening the listener. Shutdown is
listener-local: it ends active responses, cancels unfinished tools, and does not
stop another Agent. Windows retains asynchronous subprocess support. The
regression checks also reject teardown warnings, leaked transports and incomplete
HTTP responses; they do not certify all upstream examples or public
Internet reachability. The existing privileged Windows `/128` acceptance can
also run native checks with `--native-mcp`; only run it on an authorized test NIC.

### Caller-authorized Mobile actions

Declare the required Mobile scopes, then let the caller attach and authorize a
paired phone. Both `ctx.mobile` and `ctx.aio.mobile` support observation, capture,
text/coordinate taps, typing, swipe, Back, Home, recent apps, opening an app and
waiting for visible text. Home and recent apps use `mobile.press_back` (system
navigation); long-press uses `mobile.tap`. No additional manifest scopes or
OpenWrt firmware upgrade are needed for these actions.

```python
status = ctx.mobile.status()
if "long_press" in status.supported_actions:
    ctx.mobile.long_press(x=420, y=860, coordinate_space="pixels", duration_ms=750)
ctx.mobile.press_home()
await ctx.aio.mobile.press_recents()
await ctx.aio.mobile.swipe(0.5, 0.8, 0.5, 0.25, coordinate_space="normalized")
screen = await ctx.aio.mobile.capture_screen()
```

Coordinates accept `auto` (the backwards-compatible default), `normalized`
(`0..1`) or `pixels`. Invalid coordinates and durations are rejected before
dispatch. New actions require a compatible Nexus Cloud and an Android APK that
advertises support; old APKs fail explicitly rather than reporting success.
The status action list is empty on older Clouds that do not advertise it.
Caller approval, Run ownership and the original scopes are still enforced.
Live video is managed by Console and Android screen-sharing consent; SDK actions
use the authorized command channel, not video streaming or automatic consent.

Run these from a source checkout and read each example's configuration before starting it.

| Feature | Example |
| --- | --- |
| Registration and a first routed call | [router_echo_agent.py](examples/router_echo_agent.py) |
| Callable agent and streaming | [friendly_agent.py](examples/friendly_agent.py) |
| The same application on OpenWrt or in Cloud | [dual_runtime_agent.py](examples/dual_runtime_agent.py) |
| Existing FastMCP tools through OpenWrt | [fastmcp_agent.py](examples/fastmcp_agent.py) |
| Hosted MCP with Run feedback | [hosted_fastmcp_agui_agent.py](examples/hosted_fastmcp_agui_agent.py) |
| A2A serving and calling | [a2a_agent.py](examples/a2a_agent.py), [a2a_call.py](examples/a2a_call.py) |
| Direct IPv6 calls | [direct_ipv6_caller.py](examples/direct_ipv6_caller.py) |
| Per-agent addresses on a shared host | [host_alias_ipv6_agent.py](examples/host_alias_ipv6_agent.py) |
| Native MCP and Invoke on one IPv6 address | [public_ipv6_mcp_agent.py](examples/public_ipv6_mcp_agent.py) |
| Caller-authorized browser control | [browser_session_agent.py](examples/browser_session_agent.py) |
| Run files and audio | [router_file_audio_agent.py](examples/router_file_audio_agent.py) |
| Follow-up instructions during a Run | [router_follow_up_agent.py](examples/router_follow_up_agent.py) |
| Two Agents calling each other through OpenWrt | [distributed_agents.py](examples/distributed_agents.py) |
| Calling a specific remote agent | [targeted_invoke.py](examples/targeted_invoke.py) |

Examples may require a configured router, Cloud, IPv6 transport or caller permissions. Some IPv6 examples explicitly disable authentication for controlled testing; review those settings before making a listener reachable outside your test network.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `nexus-computer` or `nexus-agent` is not found | Activate the virtual environment where you installed the wheel. |
| An optional module is missing | Install its extra, for example `python -m pip install "nexilume[computer,browser]"`, in the active environment. |
| Router discovery fails | Set `NEXUS_ROUTER_URL` to the configured Agent Access Proxy and check reachability. |
| Registration works but invocation fails | Verify that the router can reach the agent's advertised host address and port. |
| Computer stays `reconnecting` | Check `nexus-computer logs`, Cloud availability and TLS trust. |
| Browser capability is unavailable | Install the browser and configure its executable in the Runtime service environment. |
| Screenshot upload fails on Community Cloud | Check server storage permissions and the configured Django media directory. |
| IPv6 setup reports a socket group error | Apply the 0.46.2 service override above, then run `nexus-agent ipv6 doctor`. |
| A local IPv6 call works but an external call fails | Check upstream routing, firewall rules and transport security from an external host. |
| Cloud reports `PYTHON_PROFILE_UPGRADE_REQUIRED` | Upgrade the deployment's SDK profile, rebuild and redeploy the agent. |

When reporting a problem, include your OS, Python version, SDK version, the command you ran and a redacted error message. Do not include pairing links, access tokens, private keys or private Run data.

## Contribute

See [Contributing](https://github.com/Nexilume-AI/nexus-agent-sdk-python/blob/main/CONTRIBUTING.md) for development and testing, [Changelog](https://github.com/Nexilume-AI/nexus-agent-sdk-python/blob/main/CHANGELOG.md) for release history, and [Issues](https://github.com/Nexilume-AI/nexus-agent-sdk-python/issues) for bug reports.

The Apache License 2.0 (modified) applies to this SDK. Third-party dependencies retain their own licenses; the SDK license does not cover the private Nexus Enterprise distribution.

### Licensing conditions

Nexus is licensed under a modified version of the Apache License 2.0, with the following additional conditions. Multi-tenant service operation and removal of existing Nexus UI branding require prior written authorization. Earlier Apache-2.0 grants and third-party licenses remain unchanged. Contributions require explicit agreement permitting commercial use and future relicensing. See [LICENSING.md](LICENSING.md). Authorization contact: **cary.nexilume@outlook.com**.
