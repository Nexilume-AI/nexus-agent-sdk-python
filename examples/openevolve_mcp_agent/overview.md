# OpenEvolve Optimization MCP

Evolve Python programs using a caller-authorized language model and your own
evaluation function. Run native OpenEvolve remotely, follow a live background
console, and download the best program, metrics, traces and checkpoints.

## Workflow

1. Inspect `get_openevolve_status`, then create a workspace.
2. Upload `initial.py` and `evaluator.py` with `put_project_file`, or load the demo.
3. Your evaluator implements `evaluate(program_path)` and returns finite numeric
   metrics including `combined_score`. Higher scores are preferred.
4. Call `authorize_openevolve_provider`. Choose your Nexus Router/model, approve
   an OpenAI-compatible endpoint on the secure page, or explicitly preauthorize
   it in your MCP client configuration. Never paste keys into tool arguments.
5. Call `start_evolution` with the approved authorization ID, a fresh request UUID,
   objective and limits. Reuse the same request UUID when retrying submission.
6. Open `view_url` for pushed progress or inspect `job_status`; cancel if needed.
7. List artifacts, download with authenticated links, then close your workspace.

The background console requires the model credential owner's personal Nexus
login. An MCP API key alone does not grant access to that private page.

## Execution and limits

- Native OpenEvolve revision `04a37f191b2eb367daef455057a0470c0ac853d8`.
- Python standard library and NumPy. CPU only; custom packages, GPUs, arbitrary
  shell operations and upstream CLI provider sessions are not exposed.
- Two source files, 64 KiB each; 1–100 iterations; maximum 55 minutes per job.
- At most iterations + 2 model requests, requesting up to 4,096 completion
  tokens per call. Model usage is billed by your selected provider. This is
  not a monetary spending cap.
- A trusted controller uses a scoped broker. Generated candidates and uploaded
  evaluators run in separate networkless, nonroot containers with read-only
  inputs, 2 GiB RAM, 2 CPUs, 128 PIDs and a 1–60 second evaluation timeout.
  Provider secrets and the controller socket are never mounted into evaluators.
- One active job globally, FIFO; one queued/active job per verified caller.
  The same Nexus API key represents the same caller. Workspace access requires
  both verified caller identity and its private capability.
- Two workspaces per caller, eight overall; 24-hour idle expiry. Closing deletes
  files and revokes downloads. Interrupted jobs require explicit resubmission.

## Artifacts and interpretation

`best_program.py`, `report.json`, `evaluations.jsonl`, evolution traces,
checkpoints and logs are available. Download links are single-use for redemption,
expire after ten minutes, and establish one-hour sessions with HTTP Range,
ETag and SHA256. Renew through MCP to resume after expiry.

Evaluation scores come from your evaluator. They do not independently certify
correctness, safety, speedup or general quality. Short demo acceptance checks
the optimization pipeline; review and test evolved code before adoption.

OpenEvolve is Apache-2.0 licensed. This independent hosted MCP integration is
free; your model provider may charge for inference.
