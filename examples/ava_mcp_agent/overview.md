# RISC-V verification with inspectable evidence

AVA connects RTL analysis, commit-log comparison and executable test generation
in caller-owned MCP workspaces. It runs the native
[AI Chip Design Platform](https://github.com/HUNT-001/ai-chip-design-platform)
and provides optional AI-assisted semantic analysis using your authorized model.

## Workflows

- **Pipeline:** native AVA semantic analysis, testbench generation, coverage and
  extended verification reports. Choose `model_pipeline` to use your own model.
- **Compare:** compare canonical RTL and ISS commit logs, including registers,
  stores and traps. Download JSON, Markdown and JUnit evidence.
- **Generate:** native RV32IM directed and random tests, RISC-V GCC assembly and
  Spike execution, with per-test results and downloadable assembly/ELF files.
- **RTL checks:** native RTL graph analysis, Verilator lint and Yosys structural
  checks/netlist output.
- **Simulation:** compile your RTL and `tb.sv` with Verilator and execute the
  supplied testbench under resource limits.

## Connect and run

Use **Connect Agent**, then `create_ava_workspace`. Save the workspace ID and key.
Call `load_demo_project` for a small demonstration, or upload your own files with
`put_project_file` in chunks of at most 1 KiB. Submit `start_verification_job`
with a UUID `request_id` and a workflow mode. Jobs return immediately; inspect
`job_status`, then list and download artifacts. Retries with the same request ID
return the same job. `cancel_job` stops queued or running work.

For `model_pipeline`, first call `authorize_ava_provider` and confirm your own
Nexus Router or OpenAI-compatible provider. Publisher credentials are never used.
Authorized model jobs include a Background page with pushed status updates.

## Isolation and downloads

Workspaces are bound to the verified Nexus caller. The same API key represents
the same caller. Native tools run in non-root, networkless containers; model
credentials stay outside them. Jobs share a FIFO CPU queue with one queued or
running job per caller. Downloads support authentication, one-time redemption,
HTTP Range, SHA256 and revocation.

Limits: two workspaces per caller, eight total; 24-hour idle expiry; 8 MiB input
per workspace, 1 MiB per file, 16 MiB exported artifacts and a 15-minute outer
job deadline. Individual tool steps have shorter deadlines. Export your data
before closing a workspace.

## Read the result, not just the job status

Successful execution is **not** full-chip verification or tape-out sign-off.
The upstream AVA pipeline generates harness artifacts but does not automatically
produce a runnable, instrumented RISC-V DUT from arbitrary RTL. Missing tandem
traces and coverage remain explicit partial/no-data results. The demo is a small
logic/testbench fixture, not a verified processor. Matching supplied logs,
structural checks and bounded simulations each establish only their stated scope.

No commercial simulator or physical implementation flow is included. LLM output
is advisory. Review native statuses, warnings, coverage and evidence before
drawing conclusions.

Pinned upstream: `d247b29c8399`, Apache-2.0. Free listing; caller model usage and
Nexus account limits still apply.
