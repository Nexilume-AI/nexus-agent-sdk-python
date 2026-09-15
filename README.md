# Nexus Agent SDK for Python

Build callable Python agents, connect them through Nexus OpenWrt, expose them as MCP tools, or give an authorized Nexus agent access to your computer.

The core SDK has no third-party runtime dependencies. Browser, Computer Runtime, MCP and A2A support are optional installations.

- **Distribution:** `nexus-openwrt-agent-sdk`
- **Python import:** `nexus_agent`
- **Source and downloads:** [GitHub](https://github.com/Nexilume-AI/nexus-agent-sdk-python) · [Releases](https://github.com/Nexilume-AI/nexus-agent-sdk-python/releases)
- **License:** [Apache-2.0](LICENSE)

## Choose your starting point

| I want to... | Start here |
| --- | --- |
| Try a Python agent without a router or Cloud account | [Run your first agent](#run-your-first-agent) |
| Register an agent with Nexus OpenWrt | [Connect to OpenWrt](#connect-to-openwrt) |
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

### 2. Install a release wheel

Download the `.whl` file from [GitHub Releases](https://github.com/Nexilume-AI/nexus-agent-sdk-python/releases), then install it in your environment. For release 0.46.3:

```sh
python -m pip install ./nexus_openwrt_agent_sdk-0.46.3-py3-none-any.whl
python -c "import nexus_agent; print(nexus_agent.__version__)"
```

Replace the filename with the wheel you downloaded. This project currently distributes installation packages through GitHub Releases; **PyPI publication is not yet available**. The PyPI package named `nexus-agent-sdk` belongs to a different project.

To include optional features, add extras to the local wheel path:

```sh
python -m pip install "./nexus_openwrt_agent_sdk-0.46.3-py3-none-any.whl[computer,browser,fastmcp,a2a]"
```

| Extra | Enables |
| --- | --- |
| `computer` | The outbound Computer Runtime connection to Nexus Cloud |
| `browser` | Browser automation through Playwright; a browser binary is also required |
| `fastmcp` | Hosted MCP tools and the FastMCP bridge |
| `a2a` | Integration with the official A2A SDK |
| `fastmcp-tasks` | Optional FastMCP Tasks integration |

### Install from source instead

Use this option to run the repository examples or work with local changes:

```sh
git clone https://github.com/Nexilume-AI/nexus-agent-sdk-python.git
cd nexus-agent-sdk-python
python -m pip install ".[computer,browser,fastmcp,a2a]"
```

Use `python -m pip install .` for the core only. Commands below that reference `examples/` run from this repository directory; the wheel does not install the example files into your working directory.

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

### Upgrade an existing Computer Runtime

Download the 0.46.3 wheel from [GitHub Releases](https://github.com/Nexilume-AI/nexus-agent-sdk-python/releases/tag/v0.46.3). Activate the **same virtual environment used to install Runtime**, then run:

```sh
python -m pip install --upgrade "./nexus_openwrt_agent_sdk-0.46.3-py3-none-any.whl[computer,browser]"
nexus-computer restart
nexus-computer status
```

Keep your existing Runtime configuration and device keys. Upgrading in the same environment preserves pairings; you do not need a new pairing link. Installation still comes before pairing for new computers.

Version 0.46.3 includes the SOCKS dependency used by WebSocket system-proxy support, identifies Runtime HTTP/WebSocket requests with `Nexus-Computer/0.46.3`, and fixes browser Enter input for macOS/Linux pipe-based terminals. Proxy availability and Cloud firewall rules still determine connectivity. The terminal is a pipe-based shell, so full-screen tools that require a PTY are not supported by this fix.

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
| Caller-authorized browser control | [browser_session_agent.py](examples/browser_session_agent.py) |
| Run files and audio | [router_file_audio_agent.py](examples/router_file_audio_agent.py) |
| Follow-up instructions during a Run | [router_follow_up_agent.py](examples/router_follow_up_agent.py) |
| Calling a specific remote agent | [targeted_invoke.py](examples/targeted_invoke.py) |

Examples may require a configured router, Cloud, IPv6 transport or caller permissions. Some IPv6 examples explicitly disable authentication for controlled testing; review those settings before making a listener reachable outside your test network.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `nexus-computer` or `nexus-agent` is not found | Activate the virtual environment where you installed the wheel. |
| An optional module is missing | Install its extra using the wheel path or source checkout. |
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

The Apache-2.0 license applies to this SDK. Third-party dependencies retain their own licenses; the SDK license does not cover the private Nexus Enterprise distribution.
