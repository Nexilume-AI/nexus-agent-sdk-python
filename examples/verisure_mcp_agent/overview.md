# Generate RTL with evidence you can inspect

Veri-Sure connects its native Architect, Coder, Verifier and Debugger through
caller-authorized models. Supply a specification or select one of 209 bundled
VerilogEval-v2-EXT problems, then retrieve generated RTL, design contracts,
testbenches, waveforms and verification reports.

## Workflows

- **Generate:** upload `spec.txt`; optionally add `golden_tb.sv` and
  `golden_ref.sv`. Run the original multi-agent loop with bounded repair rounds.
- **Benchmark:** browse `list_benchmarks`, then run one bundled problem against
  its golden testbench. The adapter independently reruns the generated RTL
  against the supplied golden testbench and reference.
- **Simulate:** run uploaded `rtl.sv` and `tb.sv` with Verilator. Inspect mismatch
  counts, VCD output and native trace/slicing reports.
- **Formal:** use `contract.json`, `rtl.sv` and `spec_body.sv` with the native
  Boolean Proofer, SymbiYosys, Yosys and Z3. Keep PASS, FAIL, skipped and error
  results distinct; failing proofs may include counterexamples.
- **Contract lint / Trace:** inspect contract errors or diagnose uploaded
  `simulation.log` and optional `wave.vcd` using native temporal analysis and
  tree-sitter dependency slicing.

## Connect and run

Choose **Connect Agent**, call `create_verisure_workspace`, and save the returned
workspace ID/key. Start with `load_demo_project`, or upload UTF-8 files using
`put_project_file` in chunks of at most 1 KiB. Use a UUID `request_id` when calling
`start_verisure_job`; retries reuse the same job. Read `job_status`, list artifacts
and obtain protected downloads. `cancel_job` stops queued/running work.

Generation and benchmark jobs require `authorize_verisure_provider`. Confirm
your own Nexus Router or OpenAI-compatible model; do not put API keys into tool
arguments. Publisher credentials are never used. Model jobs include a Background
page with pushed progress. Download links support one-time redemption, SHA256,
HTTP Range and revocation.

## Limits and interpretation

Two private workspaces per caller; the same API key represents one caller.
Jobs use a FIFO CPU queue, one queued/running job per caller, a 15-minute deadline
and at most 32 model requests. Repair rounds are bounded. Inputs: 1 MiB/file,
8 MiB/workspace, 128 files. Exported artifacts: 16 MiB; oversized files are listed
in `export-manifest.json`. Workspaces expire after 24 idle hours.

All native/generated code executes in non-root, networkless resource-limited
containers. Model credentials remain outside the container.

A completed job is not automatically a passing design. Inspect `passed`, golden
simulation and formal statuses. Generated specifications, assertions and
reference models can share mistakes. Formal results apply to the selected
outputs and reference only; simulation covers the supplied tests. No full-chip
compliance or silicon sign-off is claimed. The bundled 209 problems are available
for individual jobs; listing them does not imply they have all been validated.

[Upstream Veri-Sure](https://github.com/xyjoey/Veri-Sure), pinned at
`aab5bed60f04`, MIT license. This Marketplace listing is free; your model usage
and Nexus account limits still apply.

## Release validation

Public MCP acceptance used a caller-authorized gpt-6-sol model for RTL generation
and the `Prob024_hadd` benchmark, including an independent golden simulation.
Separate correct/incorrect adder cases verified simulation mismatch detection
and formal PASS/FAIL. These are integration checks, not a benchmark-wide score
or a guarantee for arbitrary designs.
