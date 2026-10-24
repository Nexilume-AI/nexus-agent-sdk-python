# FastVideo Distillation MCP

Distill supported pretrained video models remotely, follow background jobs, and
download student weights, reports and paired evaluation clips through MCP.

## Choose a model family

| Family | Input | Export |
| --- | --- | --- |
| Wan 2.1 T2V 1.3B | Text prompts | Four-step DMD2 student |
| MiniMax H3 T2VA | Text prompts | Eight-forward audio/video student with merged LoRA weights |

Each family supports its pinned official checkpoint, compatible private
transformer safetensors, and previously exported students. Call `list_models`
to check current readiness. These recipes retain the base parameter count;
they reduce the target sampling schedule.

H3 uses dense Torch SDPA and native FastVideo DMD2 with a frozen teacher and
rank-16 student/critic adapters. The exported student contains merged full
transformer weights and its inference schedule. This is independent of the
upstream VSA FastH3 checkpoint. Ref2VA and arbitrary model architectures are
outside this deployment's scope.

## Run from your MCP client

1. Check `get_fastvideo_status` and `list_recipes`.
2. Create a private workspace and retain its ID and capability.
3. Choose a `model_id` with `list_models`, then validate your prompts and settings.
4. Call `start_distillation` to receive a background job ID.
5. Use `job_status` to read progress, or `cancel_job` to stop it.
6. List artifacts and call `get_artifact_download` for authenticated HTTPS downloads.

Outputs include native checkpoints, exported student safetensors, resolved
configuration, per-iteration metrics, logs, paired teacher/student clips and
a report. Exports must contain finite tensors and changed weights, pass native
strict reload verification, and include full-file SHA256 checksums. H3 paired
clips include an audio track.

## Bring your own checkpoint

Call `begin_model_import` with the explicit family and each transformer shard's
size and SHA256. Upload the first batch using the returned links, then request
links for remaining shards with `renew_model_upload`. Complete uploads before
`finalize_model_import`. The entire checkpoint is checked for integrity, tensor
names, shapes and finite values before it becomes selectable. Use
`register_model_from_job` to reuse a completed student.

Wan accepts up to eight shards / 16 GiB; H3 accepts up to 64 shards / 160 GiB.
Each shard may be at most 8 GiB. Verified base components provide the tokenizer,
text encoder, VAE and configuration. Caller-supplied Python, pickle, arbitrary
URLs and custom training YAML are not accepted.

## Download and resume

File links use a ten-minute, single-use redemption ticket and a file-specific
session lasting up to one hour. HTTP Range, ETag and SHA256 support resumable
downloads of large weights. Renew expired links through MCP and resume from
the previous byte offset. Revocation or closing a workspace ends file access.
Files remain on the Agent host; no model API key or account session is exposed.

## Runtime and evaluation

This host uses SM60 GPUs with dense Torch SDPA. Wan uses four GPUs and up to
17 frames. H3 uses 16 healthy GPUs and exactly 124 frames, approximately five
seconds at 24 FPS. Both support preview dimensions up to 256×256, 4–64 prompts
and up to 4,000 training iterations within a bounded job duration. H3 dimensions
must be divisible by 32 and its iteration count by five. One GPU job runs at a
time, with at most one queued or running job per caller.

Nexus verifies caller identity; the same API key represents one caller.
Workspaces and artifacts are isolated. Training runs in networkless, nonroot
containers with read-only base models. Interrupted jobs are reported explicitly.

Short successful runs verify the pipeline, not production model quality. Paired
clips and single cold-run timings do not establish a general speedup or a
representative quality benchmark. Evaluate a larger held-out dataset before
adopting a distilled student. No VSA, FlashAttention or modern tensor-core
performance is implied on this host.

FastVideo and this adapter use Apache-2.0. Model weights and derivatives remain
subject to their own licenses, including the **MiniMax H3 Community License**.
This hosted integration is independent of the upstream projects.
