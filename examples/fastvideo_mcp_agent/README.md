# FastVideo Distillation MCP

Nexus adapter for the official FastVideo **model acceleration** workflow.
Pinned upstream: `fa2d1324c60050cd65209c6aaeac470e7fb412f5` (Apache-2.0).

## Input and output

The first recipe is Wan 2.1 T2V 1.3B DMD2: a pretrained teacher and a text
prompt collection produce a four-step student with the same parameter count.
Outputs include native training checkpoints, exported student weights, frozen
configuration, per-iteration metrics, logs, paired validation videos and a comparison report.
At least five training iterations are required: the native recipe updates the
student every fifth iteration. Exports must contain finite tensors and changed
student weights; a SHA256 manifest is saved alongside the report.
This is a training job, not an arbitrary-format model converter.

The administrator provisions the pinned base checkpoint. Callers cannot supply
shell commands, arbitrary YAML `_target_` values, remote URLs or Python plugins.
Support for additional base models requires an installed and tested recipe.

## MCP workflow

1. `get_fastvideo_status` and `list_recipes`: check actual readiness.
2. `create_fastvideo_workspace`: retain the returned workspace capability.
3. `list_models`, then `validate_distillation`: select `model_id`, submit 4–64 training prompts and bounded settings.
4. `start_distillation`: queue a job and return immediately.
5. `job_status` / `cancel_job`: inspect or cancel execution.
6. `list_artifacts` / `get_artifact_download`: download reports, clips and weights
   through authenticated HTTPS with Range, ETag and SHA256 verification. `read_artifact`
   remains available for small 64 KiB reads.
7. `close_fastvideo_workspace`: revoke access after retrieving outputs.

Caller identity comes from Nexus, not client-supplied user IDs. Same API key is
one caller. One running GPU job globally; at most one pending or running job
per caller, bounded FIFO admission, and two workspaces per caller.

## Private checkpoints and resumable files

`list_models` contains the official base and this workspace's imported or derived
checkpoints. Select the explicit `family`: **Wan 2.1 T2V 1.3B** or **MiniMax H3
T2VA**. H3 is usable only after its independent real-weight acceptance passes;
check its `ready` flag. Wan 14B, Wan 2.2, HunyuanVideo and arbitrary architectures
are not supported by these recipes. An imported checkpoint is an alternative teacher/student
initialization, not a new architecture adapter.

1. Run `python transfer_file.py manifest /path/to/transformer.safetensors` for
   each transformer shard. Install `requests` to use this helper.
2. Call `begin_model_import` with `name` and `weight_files` (filename, byte size, SHA256).
3. Open each returned link and select the matching file, or run
   `python transfer_file.py upload /path/to/shard.safetensors` and paste the
   fresh link into its hidden prompt. No Nexus login is delegated to the link.
4. If interrupted, use Upload / resume. After expiry, call `renew_model_upload`
   and continue from the existing offset. The previous link is revoked.
5. Call `finalize_model_import`, then `get_model_import` until `ready`.
   Validation runs without GPU/network in a resource-limited container and checks
   whole-file SHA256, the complete tensor names/shapes and finite tensor values.
6. Pass the returned `model_id` to `validate_distillation` / `start_distillation`.
   `register_model_from_job` also makes a completed student selectable.

Wan imports accept 1–8 safetensors files / 16 GiB total. Ready H3 imports accept
1–64 files / 160 GiB total. Both allow at most 8 GiB per file and four imports
per workspace. Upload links are issued for the first eight files. Upload that
batch, then request links for `remaining_upload_files` using `renew_model_upload`.
Completed upload links are revoked when requesting the next link, so large
checkpoints do not consume a capability for every shard simultaneously.
The verified base supplies configuration, tokenizer,
text encoder and VAE. Python, pickle, caller-provided configuration and remote
fetch URLs are not accepted. Uploads are sealed before validation and training.

Download with `python transfer_file.py download output.safetensors`. This keeps
an interrupted `.part` file and resumes after a new link is supplied; the final
file is renamed only after its declared size and SHA256 match. Do not mix
partial files from different artifacts.

Links have a **10-minute, single-use redemption ticket** in the URL fragment.
Redemption creates a file-specific HttpOnly cookie lasting up to one hour.
`revoke_file_link`, closing the workspace, account/key revocation, or expiry
revokes access. A file capability permits that file's direction only, not account
access. Treat the complete link as a bearer secret until redeemed.

