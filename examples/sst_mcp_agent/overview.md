# SST Core MCP

Run discrete-event system simulations remotely with **SST Core 16.0.0** and a matching selection of **SST Elements 16.0.0**. Upload Python models, inspect component documentation, execute simulations in private workspaces and download their outputs through a standard MCP client. No model-provider API key is required.

## Included models

- **Event exchange:** two components exchange 50 events; the example finishes at 52 ns.
- **Memory hierarchy:** a synthetic CPU issues 1,000 memory operations through an L1 cache to a simple memory backend, producing CSV statistics.
- **Network:** a small four-endpoint Merlin torus exercises packet traffic.

Installed Elements libraries: `simpleElementExample`, `simpleSimulation`, `memHierarchy`, `miranda`, `merlin`, and `cassini`. `component_info` returns native parameter, port, subcomponent and statistic documentation. Examples are demonstrations; results depend on the supplied system model and its assumptions.

## Workflow

1. Call `create_sst_workspace`; retain `workspace_id` and `workspace_key` for subsequent calls.
2. Browse `list_examples` and `read_example`, or upload your own files with `upload_model`.
3. Call `start_simulation`, using `model: "example:memory.py"` or `"file:my_model.py"`. The default is `example:events.py`.
4. Use `job_status` to inspect live logs, elapsed wall time, terminal state and simulated time. `cancel_simulation` stops your job while preserving its partial outputs.
5. Use `list_artifacts` and `read_artifact` to download configuration, logs, summary and model-produced statistics. Join base64 pages until `eof` and verify the complete SHA256 checksum.
6. Close your workspace after downloading results.

`sst_tool_help` provides complete documentation and schemas for all 14 tools. `read_sst_result` handles unusually large tool responses.

## Custom models and limits

`upload_model` accepts a simple filename, sequential byte offset and base64 data (up to 1,024 decoded bytes per call). Files are limited to 1 MiB each and 32 files per workspace; identical retries are supported. Upload helper modules and data alongside the `.py` entrypoint. Each job gets a snapshot of the uploaded files.

Runs use one MPI rank and one or two threads. `stop_at` limits simulated duration (default `1ms`); `timeout_seconds` limits wall time (default 120, maximum 900 seconds). A clean exit at the simulated-time limit does not imply that the workload completed. `stopped_at_limit` indicates that reported simulated time reached the configured limit; inspect the logs to determine workload completion.

Each workspace has 2 CPU cores, 8 GiB RAM and 512 MiB temporary storage. One job can run per workspace, with 16 retained jobs, at most two workspaces per caller and four globally. Requests use bounded fair scheduling. The same API key represents the same caller; workspace capabilities also remain required.

Python executes inside a network-disabled, read-only, unprivileged container with no host directories or credentials mounted. Uploaded native plugins, distributed multi-host MPI, external simulator backends and the other optional Elements libraries are not provided. This listing does not include an interactive web console; logs and artifacts are available through MCP.

Workspaces expire after 30 minutes idle and are discarded on closure or service restart. Save important results locally.

## Upstream

[SST Core](https://github.com/sstsimulator/sst-core) and [SST Elements](https://github.com/sstsimulator/sst-elements) are BSD-3-Clause projects. This hosted MCP integration is an independent deployment and is not an upstream endorsement.
