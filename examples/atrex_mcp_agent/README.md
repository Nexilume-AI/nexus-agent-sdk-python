# Atrex Kernel Agent MCP — preparation deployment

Community Nexus integration of [Alibaba Atrex Kernel Agent](https://github.com/alibaba/atrex-kernel-agent).
Pinned upstream revision: `7247206287ef2747342eccb89421fa4606368e2d` (Apache-2.0).

This release intentionally has **no model backend or GPU runner**. The operator
requested MCP deployment first. It does not run optimizations, execute uploaded
code, report fabricated progress, or produce measured GPU results.

## Available MCP tools

| Tool | Purpose |
| --- | --- |
| `get_atrex_status` | Actual deployment readiness and limits |
| `authorize_atrex_provider` | Caller-owned provider authorization on a secure Cloud page; no secret tool arguments |
| `list_atrex_docs`, `read_atrex_doc` | Pinned official documentation, line pagination |
| `create_atrex_project` | Persistent private project; returns ID and private access key |
| `get_atrex_project` | Draft status and file manifest with SHA256 |
| `upload_atrex_file` | UTF-8 chunks (1024 characters), byte offsets, safe identical-chunk retries |
| `read_atrex_file` | Binary-safe base64 pages with next offset and full SHA256 |
| `validate_atrex_project` | Static SOL-ExecBench/Atrex-Bench filenames and JSON checks |
| `prepare_atrex_optimization` | Bounded launch plan and missing prerequisites |
| `start_atrex_optimization` | Explicit `MODEL_BACKEND_NOT_CONFIGURED` error; starts nothing |

Every tool registers its complete description, argument constraints and example.
Cloud exposes MCP Streamable HTTP with JSON and SSE responses via OpenWrt Relay.

## Deployment

Create a deployment directory alongside an SDK checkout in `../sdk/src`.
Copy `agent.py`, `service.py`, and `compose.yaml` there. Clone the official upstream
into `upstream/` and checkout the pinned revision. Preserve its LICENSE and NOTICE.
Create `data/projects/`, owned by the configured ATREX_UID/ATREX_GID (default 1000/1000), mode 0700. The existing Nexus SDK
is mounted read-only from `../sdk/src`. Run `docker compose up -d`.

No host ports, Docker socket, GPUs, model credentials or unrelated host files are
mounted. Container is non-root, read-only except project data/tmp, and resource limited.
Set NEXUS_AGENT_NETWORK to the Router network. The default Agent origin is `agent://default/atrex-mcp`; set NEXUS_TENANT and NEXUS_AGENT_ID in the container environment to customize it.

Projects use random 256-bit bearer access keys; only their hashes are stored on disk.
Keep keys private in the caller session. Anyone given a key can access that project.
There is no global project listing. Limits: 64 projects, 64 files/2 MiB per project,
256 KiB per UTF-8 file, 24 KiB per download page. Uploaded files persist until operator
cleanup; no automatic eviction. Backup `data/` privately, never to a public repository.

Upload from `offset=0`, at most 1024 characters per call, then use the returned
`next_offset` (UTF-8 bytes) for the next chunk. Verify the final SHA256. Explicit
`overwrite=true` at offset zero replaces an existing file. Keep each request small:
the deployed Router has a 16 KiB invocation body limit, including the envelope.

## Configure execution later

Adding a credential alone does not enable this version's execution gate. A subsequent
release must wire an authenticated model backend, dedicated isolated GPU runner,
budgeted asynchronous jobs, cancellation, real progress, and artifact exports; test
the entire execution path before claiming optimization readiness. Existing host GPU
availability is not equivalent to a validated container execution environment.

### Caller-owned credentials (available in v0.2.0)

Call `authorize_atrex_provider` with `{"provider":"anthropic","wait_seconds":15}`.
Supported providers are `openai`, `anthropic`, and `nexus`, each with a fixed endpoint.
Use the caller's personal Agent API key from Cloud or a signed-in user session.
Shared or unowned API keys cannot establish credential ownership.

- Clients advertising `elicitation.url` receive an `elicitation/create` URL request.
  Other clients receive `status: pending` and `authorization_url` in the tool result.
- Open that URL, sign in as the invoking user, enter the provider API key/token,
  choose its lifetime and explicitly approve. This is a secure key-entry page;
  provider OAuth and automatic reuse of a client's own login are not implemented.
- A waiting call resumes after approval; otherwise call the tool again. Valid grants
  are reused for the same caller, tenant, project, Agent and provider. Changing caller
  credentials or scope requires separate authorization. Project access keys are not
  proof of provider credential ownership.
- Pending requests expire after 10 minutes. Approved grants last 5 minutes to
  24 hours (default 1 hour). Reopen the authorization page to revoke a grant.
- Secrets are encrypted in Cloud and excluded from MCP arguments/results, Relay
  events and training traces. URLs contain only an opaque authorization reference.
  Decline/revocation clears the stored ciphertext; expired grants cannot be resolved.
- Only authenticated runtime callbacks for an active, matching Run can resolve a
  secret. The SDK's `resolve_for_runtime` is internal execution plumbing, not an MCP
  tool. This release does not yet consume the grant to run model/GPU workloads.

Codex or another client can initiate the request and reuse prior consent. First-use
consent remains a human action. URL prompt presentation depends on the client's
capabilities; the secure link fallback works without URL-mode UI support.

Protocol reference: https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation

## Verify

With the SDK `src` on `PYTHONPATH`, run `python -m unittest discover -s . -p test_service.py`.
Then initialize the published Cloud MCP endpoint, list all eleven tools, create a project,
upload/download and verify SHA256, validate JSON, and verify the disabled-execution error.
For authorization, test URL negotiation and link fallback, same-caller reuse,
cross-caller rejection, expiry and revocation with disposable synthetic credentials.
