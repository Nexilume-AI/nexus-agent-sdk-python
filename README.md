<div align="center">

# Nexus Agent SDK for Python

**Write Python. Publish capabilities. Connect devices.**

[![PyPI](https://img.shields.io/pypi/v/nexilume.svg)](https://pypi.org/project/nexilume/)
[![Python](https://img.shields.io/pypi/pyversions/nexilume.svg)](https://pypi.org/project/nexilume/)
[![License: Apache-2.0 modified](https://img.shields.io/badge/License-Apache--2.0_modified-17251d.svg)](LICENSE)
[![Try online](https://img.shields.io/badge/Try-Nexus_Cloud-b8ef73.svg)](https://cloud.nexilume.com/)
[![Documentation](https://img.shields.io/badge/Read-the_docs-b8ef73.svg)](README_GUIDE.md)
[![Cite the technical report](https://img.shields.io/badge/Cite-technical_report-e8e9e4.svg)](#citation)
[![Repository checks](https://github.com/Nexilume-AI/nexus-agent-sdk-python/actions/workflows/ci.yml/badge.svg)](https://github.com/Nexilume-AI/nexus-agent-sdk-python/actions/workflows/ci.yml)

`Python` · `MCP` · `Computer Runtime`

**English** · [Chinese](README_zh.md)

[Highlights](#highlights) · [Quick start](#quick-start) · [Documentation](#documentation) · [Ecosystem](#ecosystem) · [Contributing](#contributing) · [Citation](#citation)

</div>

> **[Try Nexus Cloud online](https://cloud.nexilume.com/)**: Explore Nexus Cloud in your browser, or self-host to get started.

Build callable Agents, expose MCP tools, register through OpenWrt, and connect an authorized Computer Runtime to Nexus Cloud.

![Nexus Agent SDK for Python: illustrated workflow](docs/media/overview.svg)

*Workflow illustration, not a product screenshot. Connections require the setup and authorization described below.*

## From Python to a private Run

**Enterprise UI, October 1, 2026.** This real Docker-hosted SDK example uses
`NexusMCPServer`, `plan`, `chat.ask()` and private file upload. It is deterministic,
uses no paid model and does not access a personal device.

![A real SDK Agent asks an inline question in Private Display](docs/media/enterprise-inline-question.jpg)

<details>
<summary>View the generated Markdown file</summary>

![The SDK-generated checklist in the Run Files preview](docs/media/enterprise-file-preview.jpg)

</details>

[Reproduce the capture](docs/media/capture-notes.md). Cloud UI is a separate
installation; these Enterprise screenshots do not expand the Community feature set.

## Highlights

| Build | Use | Start here |
| --- | --- | --- |
| **A callable Python Agent** | Dependency-free core; local HTTP serving | [Local round trip](README_GUIDE.md#run-your-first-agent) |
| **An MCP tool server** | Hosted mode with the optional FastMCP integration | [Hosted runtime](README_GUIDE.md#use-a-hosted-mcp-runtime) |
| **Distributed capabilities** | Registration, renewal and routed calls through OpenWrt | [Two-Agent walkthrough](README_GUIDE.md#example-two-distributed-agents-calling-each-other) |
| **An Attached Computer** | Outbound WSS for authorized files, terminal and browser operations | [Computer setup](README_GUIDE.md#set-up-computer-runtime) |

## Quick start

See [dependency compatibility](README_GUIDE.md#dependency-compatibility)
for supported Python versions, optional extras and OS prerequisites in 0.48.0.

Use Python **3.12** for the optional integrations. Core runtime supports Python 3.9+; building from source needs 3.10+. Start in a virtual environment:

```sh
python -m venv .venv
```

Activate with `source .venv/bin/activate` on bash/zsh, or `.venv\Scripts\Activate.ps1` in Windows PowerShell. Install [nexilume from PyPI](https://pypi.org/project/nexilume/). For the core SDK:

```sh
python -m pip install --upgrade nexilume
```

The hosted MCP example below needs the `fastmcp` extra. Pin the 0.48.0 release to reproduce it:

```sh
python -m pip install "nexilume[fastmcp]==0.48.0"
```

Save this as `echo_agent.py`:

```python
from nexus_agent import NexusAgent

agent = NexusAgent(runtime="hosted", cloud_name="Echo Agent")

@agent.capability("demo.echo")
def echo(payload):
    return {"echo": payload}

if __name__ == "__main__":
    agent.run()
```

Run `python echo_agent.py` to start hosted mode. Use the [hosted guide](README_GUIDE.md#use-a-hosted-mcp-runtime) to connect/deploy it; running the process alone does not register it in Cloud. For a complete local HTTP request and expected response, use the [local round-trip tutorial](README_GUIDE.md#run-your-first-agent).

> [!IMPORTANT]
> The PyPI distribution is **nexilume**, imported as **nexus_agent**. The unrelated PyPI package `nexus-agent-sdk` is not this SDK. When migrating from an older GitHub wheel, uninstall `nexus-openwrt-agent-sdk` first in the same environment; these distributions share an import namespace.

## Connect your computer

Install the `computer,browser` extras and a compatible browser, then create a pairing link in your own Cloud installation:

```sh
python -m pip install "nexilume[computer,browser]==0.48.0"
nexus-computer setup "<pairing-url-from-your-cloud>"
nexus-computer status
```

Run as your normal OS user. Wait for `connected`, then attach and authorize the device in Cloud. No inbound SSH port is needed. Do not publish pairing URLs, tokens or device keys.

## Documentation

| Goal | Guide or example |
| --- | --- |
| Install wheels and choose extras | [Installation](README_GUIDE.md#install) |
| Register and invoke through OpenWrt | [Router walkthrough](README_GUIDE.md#connect-to-openwrt) |
| Run the same application in Cloud or on the edge | [Dual runtime example](examples/dual_runtime_agent.py) |
| Browser, streaming, files and A2A examples | [Example directory](README_GUIDE.md#explore-the-examples) |
| Upgrade Computer Runtime without re-pairing | [Upgrade guide](README_GUIDE.md#upgrade-an-existing-computer-runtime) |
| Linux IPv6 and platform acceptance limits | [IPv6](README_GUIDE.md#configure-agent-ipv6-on-linux) · [Validation scope](README_GUIDE.md#linux-validation) |
| Diagnose common errors | [Troubleshooting](README_GUIDE.md#troubleshooting) |
| Follow changes | [Changelog](CHANGELOG.md) |

Windows/Linux/macOS behavior depends on the selected extra, OS and installed browser. Existing platform tests are not equivalent to full device acceptance on every platform; see the validation scope.

## Ecosystem

| Project | Role | Install separately? |
| --- | --- | --- |
| [Nexus Cloud](https://github.com/Nexilume-AI/nexus-cloud-community) | Server, Web Console and bundled Cloud Relay | Main workspace |
| [Python SDK](https://github.com/Nexilume-AI/nexus-agent-sdk-python) | Agent applications and outbound Computer Runtime | Yes |
| [OpenWrt](https://github.com/Nexilume-AI/nexus-openwrt) | Edge registration and capability routing | Optional |
| [Mobile](https://github.com/Nexilume-AI/nexus-mobile) | Authorized Android device integration | Optional |
| [Documentation](https://github.com/Nexilume-AI/nexus-docs) | User guides and reference | Read online or build locally |

Repository access, release availability and compatibility determine which integrations you can install. Cloud installation does not install device runtimes.

## Contributing

Start with [CONTRIBUTING.md](CONTRIBUTING.md). Small reproducible fixes, clearer tutorials, translations and sanitized examples are welcome. Use [Issues](https://github.com/Nexilume-AI/nexus-agent-sdk-python/issues) for reproducible bugs; include versions and redacted diagnostics, never credentials or private files.

Follow [SECURITY.md](SECURITY.md) for security reports. Release checks and CI are not a guarantee of production readiness on every platform.

## Citation

If Nexus supports your research or engineering work, please cite the technical report below, rather than the software repository. [CITATION.cff](CITATION.cff) provides the same report metadata through `preferred-citation`.

Nexilume Research. *Nexus: An Execution Fabric for AI Agents Across Cloud, Edge, and Devices*. Technical Report NX-SYS-2026-001, September 2026.

```bibtex
@techreport{nexilume2026nexus,
  author      = {{Nexilume Research}},
  title       = {{Nexus}: An Execution Fabric for {AI} Agents Across Cloud, Edge, and Devices},
  institution = {Nexilume Research},
  type        = {Technical Report},
  number      = {NX-SYS-2026-001},
  year        = {2026},
  month       = sep
}
```

## License

Nexus-authored source is distributed under [Apache License 2.0 (modified)](LICENSE). Third-party components retain their own licenses and notices. Documentation does not grant rights to separately distributed Enterprise implementation.

### Licensing conditions

Nexus is licensed under a modified version of the Apache License 2.0, with the following additional conditions. Multi-tenant service operation and removal of existing Nexus UI branding require prior written authorization. Earlier Apache-2.0 grants and third-party licenses remain unchanged. Contributions require explicit agreement permitting commercial use and future relicensing. See [LICENSING.md](LICENSING.md). Authorization contact: **cary.nexilume@outlook.com**.

## Attached Computer binary files

SDK 0.48.0 adds `ctx.workspace.read_bytes()`, `write_bytes()`,
`upload()` and `download()`, with async counterparts in `ctx.aio.workspace`.
These APIs are included in 0.48.0. Matching Cloud and Computer Runtime
updates are required; existing `files.read` / `files.write` authorization applies.

In-memory APIs support up to 16 MiB; streaming APIs up to 1 GiB with bounded
HTTPS chunks and SHA-256 verification before atomic replacement. No SSH or
local Agent-host file fallback is used. See the [binary file guide](README_GUIDE.md#attached-computer-binary-files).
