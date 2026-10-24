# AccelForge MCP

Model tensor-algebra accelerators remotely with native **AccelForge** and its
HWComponents backend. Explore architecture/workload mappings and inspect energy,
latency, area and component-level statistics through a standard MCP client.
No model API key is required.

## What you can do

- Browse and read bundled upstream architecture, workload and mapping YAML examples.
- Upload your own YAML architecture, workload and mapping specifications.
- **Map** a workload to an architecture using the Fast and Fusiest Mapper.
- **Evaluate** a mapping you supply, **calculate component costs**, or **validate**
  specification structure and expressions.
- Run jobs in the background, inspect their logs, cancel jobs, and download results.

The image includes HWComponents, CACTI, NeuroSim, ADC and component-library
packages, along with the scientific Python stack and Graphviz. Installation does
not guarantee that every upstream example or every component parameter combination
is supported. Errors from infeasible mappings or unsupported models appear in logs.

## First run

1. Call `create_accelforge_workspace` and retain the returned `workspace_id` and
   `workspace_key` for later calls.
2. Call `start_analysis` with those fields. The default maps a single 32×32 matrix
   multiplication onto the upstream `simple.yaml` architecture.
3. Call `job_status` with the returned `job_id`. Jobs return immediately; status
   includes actual log output and, after success, a compact results summary.
4. Use `list_artifacts` and `read_artifact` to download the generated files.
   Concatenate base64 pages until `eof`, then verify the full SHA256 checksum.
5. Call `close_accelforge_workspace` after saving the results.

Use `accelforge_tool_help` for every tool's complete description and input schema.

## Custom specifications

`list_examples` lists official YAML paths. Choose files with the `example:` prefix,
for example `example:arches/eyeriss.yaml`. `read_example` returns the source text.
For your own YAML, call `upload_spec` with a simple filename, consecutive byte
`offset` and base64 data (up to 1024 decoded bytes per call). Files are limited to
1 MiB each, 32 files per workspace. Identical retries are safe. Select uploaded
files with `file:filename.yaml`.

`start_analysis` accepts `architecture`, `workload`, optional `mapping`, `mode`,
`objective` (`energy`, `latency`, `energy_latency`) and scalar Jinja `parameters`.
Default parameters are `N_EINSUMS=1`, `M=32`, `KN=32`; supplied values override them.
An `evaluate` job requires a mapping. For the upstream unfused matrix-multiplication
mapping, use `example:workloads/basic/matmuls.yaml` with
`example:mappings/unfused_matmuls_to_simple.yaml`.

Native YAML tags, Jinja and expressions are processed inside the isolated worker.
Custom Python component plugins, arbitrary host files and external URLs are not
exposed by this interface. Complex searches may exceed the configured limits.

## Results and interpretation

- `summary.json`: objective, mapping count, selected mapping metrics, component
  breakdowns and area. With `energy_latency`, the mapper retains its Pareto results
  and the summary selects the lowest-energy result; minima need not be one mapping.
- `results.csv` / `results.json`: per-mapping statistics; opaque mapping objects
  are excluded and the selected mapping is exported separately.
- `mapping.yaml` / `mapping.svg`: selected mapping specification and diagram.
- `component-costs.json`: modeled component attributes and costs.
- `validated-spec.json`: parsed/evaluated configuration for validation jobs.
- `run.log`, input snapshots and `config.json`: reproducibility and diagnosis.

The upstream API uses energy in joules, latency in seconds and area in square
meters when the inputs use the corresponding physical units. **The default simple
example uses synthetic unit costs and zero area**; its results are not measured
hardware figures. Outputs are analytical estimates determined by input models,
not silicon measurements or synthesis results. Null metrics represent undefined
or non-finite values, not measured zero. Downloaded SVG files are artifacts; this
release does not expose a live interactive viewer.

## Workspaces and limits

Workspaces are bound to a server-verified caller plus a private capability. The
same Nexus API key represents the same caller. Each worker has no network, host
mounts or publisher credentials, runs as non-root with a read-only image, and has
2 CPUs, 8 GiB RAM and 512 MiB temporary storage. Maximum four workspaces total,
two per caller, one active job per workspace and 16 retained jobs. Requests use
a fair caller queue. A job may run for 1–900 seconds (300 seconds by default).

Files expire after 30 minutes idle, explicit closure or service restart. Save
important results before expiry. Execution state and logs are available through
`job_status`; this release does not push a separate background console page.

## Upstream

[Accelergy-Project/accelforge](https://github.com/Accelergy-Project/accelforge), MIT.
Pinned source commit: `7312a7d915625552bf6eb1db577941f8317b6ac7`.
Follow the upstream citation guidance for research use. This hosted Nexus
integration is not an official AccelForge service.
