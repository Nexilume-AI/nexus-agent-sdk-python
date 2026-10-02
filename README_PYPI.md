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
python -m pip install nexilume==0.47.1
python -m pip install "nexilume[fastmcp]==0.47.1"
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
python -m pip install "nexilume[computer,browser]==0.47.1"
nexus-computer setup "<pairing-url-from-your-cloud>"
nexus-computer status
```

Run as your ordinary OS user. Wait until connected, then attach and authorize
the Computer in Cloud. No inbound SSH port is needed. Browser control requires
a compatible local Chromium browser and the `browser` extra. Never share
pairing links, device keys or tokens.

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

Already published packages retain their bundled license and identifier. This
source-tree clarification does not replace the artifacts published as 0.47.1;
it will be included only in a separately versioned future release.

Personal and single-tenant self-hosted use, including commercial single-tenant
use, is allowed under the license. Operating a multi-tenant service or removing
supplied Nexus UI branding requires prior written authorization. Headless SDK
integration does not require adding a new logo. Earlier Apache-2.0 grants and
third-party licenses remain unchanged. New contributions require the explicit
contributor agreement included in the source distribution.

Commercial licensing: **cary.nexilume@outlook.com**.
