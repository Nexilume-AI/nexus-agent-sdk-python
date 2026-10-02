# Releases

GitHub Releases distribute the wheel, source distribution and SHA-256 checksums.
The repository includes an OIDC PyPI publishing workflow. Account-side Trusted
Publisher authorization must be verified before using it. Do not advertise an
index installation until the release is actually available on PyPI.

The public distribution name is `nexilume`; the import package
remains `nexus_agent` and the `nexus-computer` command is unchanged. The PyPI name
`nexus-agent-sdk` belongs to a different project and must not be recommended as
an installation command for this SDK.

1. Update the version in `pyproject.toml`, `src/nexus_agent/__init__.py` and changelog.
2. Run CI, including standalone tests, build, metadata validation and archive comparison.
3. Build from the reviewed commit with `python -m build`; run
   `python -m twine check dist/*` and `python scripts/verify_artifacts.py`.
4. Tag that commit as `vVERSION` and attach those exact artifacts and `SHA256SUMS`
   to its GitHub Release. Do not package local credentials or the parent workspace.

## PyPI authorization

Create a PyPI account and configure a pending Trusted Publisher for:

| Field | Value |
| --- | --- |
| Project | `nexilume` |
| Owner | `Nexilume-AI` |
| Repository | `nexus-agent-sdk-python` |
| Workflow | `publish-pypi.yml` |
| Environment | `pypi` |

See [PyPI's setup instructions](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).
The workflow is manually dispatched at a reviewed release tag and builds/tests the tagged
source before publishing using OIDC. No API token belongs in this repository or chat.
Enable it only after the account-side publisher is configured. An unused JSON API name
does not guarantee that PyPI will permit registering it.

After publishing, inspect the PyPI release metadata and install the exact new
version from `https://pypi.org/simple` in a fresh environment. Verify
`from nexus_agent import NexusAgent`, `nexus-computer --help`, the uploaded
artifact hashes, declared extras and the bundled LICENSE. Only then change
README installation instructions to the public-index command.

The current source carries Apache License 2.0 (modified). Complete its rights
review before release, include the contribution agreement in the source archive,
and choose a new version for a license-changing release; do not overwrite an
existing Apache-2.0 release or reuse its upload filenames.
