# Changelog

## Unreleased

- Use the heading "Open Source License" and describe a modified version of
  Apache License 2.0 with additional conditions, without changing the
  multi-tenant, attribution or contribution conditions. Current-source metadata
  uses `LicenseRef-Nexus-Additional-Terms-1.0`. Previously published artifacts
  retain their original license and identifier; do not replace their files.

## 0.47.1 — 2026-10-01

- Verify decoded Browser frame dimensions and nonblank pixels across platforms, instead of assuming JPEG byte size is identical across fonts and operating systems.
- Add the conditional `tomli` dependency and parser fallback for Computer Tool Setup on Python 3.9/3.10; the core still has no third-party dependencies.
- Declare directly used MCP/A2A dependencies in their respective extras. Preserve the `fastmcp-tasks` extra name but use official `fastmcp[tasks]` instead of the unresolvable `fastmcp-tasks<1` requirement.
- Require Python 3.10+ for the Browser extra through Playwright 1.63+; avoid silently selecting old browser builds and source-only dependencies on Python 3.9. Real browser checks reproduced a slow-page screenshot timeout on 1.61 and passed on 1.63. Core and Computer Runtime retain Python 3.9 support.
- Check each extra independently and at its declared lower bounds, including Windows helpers and Python 3.9 wheel installs. Browser binary/OS prerequisite checks remain separate from package installation tests.

## 0.47.0 — 2026-10-01

- Publish the SDK on PyPI as `nexilume`; Python imports remain `nexus_agent`
  and CLI names, including `nexus-computer`, stay unchanged.
- Update Runtime self-update, Windows service packaging and optional-dependency
  installation guidance to use the public distribution name.
- Use Nexus Community License 1.0 for this new release: multi-tenant service
  operation and removal of supplied UI branding require written authorization.
  Earlier Apache-2.0 releases and third-party licenses remain unchanged.
- Add the prospective contributor agreement and licensing explanation to the
  source distribution. Commercial contact: cary.nexilume@outlook.com.
- When migrating from a previous GitHub wheel, uninstall `nexus-openwrt-agent-sdk`
  before installing `nexilume`; do not install both distributions in the same
  environment because they provide the same import package.

## 0.46.5

- Use a real POSIX PTY and an interactive shell for macOS/Linux Computer terminals, restoring the initial prompt, input echo, line editing and Ctrl+C.
- Apply initial terminal dimensions and subsequent resize requests on both RPC and live-stream paths.
- Drain final streamed output and release terminal resources when the shell exits or the stream closes. Windows retains its existing pipe transport.
- Add real PTY regressions for prompts, zsh editing, resizing, interruption, stream output and process cleanup. Restart Runtime and open a new terminal after installing the updated SDK; existing pairings remain valid.

## 0.46.4

- Select zsh for automatic Computer Runtime terminals on macOS, with `/bin/zsh` as the fallback path. Explicit bash/sh choices remain unchanged.
- Requires the accompanying Cloud update that forwards `auto` to Runtime instead of resolving it to sh on the server. Restart Runtime after upgrading and open a new terminal session.
- Add shell-selection regressions and a real macOS zsh execution test.
- Add a runnable two-Agent OpenWrt example with bidirectional calls and English README instructions.


## 0.46.3

- Include SOCKS proxy support in the `computer` extra.
- Send an explicit Nexus Computer User-Agent on HTTP uploads, session requests and WebSocket connections.
- Normalize browser Enter input for POSIX pipe-based terminals; preserve Windows input handling.
- Keep the existing install-then-pair workflow and device configuration during upgrades.
- Add macOS to the CI matrix and regression tests for headers and terminal input.

Runtime fixes adapted from [Nexus Cloud Community PR #5](https://github.com/Nexilume-AI/nexus-cloud-community/pull/5), without its generated installer or site-packages patches.

## 0.46.2

- First standalone public distribution, named `nexus-openwrt-agent-sdk`.
  Python imports remain `nexus_agent`; the unrelated PyPI package `nexus-agent-sdk`
  is not this SDK. Install this release's wheel or source checkout.
- Accept the transient Cloud state `unavailable` and continue bounded readiness polling
  through `pending` to `ready`, including OpenWrt cold-boot recovery.
- Include OpenWrt and hosted MCP examples, IPv6 helpers, optional Computer/Browser support,
  standalone tests and package verification.
- Update Windows service distribution lookup to the new public package name.

Earlier versions were developed in the Nexus workspace. This repository starts with a
clean source snapshot and does not include that workspace's history or server code.
