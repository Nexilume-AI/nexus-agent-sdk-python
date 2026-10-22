# Wave MCP

Analyze RTL waveforms and SystemVerilog structure remotely through MCP. This community-hosted integration runs Tencent's open-source [wave-mcp](https://github.com/Tencent/wave-mcp) on isolated workers and connects it to Nexus Cloud through OpenWrt Relay.

## What you can do

- Prepare FST or VCD waveforms together with a SystemVerilog file list. VCD is converted to FST automatically.
- Explore hierarchy, modules and signals; query values and activity over a time window.
- Inspect drivers, connectivity, fan-in and downstream signals; trace values and unknown states.
- Compare waveforms, sample at clock edges, and analyze transactions and state transitions.
- Analyze RTL without running a simulator using `open_static_session`.
- Upload your own files, list workspace artifacts and download results with SHA256 integrity checks.

## Start in three steps

1. Call `create_wave_workspace` with `sample: true` to try the included counter example, or use an empty workspace for your own design. Save its `workspace_id` and private `workspace_key`.
2. Call `prepare_session` with those workspace credentials, `wave_path: "/work/sample/dump.vcd"`, `filelist_path: "/work/sample/rtl.f"`, and `top: "top_tb"`. Save the returned `session_id`.
3. Call `signal_values` with the workspace credentials, session ID and `paths: ["top_tb.u_counter.count"]`. Use `signal_drivers` for the same signal to inspect the RTL source.

All 45 published tools have typed input schemas. `wave_tool_help` returns the complete original instructions for each upstream tool. File parameters refer to paths inside `/work`, not your local computer. Upload binary or text files as base64 in chunks of at most 1,024 decoded bytes; download them with `read_wave_file`. Large query results return a paging reference usable with `read_wave_result`.

## Open a waveform in your browser

Call `open_wave_view` with the workspace credentials, `fst_paths: ["/work/sample/dump.vcd"]`, and `signals: [{"path": "top_tb.u_counter.count"}]`. Open the complete returned URL. The single-use link expires in ten minutes; the browser view lasts up to one hour. `update_wave_view` pushes new signals, cursor, viewport and markers to the page through SSE. `get_view_state` returns browser readiness and a fresh link. Download the FST directly from the viewer; `close_wave_view` revokes access.

Only these uploaded waveform snapshots are accessible through the link; it grants no publisher account permissions. Keep the link private. Two waveforms open side by side.

## Workspace lifetime and limits

Each workspace runs in its own non-root container, with no network, host mounts or Docker socket. Workspaces have a hard 256 MiB temporary filesystem limit, a 768 MiB memory limit and one CPU. Up to four workspaces run concurrently, with two per caller. Calls are queued fairly by caller: two active overall and one per caller, with a ten-second queue deadline. An operation exceeding its separate 45-second execution deadline closes its worker. Back off before retrying a queue error; narrow large queries or split designs accordingly.

**Files and sessions are temporary.** A workspace expires after 30 minutes without calls, when explicitly closed, or when the service restarts. Download required artifacts before it expires. Access requires both the workspace key and its Cloud-verified owner. The same Nexus API Key is one caller across clients and MCP sessions, sharing workspaces and quotas. Use separate Keys for independent callers; client-provided user labels cannot change this boundary.

## Availability

FST, VCD conversion, static RTL analysis and remote file transfer are installed and tested. Verible CLI tools, build tools, Surfer viewer assets and the fstdumper plugin are installed in the worker image. The current RTL engine uses pyslang. The five viewer tools return authenticated public browser links; no SSH tunnel is needed. The fstdumper plugin requires a separately installed Xcelium simulator for simulation. FSDB conversion requires a compatible Verdi FsdbReader runtime, which is not installed on this deployment. The server analyzes existing waveforms; it does not run your simulator or an autonomous model.

Free to connect through Nexus Marketplace; normal Nexus account quotas still apply. No upstream model API key is required. Source: Tencent/wave-mcp 1.1.0, pinned to `0b8c5fff11c9b4bf1e668787322ec41d08dd9d91`, Apache-2.0. This is a community integration, not an official Tencent hosted service.
