# ChampSim MCP

Run trace-driven CPU microarchitecture simulations remotely from your MCP client.
This hosted integration uses the native **ChampSim** engine and returns detailed
instruction, cycle, branch-prediction, cache, prefetch and DRAM statistics.

## Available configurations

| Preset | Branch predictor | L1D prefetcher | LLC replacement |
| --- | --- | --- | --- |
| baseline | Bimodal | None | LRU |
| next-line | Bimodal | Next-line | LRU |
| perceptron | Perceptron | None | LRU |

All presets use one simulated core. Other microarchitecture parameters use the
upstream defaults; `list_simulation_presets` returns the exact configuration input.
No model API key is required.

## First run

1. Call `create_champsim_workspace`; retain its `workspace_id` and `workspace_key`.
2. Call `start_simulation` with those fields and an optional `preset`, warmup and
   simulation instruction count. It immediately returns a background `job_id`.
3. Call `job_status` to read execution state, elapsed time and logs. Completed jobs
   include native JSON statistics. IPC is retired instructions divided by cycles
   in the simulation phase; warmup is excluded. Retirement can slightly exceed
   the requested instruction count because of superscalar execution.
4. Use `list_artifacts` and `read_artifact` to download JSON results, configuration,
   trace and logs. Concatenate base64 pages and verify the complete SHA256.
5. Call `close_champsim_workspace` after downloading your files.

The included `sample.champsimtrace` is a deterministic **synthetic 4096-instruction
trace**, intended for demonstrations and connectivity checks. It is not SPEC or
another representative application benchmark. ChampSim repeats a trace when
the requested instruction count exceeds its length.

## Bring your own trace

`upload_trace` accepts standard ChampSim **64-byte little-endian input_instr**
binary records, optionally compressed with xz, gzip or bzip2. Upload base64 chunks
of up to 1024 decoded bytes, using consecutive byte offsets. Identical retries
are safe. Limits: 64 MiB per uploaded file and 256 MiB after decompression.
CloudSuite-format traces and arbitrary URLs are not supported by this interface.
Use `champsim_tool_help` for full schemas and argument descriptions.

## Execution and privacy

Each caller-bound workspace runs in a non-root container with no network, host
mounts or host credentials, a read-only image, 1 CPU, 2 GiB RAM and 512 MiB temporary
storage. The server checks the verified API-key identity and workspace capability;
the same API key represents the same caller. Maximum two workspaces per caller,
four total, one running simulation per workspace and 16 retained jobs.

Jobs have a configurable wall-time limit of 1–600 seconds and can be cancelled.
Workspace data expires after 30 minutes idle, closure or service restart. Save
required artifacts before expiry. Logs are available while a job runs; no
estimated completion percentage is invented.

This release exposes the three precompiled configurations. It does not compile
user C++ modules, accept arbitrary shell commands, or run Intel Pin to trace
applications. A JSON null metric can indicate that the upstream engine had no
events for that denominator; it does not mean a measured zero.

## Upstream

[ChampSim/ChampSim](https://github.com/ChampSim/ChampSim), Apache-2.0 license.
Pinned commit: `410cee62d9c429c043ea611325ce00e6c303be2a`.
Please follow the upstream citation guidance for research use. This is a hosted
Nexus integration and is not an official ChampSim service.
