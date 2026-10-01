# Changelog

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
