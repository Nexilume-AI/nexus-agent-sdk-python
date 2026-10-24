# DiffSynth LoRA Training MCP

Fine-tune a video LoRA remotely using your own captioned clips, then download
the trained adapter, training report and generated preview through MCP.

## Installed recipe

**Wan 2.1 T2V 1.3B · native DiffSynth FlowMatch SFT · LoRA adapters**

This deployment exposes one tested recipe from DiffSynth-Studio. Other model
families, full-model training and arbitrary code execution are not exposed.
Base model weights remain on the server. No model-provider API key is needed.

## Workflow

1. Call `get_diffsynth_status` and `list_recipes`.
2. Create a workspace with `create_diffsynth_workspace` and retain its ID/key.
3. Reserve MP4 uploads with `begin_video_upload`, upload or resume using the
   authenticated link, then call `finalize_video_upload` to verify SHA256.
4. Call `start_lora_training` with `samples` containing `video` and `caption`.
   Choose training steps, learning rate, LoRA rank, dimensions and frame count.
5. Use `job_status` for stage, completed steps, loss and logs; `cancel_job` stops
   your job. Training runs in the background and returns a job ID immediately.
6. Use `list_artifacts` and `get_artifact_download` to retrieve results.
7. Close the workspace after downloading; this deletes its files and revokes links.

## Outputs and checks

- `lora.safetensors`: adapter weights, not a standalone base model.
- `report.json`: losses, changed tensor count, SHA256, configuration identity
  and exact adapter reload verification.
- `metrics.jsonl`: per-step losses and gradient norms.
- `preview.mp4`: generated using the reloaded trained adapter.
- Resolved request, progress and logs.

Successful jobs require finite losses/gradients, actual adapter updates, matching
adapter keys and tensor values after reload into a fresh model, a finite reloaded
forward pass and generated preview. Short tests demonstrate the pipeline;
they do not establish trained-model quality or generalization.

## Limits and isolation

- Up to 8 MP4 clips per workspace, 128 MiB each; clips must be at most 30 seconds
  and 32–1024 pixels per side. Training resizes and uniformly samples frames.
- Training resolution: 64–256 pixels per side, multiples of 16.
- Frames: 5, 9, 13 or 17. LoRA rank: 4, 8, 16 or 32.
- 1–1,000 optimization steps; learning rate 0.000001–0.001; maximum 6 hours/job.
- One GPU job at a time, FIFO; one queued/running job per verified caller.
- Same Nexus API key means the same caller. Private workspaces require both
  verified caller identity and the workspace capability; two per caller.
- Idle workspaces expire after 24 hours. Download before closing or expiry.

Download tickets are single-use and expire after ten minutes. Redeemed sessions
last up to one hour and support HTTP Range, ETag and SHA256; renew via MCP to
resume. Anonymous access is denied and links can be revoked.

DiffSynth-Studio is Apache-2.0 licensed. Users remain responsible for rights to
their training data and applicable base-model terms. This service is free.
