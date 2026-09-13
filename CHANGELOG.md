# Changelog

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
