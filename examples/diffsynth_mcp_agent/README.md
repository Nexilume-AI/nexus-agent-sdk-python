# DiffSynth LoRA MCP on Nexus OpenWrt

Pinned upstream: `d188f01d09013688dea27cc28f2e8dc0953c2543`.
Native Wan 2.1 T2V 1.3B supervised LoRA fine-tuning on physical GPU2 of host11.
Read [overview.md](overview.md) for API scope, limits and artifact semantics.

## Runtime

- Python SDK host agent → existing OpenWrt router → Cloud Relay/MCP.
- Dedicated Docker image `nexus-diffsynth-mcp:d188f01d0901`, based on the existing
  SM60-compatible Torch2.12/CUDA12.6 image. Upstream model code is pinned.
- Jobs: nonroot, network disabled, read-only root, capabilities dropped,
  no-new-privileges, one GPU,64GiB host memory,8CPU,512PID limit.
- Only operator-controlled Python and local, hash-verified model paths execute.
  Client MP4s decode inside the job container; clients cannot supply pickle,
  Python, YAML targets or arbitrary URLs.
- Dense Torch SDPA selected explicitly for SM60. No quantized or FlashAttention
  backend claims. Training uses upstream WanTrainingModule/FlowMatchSFTLoss/PEFT.

## Provision and verify

1. Clone the pinned upstream into `upstream/`; retain its LICENSE.
2. Run `python provision.py model-manifest.json models`. Download is resumable;
   every official ModelScope file must match its pinned whole-file SHA256.
3. Build Dockerfile; original Torch/CUDA packages are not replaced.
4. Run the operator `prepare_native.py` helper before installing the service.
   `check.py` exercises actual LoRA updates, fresh adapter reload, preview,
   identity/capability/path isolation, queue bounds and cancellation.
5. Install `nexus-diffsynth-agent.service`, with SDK path matching host setup.
6. Prepare the English Marketplace profile, run public MCP upload/train/download
   acceptance, then publish free only after it succeeds.

`runtime-ready.json` must only be installed after native acceptance. It is not
evidence that every setting has been benchmarked. The acceptance recipe uses
two steps at64×64,5frames,rank4; production quality needs a suitable dataset and
longer training/evaluation. Service restarts interrupt in-flight jobs.

## Preservation

Keep source, upstream revision, image identity, dependency lock, model SHA256
manifest, service configuration and native/public evidence together. Same-host
hardlinked weight snapshots protect against accidental path removal, not disk
failure. Restore image/model cache and source before starting the service.
Never put workspace keys, Cloud credentials or capability links in Git.
