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

Changes are licensed under Apache-2.0. By submitting a contribution you confirm you have
the right to contribute it under that license. Keep dependencies optional where possible.

For the two Server boot integration tests, explicitly set `NEXUS_SERVER_SOURCE` to a separate Server source checkout. Without it those tests skip.
