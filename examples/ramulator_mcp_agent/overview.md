# Ramulator MCP

Run cycle-accurate DRAM simulations remotely, directly from your MCP client. This hosted integration builds the native engine from **CMU-SAFARI Ramulator 2.1** and exposes typed tools through Nexus Cloud.

## What you can do

- Discover installed DRAM organization and timing presets.
- Compare memory configurations using DDR3/4/5, GDDR6/7, LPDDR5/6 and HBM1/2/3/4.
- Upload memory-access traces and configure channel counts and numeric DRAM overrides.
- Start background simulations, inspect job status and logs, and cancel running jobs.
- Download JSON/YAML statistics, the resolved configuration, input trace and execution log, with SHA256 verification.

## First simulation

1. Call `create_ramulator_workspace` and retain its `workspace_id` and `workspace_key`.
2. Call `start_simulation` with those fields. Defaults run the included `sample.trace` against DDR4.
3. Call `job_status` with the returned `job_id` until it completes.
4. Call `list_artifacts`, then `read_artifact` to retrieve results. Concatenate decoded base64 pages until `eof` is true.
5. Close the workspace after downloading needed artifacts.

For your own workload, upload a trace with `upload_trace`. Each line is `LD 0xADDRESS` for a read or `ST DECIMAL_ADDRESS` for a write. Upload chunks contain at most 1024 decoded bytes; traces may be up to 16 MiB. Use `list_dram_presets` for exact matching organization and timing names. `ramulator_tool_help` returns complete tool documentation.

## Privacy and limits

Each workspace runs in its own non-root, networkless container with a read-only image, 1 CPU, 2 GiB RAM and 512 MiB temporary storage. Workspaces are bound to the server-verified caller; the same API key represents the same caller. There are at most two workspaces per caller and four total, with fair request scheduling. No model credentials are required.

One simulation runs at a time per workspace, with a configurable timeout up to 600 seconds and up to 16 retained jobs. Workspace files expire after 30 minutes of inactivity, closure or service restart. Download results before expiry. Status reports execution state and logs; it does not invent a completion percentage.

This release provides trace-driven simulation with FRFCFS scheduling and an open-row policy. It does not expose arbitrary shell/Python execution, gem5 integration, the upstream browser visualizer, or arbitrary controller plugins. Invalid configurations return explicit job failures and logs.

## Upstream

[CMU-SAFARI/ramulator2](https://github.com/CMU-SAFARI/ramulator2), MIT license. Pinned source: `c6c7a5de0cef35935d6fad7369e6271406798865`. This is a hosted Nexus integration, not an official CMU-SAFARI service.

The integration includes a small LoadStoreTrace completion fix: simulation stops after all request callbacks complete, rather than immediately after the final request is enqueued. The frontend reports `completed_requests` alongside native controller statistics. Write completion follows upstream's callback semantics at command issue.
