# Atrex Kernel Agent MCP — execution integration (v0.3)

Community Nexus integration of [Alibaba Atrex Kernel Agent](https://github.com/alibaba/atrex-kernel-agent).
Pinned optimizer: `7247206287ef2747342eccb89421fa4606368e2d` (Apache-2.0).
Pinned evaluator: `alibaba/atrex-bench@22d6a57b98ebb45c8db76e98815158d820c1dca3`.

## Workflow

1. `get_atrex_status`: verify the actual GPU preflight and supported frameworks.
2. `create_atrex_project`, then `upload_atrex_file`: upload native Atrex-Bench
   (`reference.py`, `input.py`, `shapes.json`, optional metadata/public contract)
   inputs. SOL-ExecBench uploads/static validation remain available, but its current
   CUDA 13 / PyTorch 2.9 evaluator cannot execute on the reference Pascal worker.
   Upload at most 1024 UTF-8 characters per call; continue at `next_offset` (bytes).
3. `validate_atrex_project` and `prepare_atrex_optimization`: static checks only.
4. `authorize_atrex_provider`: confirm model access in the client's authorization
   form when supported. Nexus Cloud offers Router/model selection without a key
   or endpoint. New third-party keys and clients without form support use the
   authenticated Cloud page. Never send provider keys through MCP arguments,
   uploaded files, or chat.
5. `start_atrex_optimization`: pass project ID/key, authorization ID, provider,
   exact model, a fresh UUID `request_id`, and bounded iteration/token/time settings.
   Reuse the request ID on retries. This starts billable model requests using your
   provider account and returns `job_id` immediately.
6. Open the returned `view_url` for live events, logs, cancellation and authenticated
   artifact downloads. For MCP progress notifications, use `watch_atrex_job` with
   a progress token; resume with the last event sequence. `get_atrex_job` remains
   available for snapshots and saved log pages. No invented percentages.
7. `cancel_atrex_job` requests cancellation; follow events until terminal.
8. `read_atrex_artifact`: read each manifest path in base64 pages, append using
   `next_offset` until `eof`, then verify the full SHA256. Failed/cancelled runs
   retain available outputs. `read_atrex_file` retrieves original uploads.

Fifteen tools register complete schemas and descriptions. JSON and SSE travel
through Cloud/OpenWrt Relay. A terminal success means the official optimizer
exited successfully; improvement and correctness must be read from its reports.

## Execution and authorization

`agent.py` has no Docker socket or GPU. It calls a private Unix control socket.
`broker.py` runs as the deployment user and creates only fixed-image containers
with validated options, immutable input snapshots, one assigned GPU, no network,
read-only root filesystem, no capabilities, non-root UID, bounded RAM/CPU/PIDs,
and bounded temporary storage. Containers have no host home or credentials.
Each job has a separate working filesystem. The upstream localhost gateway runs
only inside this job container; it is never exposed as a public execution API.

The broker holds the resolved provider key only in memory for the authorized
operation. Worker code receives a random job capability, never the real key.
A separate Unix model proxy restricts the provider endpoint and exact model and
checks grant expiry/revocation before every request; a five-second watchdog stops
jobs after revoked/expired grants or failed authorization checks. Model responses
are streamed. Redirects are refused. Only the submitting caller may inspect or
cancel funded jobs, in addition to the private project key.

The existing active Cloud Run resolves the grant when submitting. Its scoped
callback token can check the grant after submission (up to one hour); it cannot
resolve new secrets once that Run completes. Jobs are capped at 3300 seconds and
the grant lifetime. Restarted broker jobs become `interrupted`; credentials are
not persisted and work does not silently resume. Reauthorize and use a new request
ID to start a new job. Cancellation disables subsequent model requests and stops
the complete container, including optimizer descendants.

The upstream `token_budget` is a soft stop checked at stage boundaries. One CLI
stage can exceed it, including repeated cached input tokens. The broker separately
caps model calls at 80, output tokens per request at 16384, and wall-clock duration.
Token counting and billing remain provider-dependent; these are not a currency cap.
Nexus Cloud must support native Codex Responses items (`additional_tools`, client
tool namespaces/custom tools, and tool history) without dropping their semantics.

## Model authorization configuration

MCP connection authentication identifies the caller; it does not itself authorize
upstream model access or charges. This flow never forwards the MCP client's own
login tokens. Cloud must enable this Agent for caller-owned authorization.

For Nexus Cloud, choose an available Router and one of its model outputs in the
client's authorization form. No API key or endpoint entry is needed. The secure
page offers the same selection for clients without form support. Approval creates
a temporary credential restricted to that Router/model and the selected lifetime.
Router use permissions and original caller validity are checked at gateway admission.
Model usage is billed in the selected Router's Organization/project context.

Set `force_new: true` on `authorize_atrex_provider` to replace credentials or choose
another configuration without revoking existing jobs. Other providers can confirm
an existing authorization in-client; new keys still use the secure page. If a user
takes longer than the tool's wait window, check the returned `authorization_id`
again after approval. The model can then continue the task without requesting keys.

For example, call `authorize_atrex_provider` with:

```json
{"provider":"nexus","model":"gpt-6-sol","wait_seconds":15}
```

The model name must be available through an accessible Router. Omit it to let the
user choose. After approval, call the same tool with the returned
`authorization_id`; use the returned provider/model for execution. Opening the
URL alone is not approval. A third-party key must be entered on the secure page,
never in the in-client form or a chat message.

Pending requests expire after 10 minutes. The confirmation UI offers 5 minutes,
1 hour (default), or 24 hours of access. `wait_seconds` is only a 0–20 second tool
wait, not the authorization lifetime. Reuse valid grants within the same caller,
Agent, tenant, project, provider/endpoint and matching model scope. If the request
omits a model, check the actual returned model before using a reused grant.

To choose another Router, model, endpoint or key, make a fresh tool invocation with
`force_new: true` and explicitly approve the new configuration. Existing jobs keep
their original grants. To switch a job, cancel it and submit using the new grant
and a new `request_id`. The authorization page's Revoke access action prevents
further authorized model requests; already-issued requests cannot be undone.

`authorize_atrex_provider` accepts `nexus`, `openai`, `anthropic`, `custom_openai`,
or `custom_anthropic`, plus `base_url` and `model`. Custom endpoints must be public
HTTPS API base URLs. OpenAI-compatible services need Responses support; Anthropic
compatible services need Messages support. The secure page allows choosing these
fields, authorization lifetime, and a new key or an eligible saved credential.
After approval, call the tool with `authorization_id` to retrieve the approved
configuration, then pass that exact provider/model to `start_atrex_optimization`.
An optional `base_url` on start asserts the expected authorized destination.

Keys never enter chat or MCP arguments. Saved credentials are scoped to the same
caller/Agent/tenant/project/provider/endpoint. A new model requires explicit consent;
derived grants expire with their source and stop working when it is revoked.
Endpoint connections pin validated public DNS addresses, verify TLS and refuse
redirects. Existing grants retain their original endpoint until they expire.

## Reference worker hardware limits

GPU compute capability is 6.0 (Pascal). The reference worker exposes **CUDA only**,
using CUDA 12.4 / PyTorch 2.5.1, and the upstream fast episode path with correctness
and timing evaluation. Modern Triton and Nsight Compute hardware-counter profiling
are unavailable on this hardware. No driver security settings are relaxed and
no unsupported profiling results are fabricated. Model backend defaults are not
shared: the caller supplies the exact model, e.g. `gpt-6-sol`, and provider.

`bench_compat.py` applies one explicit patch to the pinned evaluator: Triton 3.1
does not implement `return_mode="all"`, so eager timing records its real CUDA-event
median as one sample. Correctness remains unchanged; no per-iteration distribution
is claimed. Both reference and candidate use the same timing implementation.

## Deployment

Build `Dockerfile.worker` with `upstream/`, its pinned ncu-report-skill submodule,
and `bench/` above. Include only those directories and `worker.py` in build context.
The service unit is a **user** systemd unit; adapt absolute paths/user IDs for your
host, enable linger, then enable `nexus-atrex-broker`. The broker user must already
have Docker permission. It exposes no TCP control port.

Run `python3 preflight.py` followed by `python3 smoke_runner.py` on the deployment
host. They verify real CUDA arithmetic, CUDA/C++ compilation, CLI binaries, official
reference evaluation, optimizer startup, model proxy routing, cancellation and
artifact collection. The smoke test uses a local rejecting model stub and spends
no provider credits. Only after both succeed is `broker-state/gpu-health.json`
written with measured GPU details, timestamp and tested image ID.
The agent remains unready if the image or successful preflight is missing.
Start `docker compose up -d` for the MCP agent after the broker socket exists.

Back up source, pinned dependency revisions, the worker image and configuration.
Back up private project/job data separately with restrictive permissions. Never
publish project access keys, model grants or runtime callback tokens.

## Limits and verification

One active job on assigned GPU 0; 128 retained jobs before operator cleanup.
Inputs: 64 projects, 64 files / 2 MiB per project, 256 KiB per file. Artifacts:
256 regular files / 64 MiB per job, 16 MiB per file; links and hidden paths excluded.
Logs: final 16 MiB. MCP download pages: 24 KiB. Artifacts are user-generated data;
inspect them before executing locally.

With SDK `src` on `PYTHONPATH`, run:

```sh
python -m unittest discover -s . -p 'test_*.py' -v
```

Coverage includes isolation, idempotent submission, grant/model scope, revocation,
input snapshots, artifact traversal/link rejection, capability redaction, and the
existing upload/download/static-validation tests. Production acceptance must also
cover GPU arithmetic/compiler execution, public MCP launch/poll/cancel/download,
and one caller-authorized model optimization; unit tests alone do not prove this.

## Background jobs and push events

New jobs register a caller-bound Cloud telemetry channel before execution. The
broker writes ordered events to a local journal and uploads new batches over an
outbound authenticated HTTPS callback. MCP calls still use the existing
Cloud/OpenWrt Relay; background telemetry does not require opening a host port or
keeping the original tool call alive. The delivery token is restricted to one
job, cannot invoke models or download files, and expires after two hours.

Cloud commits events before acknowledging them. Identical retries are accepted;
conflicting duplicates and sequence gaps are rejected. Redis wakes SSE subscribers
without periodic job queries. The browser replays persisted events and resumes
with Last-Event-ID after disconnection. Short stream rotations revalidate access.
Model authorization and container safety watchdogs remain separate checks.

`watch_atrex_job` streams for up to 45 seconds. Its `after` argument is the last
received sequence (initially 0). MCP clients must request SSE and supply
`_meta.progressToken` to receive `notifications/progress`. Notifications contain
actual event JSON in `message`; progress numbers are sequence numbers, not percent.
Reconnect using the last sequence; never resubmit optimization to reconnect.
JSON-only callers receive a snapshot and `view_url`. A page connection or watch
call closing does not cancel the job. Cancellation is explicit.

The flat, English page shows execution states, worker-reported reference and
optimizer stages, model request counts, streamed logs and terminal artifacts.
It requires the initiating credential owner's active Cloud account. Downloads
verify the uploaded file against the job manifest and use authenticated routes;
files are stored outside public media. Failed/cancelled jobs retain collected
outputs. Performance comparisons remain in the official result artifacts; a
successful process exit does not establish a speedup.

Deployment requires Cloud migration 0063, working Redis, `job_events.py` alongside
the broker and the updated SDK/Agent. The Cloud private artifact directory defaults
to `BASE_DIR.parent/private-agent-jobs` and can be set using the Django setting
`NEXUS_PRIVATE_JOB_ROOT`; back it up with the database. Keep local event-delivery
files private (mode 0600). Reserve disk for journals and artifacts and apply your
retention policy to completed jobs; no automatic deletion policy is installed.

Each job permits up to 12,000 events / 24 MiB and 256 artifacts / 64 MiB (16 MiB
per file). Log forwarding stops at its journal quota; the final optimizer.log
remains available within existing retention limits. Browser log rendering is
bounded. Already-running legacy jobs have no Cloud channel; only newly submitted
jobs gain a view URL. This implementation does not advertise the MCP Tasks
subscription extension; it uses an explicit watch tool for compatible clients.


The broker and worker example is configured for hosted Nexus Cloud: callback URLs are pinned in `broker.py` and `job_events.py`. Personal hosts expose the same background API but do not issue hosted Router delegation keys; use their secure page with a caller-owned key. Adapting the execution example to another host requires reviewing both callback allowlists. No installation or shared provider key is substituted.

The sample user service uses `%h/nexus-atrex-agent`; set Compose `ATREX_UID`/`ATREX_GID` to the broker user. The isolated worker runs as UID/GID 1000 with no host home or credentials.