Files stay on the Agent host. The Agent makes outbound HTTPS requests to Cloud;
Cloud handles bounded 4 MiB chunks through Redis and streams HTTP Range replies.
This does not require inbound firewall rules, SSH tunnels, Cloud disk staging or
thousands of MCP tool calls. At most eight chunk exchanges run globally; clients
may resume if a channel is busy or disconnected. Cloud uses ASGI streaming to
avoid collecting multi-GB response bodies in memory.

## Deployment

Build `Dockerfile.base`, then `Dockerfile`. Run `agent.py` with the Nexus SDK
and the existing OpenWrt Router. The host runs the authenticated job controller;
training runs in a networkless, nonroot Docker container with only its job
directory writable and the base model read-only. No public ports are added.

Wan reserves GPU indices 4–7 per job and uses CUDA 12.6 / Torch
SDPA on SM60 hardware. It does not claim VSA, FlashAttention or tensor-core
performance. Training is capped at 256×256, 17 frames, 4000 updates and 24 hours.

### MiniMax H3 provisioning and acceptance

`minimax-h3-t2va` uses the official 33B audio/video transformer with the
Qwen3-VL text encoder. This recipe accepts text prompts, exactly 124 frames
(about five seconds at 24 FPS), dimensions divisible by 32 up to 256×256,
and training steps divisible by five. DMD2 trains rank-16 LoRA student/critic
adapters and exports merged student safetensors with an eight-forward inference
contract. It uses dense Torch SDPA, not the separately published VSA FastH3
checkpoint. These preview dimensions and short acceptance runs do not certify
production output quality or speedup.

On host 11, H3 reserves GPUs 3–14 and 16–19 (GPU 15 failed CUDA health checks),
uses 16-way parameter sharding and eight-way
sequence parallelism, and shares the global FIFO queue with Wan. CPU source
staging avoids materializing all 66 GB of transformer weights on each GPU
before sharding. Text preprocessing and paired audio/video generation use
layerwise offload. Full real-model memory/performance must be validated on
the target hardware before enabling the family.

`provision_h3.py` resumes ModelScope downloads and validates every component
against the pinned official Hugging Face manifest at revision
`42ed227ee7df40d41602854ae760620d6eb651fe`. It installs T2VA components only;
the separate Ref2VA transformer is excluded. Original model manifests are kept
as `.upstream`, and only the unused `transformer_ref` declaration is removed
from runtime manifests. Model weights retain the **MiniMax H3 Community License**.

`WEIGHTS_VERIFIED.json` alone never enables H3. A host-owned
`h3-provision-enabled.json` containing the exact `revision` opts into one
queued operator acceptance run after download completion. It must complete
real-weight training, verify finite and genuinely changed exported tensors,
strictly reload the student, and generate both teacher/student videos with
audio. Only this evidence creates `h3-acceptance.json`. Failed or interrupted
attempts require operator review; they do not automatically retry or publish.
Tiny random-network diagnostics are explicitly excluded from readiness.

After local acceptance, also test the public MCP path and authenticated Range
downloads before updating the Marketplace version to advertise H3 as ready.

The job mounts the host CUDA, PTX JIT and NVML libraries read-only. CUDA renumbers
the permitted device nodes as 0–3 for Wan and 0–15 for H3. Static torchrun rendezvous uses 127.0.0.1
inside the networkless container. `train.py` releases the temporary text encoder's
Python reference cycles and CUDA cache before optimizer/NCCL allocations, records
finite local losses, and runs the native upstream training entry point.
External experiment trackers are explicitly disabled (`trackers: [none]`).

`export.py` restores only the student DCP state and uses upstream export mapping
and strict reload verification. It avoids initializing teacher/critic optimizers
on a single export GPU. The exported directory includes the unchanged tokenizer,
text encoder and VAE; `weights-manifest.json` identifies the changed transformer
file separately, so clients can reuse their existing pinned base components.

`models/wan2.1-1.3b/READY.json` must only be created after all pinned files pass
integrity checks. `runtime-ready.json` must only be created after runtime
acceptance passes. Without both markers, training requests fail explicitly.
Do not publish a usable Marketplace listing before native end-to-end acceptance.

## Interpreting results

A completed training job does not guarantee a useful distilled model. Short
runs are smoke tests. The report distinguishes training completion from quality
validation; paired single-prompt, cold-run timing excludes model loading.
Pixel MSE/PSNR measures agreement with the teacher, not perceptual quality.
No FVD, VBench score or general speedup is implied. Review paired videos and
evaluate a representative held-out dataset before deploying a student.

Restarted active jobs are marked interrupted. Workspaces persist on the host;
closing one revokes MCP access but retains files for operator recovery.
Back up code, image, pinned base-model manifest and service configuration.
