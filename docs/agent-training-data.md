# Agent training data

Data Assets → select a collection → **Agent training** provides trajectory
collection, sample review, corrections and immutable SFT exports. This workflow
is independent of Marketplace publication. Creating a training version does not
publish it.

Preparation writes require a private collection. Unpublish an existing public
collection before adding/reviewing training samples or generating a new export;
this prevents an existing public listing from exposing a new training version
as a side effect of preparation.

## Collect

- Import one complete client trajectory as JSON, or use the Python SDK below.
- Select up to 20 existing Agent runs to redact and collect in one UI action.
  Each run has an independent result, so successful imports survive retries.
- Failed terminal runs can be redacted and archived. Running runs remain blocked.
- A tool server's display events do not necessarily contain the user's prompt,
  every model message, or the tool schema. AG-UI reconstruction only uses recorded
  messages and tool events; missing context is flagged and cannot be approved.
- External client submissions are labeled `external_client`, not presented as
  verified Nexus runs. Their outcome starts as `unknown`, including completed runs.

The canonical `nexus.trajectory.v1` payload contains:

```json
{
  "schema": "nexus.trajectory.v1",
  "context": {
    "task_id": "task-42",
    "session_id": "conversation-7",
    "client": "my-agent",
    "model": "my-model-version",
    "agent_version": "v2"
  },
  "execution_status": "completed",
  "tools": [{"type":"function","function":{"name":"add","description":"Add numbers","parameters":{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]}}}],
  "messages": [
    {"role":"user","content":"What is 2 + 3?"},
    {"role":"assistant","content":"","tool_calls":[{"id":"call_1","type":"function","function":{"name":"add","arguments":"{\"a\":2,\"b\":3}"}}]},
    {"role":"tool","tool_call_id":"call_1","content":"5"},
    {"role":"assistant","content":"2 + 3 = 5.","weight":1}
  ]
}
```

Use one `task_id` for attempts at the same task and one `session_id` for a
conversation. These associations determine split grouping, so do not generate
unrelated IDs for retries. A source with neither ID cannot be approved.
Record tool definitions as they were exposed at execution time. Do not reconstruct
missing prompts or private model reasoning from tool logs.

v1 accepts text messages, up to 1 MiB / 1000 messages / 256 tool definitions per
trajectory. Binary inputs and outputs remain separate source artifacts; there is
no automatic multimodal conversion. Truncation is not performed silently.

## SDK integration

```python
import os
from nexus_agent.training import TrainingClient, TrainingTrajectory

client = TrainingClient(
    "https://cloud.nexilume.com",
    token=os.environ["NEXUS_TRAINING_TOKEN"],
    tenant_id=os.environ["NEXUS_TENANT_ID"],
    dataset_id=os.environ["NEXUS_TRAINING_DATASET_ID"],
)
episode = TrainingTrajectory(
    task_id="task-42", session_id="conversation-7", client="my-agent",
    model="my-model-version", agent_version="v2", tools=tool_definitions,
)
episode.message("user", user_prompt)
# Record the actual model/tool interaction at the client, in order.
episode.tool_call(call_id, tool_name, arguments)
episode.tool_result(call_id, actual_result)
episode.message("assistant", final_answer)
sample = client.submit(
    episode, request_key="task-42-attempt-1", rights_confirmed=True,
    execution_status="completed",  # also failed / cancelled
)
```

Use a Cloud credential with collection-management access, not the Router's
registration secret. `rights_confirmed` must reflect permission to retain and
train on the content. The SDK does not capture or upload client sessions by
default; Codex/TCagent require explicit client-side integration or a JSON export.
Retries reuse the same request key and content. Different content under that key
is rejected. Redirects are refused, and non-local uploads require verified HTTPS.

## Review

Review ordered messages and matching tool calls/results. Mark the task outcome
(`unknown`, `success`, `failure`) independently of execution status. Record a
0–1 score, feedback/failure reason, and feedback source. The server records the
reviewer and timestamp. Evaluator/user-feedback labels are reviewer declarations,
not an automatic verified benchmark verdict.

Select assistant steps to train. Unselected steps remain as context with
`weight: 0`; selected steps use `weight: 1`. Structural warnings block approval.
Choose **Edit as a new corrected sample** to repair a trajectory. This creates a
new immutable source file linked by source ID and SHA-256; the original remains.
Corrected samples must be reviewed separately and stay grouped with their source.
Concurrent review saves use revision checking to avoid overwriting someone else.

## Export

Exports include approved successful samples by default. Training on approved
failure trajectories requires an explicit option. Unknown outcomes and pending,
excluded, incomplete samples are skipped with reasons.

- `train.jsonl`, `validation.jsonl`, `test.jsonl`: one complete conversation per
  line, using `messages`, optional `tools`, and assistant `weight` fields.
- `manifest.jsonl`: source IDs/hashes, annotation revisions, labels, split
  assignments, settings and skipped reasons. It is not a training sample.
- Empty splits do not produce a file. A split configuration with no training
  rows is rejected. Small collections can use 0% validation/test initially.

Exact content duplicates are removed. Connected task/session/content groups are
assigned together using deterministic SHA-256 buckets and a user-specified seed.
Fractions are targets, not exact counts. The immutable manifest is the authority;
adding new connecting trajectories can change a future version's grouping.

Exports currently support up to 500 source trajectories and 32 MiB of SFT data
per collection/export. Larger asynchronous/sharded training exports, automatic
quality scoring, semantic deduplication, preference-pair generation and training
job execution are not included in v1. JSONL is a portable source format; check the
chosen training framework's tool-call and loss-mask adapter before ingestion.

## APIs and storage

All routes require existing Cloud authentication, tenant/project scope and
collection-management permission:

- `GET/POST /api/v1/datasets/{id}/training/samples/`
- `GET/PATCH /api/v1/datasets/{id}/training/samples/{sample_id}/`
- `POST /api/v1/datasets/{id}/training/import-runs/`
- `GET/POST /api/v1/datasets/{id}/training/exports/`

Samples use immutable DatasetFile storage; annotations are bounded metadata with
revision checks. Storage/import/export quotas, private download authorization,
SHA-256 checks, object storage and immutable DatasetVersionEntry downloads are
reused. No database schema migration is required. Sample payloads are sanitized
server-side and checked against the versioned training schema. The structured
scanner preserves protocol IDs and numerical JSON arguments; it does not apply
phone-number heuristics to system-generated timestamps and hashes. These are rule-based
checks, not a guarantee of removing all sensitive content; review remains needed.
Training manifests preserve the exact review revision and never change when a
later review edits a sample. The source collection can still contain ordinary
assets; a training version includes only its generated training files and recipe.
