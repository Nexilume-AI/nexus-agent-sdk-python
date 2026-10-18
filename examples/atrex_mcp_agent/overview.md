# Atrex Kernel Agent MCP

Run bounded GPU kernel optimization jobs through MCP using the official
[Alibaba Atrex Kernel Agent](https://github.com/alibaba/atrex-kernel-agent).
This is a community Nexus integration, not an official Alibaba-hosted service.

## Workflow

Create a private project, upload native Atrex-Bench operator inputs,
validate them, and authorize your model provider on the secure Nexus Cloud page.
Start optimization with your model name and budget limits. The call returns a job
ID immediately; query real optimizer progress, cancel work, and download generated
source, reports and logs through paged MCP artifact tools with SHA256 verification.

## Credentials and isolation

You supply your own model authorization. Keys are entered only on an authenticated
Cloud page, encrypted at rest, and never returned in MCP results. Jobs use a scoped
model proxy, recheck expiry/revocation, and run in separate containers without
network access, host credentials or Docker control. Your model provider charges
model usage; the Marketplace listing does not include model credits.

Choose Nexus Cloud, OpenAI, Anthropic, or a custom compatible HTTPS endpoint,
an exact model and an authorization lifetime. Reuse an eligible saved credential
or enter a new key on the secure page. Changing endpoint/model requires explicit
approval; revoking a saved credential also disables grants derived from it.
Custom OpenAI-compatible services must support Responses; custom Anthropic
compatible services must support Messages. Private network endpoints are rejected.

Nexus Cloud users can select an accessible Router and model directly in a supported
MCP client's confirmation UI, with no API key or endpoint to enter. A secure page
provides the same selection for other clients. Temporary access is restricted to
the chosen Router/model and lifetime; model usage is billed to that Router context.
Use `force_new` to choose new credentials without changing running jobs.

## Hardware and limits

Check `get_atrex_status` before starting. This host supports CUDA on Pascal GPUs
(compute capability 6.0). Modern Triton and Nsight hardware-counter profiling are
not available. SOL-ExecBench inputs support static preparation only because the
current official evaluator requires a newer GPU software stack. The official optimizer runs correctness and timing evaluation;
read its reports to determine whether a candidate actually improved performance.

One active job at a time, maximum 55 minutes and 20 iterations, with a separate
80-model-request ceiling. The optimizer token budget is a soft stop between stages;
a stage can exceed it, so it is not a hard spending cap. Uploads use 1024-character chunks;
inputs are limited to 2 MiB/project. Artifact downloads use 24 KiB base64 pages.
Keep your project access key private. Jobs require the same authorized caller.

Interrupted services do not silently resume model spending. Outputs remain
available after failure/cancellation when successfully collected. Download files
from the manifest instead of rerunning a job to retrieve them.

Optimizer revision: `7247206287ef2747342eccb89421fa4606368e2d` (Apache-2.0).

## Live task progress

New optimization jobs return a private live page URL. Sign in as the caller to
follow execution stages, elapsed time, model request counts and streamed logs,
cancel a job, and download verified artifacts when uploaded. The page uses SSE
push with replay after disconnection; it does not repeatedly query job status.

MCP clients can use `watch_atrex_job` with a progress token for streamed events.
Resume with the last event sequence after the bounded watch ends. Clients without
stream notifications can open the live page. Closing a page or watch does not
cancel the task. Fifteen MCP tools are registered with complete input schemas.
Performance gains remain subject to the official correctness/timing reports.
