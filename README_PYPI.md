# nexilume — Nexus Python Agent SDK

[![PyPI](https://img.shields.io/pypi/v/nexilume.svg)](https://pypi.org/project/nexilume/)
[![Python](https://img.shields.io/pypi/pyversions/nexilume.svg)](https://pypi.org/project/nexilume/)

Build callable Agents and connect an authorized Computer to Nexus Cloud with
the same Python SDK. Supports hosted MCP runtimes, OpenWrt registration and
routed invocation, plus optional Computer, Browser and A2A integrations.

## Install

Use a virtual environment. The dependency-free core supports Python 3.9+;
Python 3.12 is recommended for optional integrations.

```sh
python -m pip install nexilume==0.49.1
python -m pip install "nexilume[fastmcp]==0.49.1"
```

Use `python -m pip install --upgrade nexilume` to upgrade the core SDK.
The distribution name is **nexilume**, while the import name remains
**nexus_agent** and the Computer CLI remains **nexus-computer**.
The PyPI project named `nexus-agent-sdk` is unrelated.

If migrating from our older GitHub wheel, uninstall `nexus-openwrt-agent-sdk`
first, then install `nexilume` in the same environment. Do not install both:
they contain the same import namespace. Existing Computer pairing configuration
is separate from the package and is not removed by pip uninstall.

## A hosted MCP Agent

```python
from nexus_agent import NexusAgent

agent = NexusAgent(runtime="hosted", cloud_name="Echo Agent")

@agent.capability("demo.echo")
def echo(payload):
    return {"echo": payload}

if __name__ == "__main__":
    agent.run()
```

Install the `fastmcp` extra to run this example. Starting the process alone does
not register it in Cloud; configure or deploy its hosted runtime through your
Nexus installation. OpenWrt LAN registration is a separate supported mode.

## Computer Runtime

```sh
python -m pip install "nexilume[computer,browser]==0.49.1"
nexus-computer setup "<pairing-url-from-your-cloud>"
nexus-computer status
```

Run as your ordinary OS user. Wait until connected, then attach and authorize
the Computer in Cloud. No inbound SSH port is needed. Browser control requires
a compatible local Chromium browser and the `browser` extra. Never share
pairing links, device keys or tokens.

## IPv6 and MCP recovery in 0.49.1

OS-confirmed IPv6 conflicts now trigger bounded automatic reallocation: the
failed address is removed, another candidate is tried, and known conflicts are
temporarily avoided. DAD, firewall protection and other addresses stay intact.
Windows address readiness no longer depends on the system language.

```sh
nexus-agent doctor
nexus-agent repair --yes
nexus-agent run --repair agent.py
```

Dependency repair is explicit, limited to recognized problems and verified before
execution. Agent failures are never automatically replayed. Existing Windows
address services use a separate SDK copy: after pip upgrade, follow the
[service update instructions](https://github.com/Nexilume-AI/nexus-agent-sdk-python/blob/main/README_GUIDE.md#configure-agent-ipv6-on-windows).

## Attached Computer binary files

Version 0.48.0 adds synchronous and asynchronous binary Workspace transfers:

```python
data = ctx.workspace.read_bytes("images/input.png")
ctx.workspace.write_bytes("images/result.png", data)
ctx.workspace.upload("/agent-local/result.zip", "exports/result.zip")
ctx.workspace.download("exports/result.zip", "/agent-local/download.zip")
```

These operations require a caller-attached Computer and `files.read` /
`files.write` authorization. Upgrade Cloud and Computer Runtime together to
support `workspace.binary.v1`; upgrading this package alone cannot add the
server-side protocol. The SDK never falls back to the Agent host's files.
In-memory operations are limited to 16 MiB; streamed files to 1 GiB. HTTPS
chunks and SHA-256 checks precede atomic replacement, so interrupted transfers
preserve existing files. Use `ctx.aio.workspace` for asynchronous counterparts.
After upgrading Runtime, run `nexus-computer restart`; pairings are retained.

## Optional integrations

Dependency fixes in 0.47.1: Computer
Tool Setup uses `tomli` on Python 3.9/3.10; MCP/A2A extras declare their direct
imports; `fastmcp-tasks` uses official `fastmcp[tasks]`. See the
[compatibility guide](https://github.com/Nexilume-AI/nexus-agent-sdk-python/blob/main/README_GUIDE.md#dependency-compatibility).
Browser/FastMCP/A2A require Python 3.10+; core and Computer support 3.9+.
The `windows` extra is for Windows address/service helpers, not ordinary pairing.
Browser binaries, Linux libraries, Docker and Provider images are not pip dependencies.

| Extra | Purpose |
| --- | --- |
| `fastmcp` | Hosted MCP tools |
| `fastmcp-tasks` | MCP task integration |
| `computer` | Outbound Computer Runtime |
| `browser` | Browser automation integration |
| `a2a` | Agent-to-Agent integration |
| `windows` | Windows service helpers |

## Documentation and support

- [Repository and bilingual guides](https://github.com/Nexilume-AI/nexus-agent-sdk-python)
- [Examples](https://github.com/Nexilume-AI/nexus-agent-sdk-python/tree/main/examples)
- [Issue tracker](https://github.com/Nexilume-AI/nexus-agent-sdk-python/issues)
- [Detailed guide](https://github.com/Nexilume-AI/nexus-agent-sdk-python/blob/main/README_GUIDE.md)

## License

The current source uses **a modified version of the Apache License 2.0, with additional conditions** (`LicenseRef-Nexus-Additional-Terms-1.0`), a source-available license, not unmodified
Apache-2.0 or an OSI-approved open-source license. The complete LICENSE is
bundled in the wheel and source distribution.

Already published packages retain their bundled license and identifier.
Version 0.48.0 includes this license clarification without replacing any
earlier release files.

Personal and single-tenant self-hosted use, including commercial single-tenant
use, is allowed under the license. Operating a multi-tenant service or removing
supplied Nexus UI branding requires prior written authorization. Headless SDK
integration does not require adding a new logo. Earlier Apache-2.0 grants and
third-party licenses remain unchanged. New contributions require the explicit
contributor agreement included in the source distribution.

Commercial licensing: **cary.nexilume@outlook.com**.
