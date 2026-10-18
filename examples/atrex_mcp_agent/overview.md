# Atrex Kernel Agent MCP

Prepare GPU kernel optimization projects through MCP, backed by a pinned checkout of
[Alibaba Atrex Kernel Agent](https://github.com/alibaba/atrex-kernel-agent).
This is a community Nexus integration, not an official Alibaba-hosted service.

## Current availability: preparation only

The MCP service is online. **A model backend and isolated GPU execution runner have
not been configured.** This release cannot optimize kernels, run benchmarks, stream
optimization progress, or return generated performance results. Calling
`start_atrex_optimization` returns `MODEL_BACKEND_NOT_CONFIGURED` and starts no work.

## What you can do now

- Read the official documentation from the deployed upstream revision.
- Create a private persistent project and upload operator definitions and source files.
- Check SOL-ExecBench or Atrex-Bench file layouts and JSON syntax without executing code.
- Inspect file manifests and download uploaded files with pagination and SHA256 checks.
- Prepare a bounded optimization plan and see the missing execution prerequisites.
- Authorize a supported model provider through a private Cloud page, with expiry and revocation.

## Typical MCP workflow

1. Call `get_atrex_status` to check readiness.
2. Call `create_atrex_project` with a project name. Keep its `project_id` and
   `access_key` private; use both for every subsequent project call.
3. Call `upload_atrex_file` for `reference.py`, `definition.json`, and
   `workload.jsonl` (SOL-ExecBench), or `reference.py`, `input.py`, and `shapes.json`
   (Atrex-Bench). Send at most 1024 characters per call, starting at `offset=0`,
   then continue with the returned `next_offset` in bytes. Retry identical chunks
   safely and compare the final SHA256 with your source. File-layout validation
   does not establish evaluator completeness.
4. Call `validate_atrex_project`, then `prepare_atrex_optimization` to inspect a plan.
5. Use `get_atrex_project` and `read_atrex_file` to retrieve your inputs. Decode and
   concatenate the base64 pages using `next_offset`, then verify the full SHA256.

## Limits and privacy

Files are never executed in this release. Each project is protected by a random bearer
access key; anyone who obtains that key can access the project. Do not share it publicly.
Projects persist until operator cleanup. Each project supports up to 64 files / 2 MiB,
with a 256 KiB file limit. Download pages are bounded to 24 KiB for Relay compatibility.

MCP supports Streamable HTTP through Nexus Cloud and OpenWrt Relay. Model generation
and GPU compute are disabled and therefore not included in this free preparation listing.
Execution capabilities and any future compute pricing will require a separate update.

## Secure provider authorization

Call `authorize_atrex_provider` with `provider` set to `openai`, `anthropic`, or
`nexus`. MCP clients supporting URL elicitation receive an authorization prompt;
other clients receive a secure link. Open it and sign in as the owner of the personal
Agent API key used by your MCP client. Enter the provider key on that page and
explicitly approve a lifetime of 5 minutes to 24 hours (default: 1 hour).

The client can initiate authorization automatically and reuse your approval until
expiry or revocation. First-use consent is yours; this flow does not access a client's
own login tokens. Cloud encrypts provider secrets and returns only authorization
status and an opaque reference to MCP. Grants are isolated by caller, tenant, project
and Agent. Reopen the same page to revoke access. Never put provider credentials in
chat, uploaded files or tool arguments. Shared/unowned API keys are not supported.

Authorization is available now, but model invocation and GPU execution remain
disabled pending the execution integration. Authorizing a provider alone does not
start an optimization job or incur model usage through this Agent.

Upstream revision: `7247206287ef2747342eccb89421fa4606368e2d`. Upstream license: Apache-2.0.
