# Contributing

Use GitHub Issues for reproducible bugs and feature requests, and pull requests for changes.
Include the SDK version, Python version, OS, minimal reproduction, and expected behavior.
Remove tokens, device identifiers, private addresses and user data from reports.

Use Python 3.10+ to build from source:

```sh
python -m venv .venv
. .venv/bin/activate
# Windows: .venv\Scripts\Activate.ps1
python -m pip install -e '.[computer,browser,fastmcp,a2a]' pytest build twine
python -m pytest tests -q
python -m build
python -m twine check dist/*
python scripts/verify_artifacts.py
```

Core tests run locally with mock services and loopback HTTP. Optional A2A/FastMCP tests
require the respective extras. Hardware, Docker and separate Server integration tests
report explicit skips when their prerequisites are absent. A core test pass is not
acceptance of those integrations. Tests must not use production credentials or networks.

Changes require explicit contributor agreement acceptance before merge. Keep dependencies optional where possible.

For the two Server boot integration tests, explicitly set `NEXUS_SERVER_SOURCE` to a separate Server source checkout. Without it those tests skip.

## Contribution licensing

Nexus-authored changes are distributed under the Nexus Community License 1.0.
Read [LICENSE](LICENSE), [LICENSING.md](LICENSING.md) and the
[Nexus Contributor License Agreement](CONTRIBUTOR_LICENSE_AGREEMENT.md).
Every contributing author must explicitly accept that agreement for their PR
before merge; maintainers must record the acceptance as described there.
Contributors retain copyright while permitting commercial use, dual licensing
and future relicensing. Historical contributions and third-party code are not
automatically subject to the new grant. Preserve all upstream notices.

许可咨询：cary.nexilume@outlook.com。每位贡献者须对本 PR 明确同意贡献者协议；
仅勾选模板或由维护者代为声明不构成其他作者的同意。
