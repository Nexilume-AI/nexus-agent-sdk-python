# Optional components on the deployment host

Build these locally; generated binaries, source bundles and commercial libraries
do not belong in this repository. Preserve the upstream licenses with the local
installation and retain the core image for rollback.

| Component | Pinned source |
| --- | --- |
| Verible static Linux x86-64 | `chipsalliance/verible`, `v0.0-4296-g0f262651` |
| Surfer WASM and surver | `surfer-project/surfer`, `86eedfd0cda70fc0a61ab200ebf37aabf97c5cde` |
| fstdumper | `semify-eda/fstdumper`, `3c7506333d2c9ad098d4526209621ea401a077ba`, with Wave's Xcelium patch |

Verible archive SHA256:
`8569defb891d2316067613ea00442af28a7a09d405d95b54c0c91f9942d26635`.

Build both viewer components from the same checkout and lockfile using Rust
1.92.0 and Trunk 0.21.14. Both must report Wellen 0.25.6. Assemble `assets/surver`
and `assets/wasm/index.html`, retain the license and provenance, and remove the
upstream service workers and sample waveforms as described in Wave's
`deploy/build_viewer_assets.sh`. Wave supplies its own viewer shell/service worker.

Build fstdumper using the pinned Wave checkout's `deploy/build_fstdumper.sh`.
The resulting plugin requires Xcelium for actual simulation; building it alone
does not install or license Xcelium. Verible is available as CLI binaries; the
current Wave RTL engine uses pyslang.

Use `Dockerfile.optional` with the prepared assets, extracted Verible directory,
fstdumper checkout and `PROVENANCE.json` in the build context. Build a separate
image as `nexus-wave-mcp:0b8c5fff11c9-optionals-20261022` and set the bridge's
`WAVE_IMAGE` to that tested tag. The runtime image adds
gcc, g++, make, patch and zlib headers. Keep the networkless workers, read-only
root filesystem, caller binding, tmpfs limits and fair queue intact.

Installing viewer assets does not create a remote browser route. The upstream
viewer listens on worker-local loopback. The updated bridge publishes the five
viewer tools through the separate Cloud capability route described in
`PUBLIC_VIEWER.md`. Build the updated worker on top of the optional image
using `docker build -f Dockerfile.viewer -t nexus-wave-mcp:public-viewer-20261022 .`, tag it
`nexus-wave-mcp:public-viewer-20261022`, and deploy matching Cloud assets. Never present
a worker's `127.0.0.1` URL as a usable remote link.

FSDB conversion requires the operator's compatible Synopsys FsdbReader libraries
(`libnffr.so`, `libnsys.so`). They are not supplied here. Without them, retain an
explicit unavailable status and use VCD/FST. If supplied later, build the converter
into the image; do not remove the workers' `noexec` tmpfs restriction to build it
on demand.
