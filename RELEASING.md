# Releases

GitHub Releases distribute the wheel, source distribution and SHA-256 checksums.
PyPI publishing is not configured yet. Do not advertise an index installation until
the package is actually available there.

1. Update the version in `pyproject.toml`, `src/nexus_agent/__init__.py` and changelog.
2. Run CI, including standalone tests, build, metadata validation and archive comparison.
3. Build from the reviewed commit with `python -m build`; run
   `python -m twine check dist/*` and `python scripts/verify_artifacts.py`.
4. Tag that commit as `vVERSION` and attach those exact artifacts and `SHA256SUMS`
   to its GitHub Release. Do not package local credentials or the parent workspace.

## Future PyPI setup

Create a PyPI account and configure a pending Trusted Publisher for:

| Field | Value |
| --- | --- |
| Project | `nexus-openwrt-agent-sdk` |
| Owner | `Nexilume-AI` |
| Repository | `nexus-agent-sdk-python` |
| Workflow | `publish-pypi.yml` |
| Environment | `pypi` |

See [PyPI's setup instructions](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).
The workflow is manually dispatched at a reviewed release tag and builds/tests the tagged
source before publishing using OIDC. No API token belongs in this repository or chat.
Enable it only after the account-side publisher is configured. An unused JSON API name
does not guarantee that PyPI will permit registering it.
