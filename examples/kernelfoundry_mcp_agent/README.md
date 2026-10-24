# KernelFoundry MCP deployment

Host: `/opt/nexus-agents/kernelfoundry-agent`.
Unit: `nexus-kernelfoundry-agent.service` (user service).
Image: `nexus-kernelfoundry-mcp:0b2338891b7b`.

The trusted native controller parses task Python with AST instead of importing
it for pytest collection. Its model and evaluation calls go through a private
Unix broker. The broker resolves caller-owned credentials in memory and checks
authorization before each provider request. Full evaluator execution takes
place in an offline GPU container, without the controller or broker mounts.
Only JSON metrics and reference results return to the controller, never compiled
artifacts or pickle. Caller-controlled config, paths and package installation
are not accepted by the service.

`check.py` runs real CUDA compilation and native validation without a model.
Real authorized autonomous optimization and public artifact tests are separate
acceptance gates; do not publish on installation or deterministic checks alone.
Private runtime directories are `data/` and `control/`; exclude them from Git
and deployment backups. Preserve sanitized accepted artifacts and image/dependency
identity separately. See `overview.md` for the supported client workflow.

The image reuses the compatible existing Atrex CUDA base without changing Atrex.
Its pip/setuptools must be upgraded for current upstream package metadata.
Python 3.10 compatibility changes remove redundant TemporaryDirectory delete
arguments, use typing_extensions.override and rewrite a newer f-string syntax.
Temporary directories use normal cleanup; the upstream debug keep-temp option
is not supported. Serialized metric parsing uses ast.literal_eval instead of
eval. The evaluator's isolated /tmp allows executable mappings required to load
compiled CUDA extensions; the root filesystem remains read-only.
