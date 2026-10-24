# VBench Video Evaluation MCP

Evaluate your generated videos with native **VBench** metrics through MCP.
Upload MP4 files, submit a background evaluation, inspect progress and logs,
and download the JSON report, CSV scores and per-video upstream results.

## Available metrics

- **Subject consistency** — DINO feature consistency across frames.
- **Background consistency** — CLIP feature consistency across frames.
- **Temporal flickering** — frame-to-frame intensity changes; intended for static scenes.
- **Motion smoothness** — AMT frame-interpolation consistency.
- **Dynamic degree** — RAFT optical-flow motion measurement.
- **Aesthetic quality** — CLIP and the LAION aesthetic predictor.
- **Imaging quality** — MUSIQ technical image-quality assessment.

These are the upstream VBench custom-input visual metrics. This service does
not provide an official leaderboard score, VBench 2.0, audio assessment or
semantic/object-category evaluation. Metrics measure different properties;
higher motion is not universally better. A small test clip verifies the
pipeline and does not establish a generative model's quality.

## Workflow

1. Call `create_vbench_workspace` and retain its private workspace credentials.
2. Call `begin_video_upload` with a filename, byte size and SHA256. Open the
   authenticated upload page, or use `upload_video_chunk` from an MCP client.
3. Call `finalize_video_upload`, then `start_evaluation` with your video names
   and chosen dimensions. `create_demo_video` provides a synthetic test input.
4. Use `job_status` for actual progress and logs. `cancel_job` stops your job.
5. Use `list_artifacts` and `get_artifact_download` for reports and media, or
   `read_artifact` for programmatic base64 pages. Verify the returned SHA256.
6. Close the workspace after downloading. Closing deletes its files.

Download links have a one-time redemption token valid for ten minutes and a
one-hour download session, with HTTP Range support. Links can be revoked.
Upload links can be renewed with identical file metadata to resume.

## Runtime and limits

- Free access; no model-provider API key is required.
- One evaluation runs globally on a dedicated GPU. FIFO queue, with at most
  one active or queued job per authenticated caller.
- Up to 8 MP4 files per job/workspace, 128 MiB per file, 32–512 pixels per side,
  2–360 frames and at most 30 seconds per video. Jobs have a one-hour maximum.
- Two workspaces per caller, eight total; 24-hour idle expiry. Files survive a
  service restart, while unfinished jobs become interrupted.
- Workspaces are bound to server-verified identity plus a private workspace
  capability. The same API key represents the same caller.
- Evaluations execute in nonroot, networkless containers. Inputs are videos,
  not user-supplied Python, models or commands.

Source: [Vchitect/VBench](https://github.com/Vchitect/VBench), pinned to
`fd18b3d055cb0fc6f066ca90fe2c3c8cbb698490`. VBench uses Apache-2.0; evaluation
weights and included third-party components retain their respective licenses.
