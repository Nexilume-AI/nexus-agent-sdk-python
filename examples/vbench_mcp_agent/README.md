# VBench MCP adapter

An outbound Nexus SDK / OpenWrt Relay adapter for pinned upstream VBench
`fd18b3d055cb0fc6f066ca90fe2c3c8cbb698490`. This is a custom-input, seven-metric
deployment, not the complete VBench/VBench++/VBench 2.0 benchmark.

See [overview.md](overview.md) for tool workflow, metric semantics and limits.

## Components

- `agent.py`: SDK registration with typed tools and streaming Relay responses.
- `service.py`: verified-caller workspaces, uploads, FIFO job queue, cancellation,
  artifact listing and 24-hour idle cleanup.
- `file_bridge.py`: existing outbound authenticated upload/download bridge.
- `runner.py`: media validation and native VBench evaluation in a container.
- `Dockerfile`: dedicated image layered over the installed SM60-compatible
  Torch base. The FastVideo and VBench service images remain separate.
- `check.py`: actual seven-metric GPU evaluation and isolation checks.

Only the SDK-trusted caller identity is accepted. The same API key is one caller.
Workspace capability hashes are persisted; plaintext capabilities are returned
once. HTTP file tickets/runtime tokens are never written into public reports.
No caller-supplied URLs, Python code, evaluator weights or shell commands run.
Containers use one assigned GPU, four CPUs, 24 GiB memory, a read-only root,
nonroot UID, no network and no additional Linux capabilities.

## Installation

1. Clone the pinned VBench source into `upstream/`; save its commit to
   `upstream.commit`. Provision official checkpoints in `models/` and record
   hashes in `models/manifest.json`. CLIP checkpoint hashes are checked against
   their upstream content-addressed URLs.
2. Build `nexus-vbench-mcp:fd18b3d055cb`. `constraints.txt` prevents upgrading the
   deployed Torch 2.12/CUDA 12.6 runtime. This GPU family requires its SM60 build.
   The upstream package installer only allows old CUDA versions; we load the
   pinned source directly and validate the actual metric implementations.
3. Produce `demo.mp4`, a 2-second, 256x256, 8fps test pattern. Run `check.py`
   with an operator-only readiness marker before installing the service. Keep
   `runtime-ready.json` only after all seven real metric checks pass.
4. Adapt `nexus-vbench-agent.service` paths, SDK import path, OpenWrt bridge
   address and GPU device mapping for your installation, then enable the unit.
5. Complete the English Marketplace profile and free pricing, publish, and
   verify a real request through the public Cloud MCP endpoint.

The image includes a torchvision compatibility alias for the older pyiqa
import path. Official operator-provisioned model weights are loaded using
legacy Torch pickle compatibility; callers cannot supply checkpoints.
Video decoding and evaluation happen only inside the bounded container.

Uploads and report files survive service restarts. In-progress jobs become
`interrupted` and are never silently marked successful. Closing a workspace
or its 24-hour idle expiry permanently deletes its files and revokes links.

VBench is Apache-2.0. Preserve its LICENSE and third-party/model license terms
when redistributing the image or evaluator checkpoints.
