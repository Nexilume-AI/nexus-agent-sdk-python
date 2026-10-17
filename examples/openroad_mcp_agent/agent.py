"""Expose the official OpenROAD MCP server through the Nexus OpenWrt SDK."""

import copy
import contextvars
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import threading
import time
import uuid
import hashlib
from urllib.parse import urlsplit, urlunsplit

from nexus_agent import McpToolDescriptor, McpToolResult, NexusAgent, SseEvent, current_run


# Keep a conservative default for older Routers; configure after upgrading the
# whole Relay path. Count the complete SDK JSON envelope, not just log text.
RESPONSE_BUDGET = int(os.environ.get("OPENROAD_RESPONSE_BUDGET", 12 * 1024))
if not 4096 <= RESPONSE_BUDGET <= 960 * 1024:
    raise ValueError("OPENROAD_RESPONSE_BUDGET must be 4096..983040 bytes")
RESULT_READER = {
    "name": "read_openroad_result",
    "description": "Read a saved large result without rerunning its command. Use result_id from the truncated response. offset and next_offset are UTF-8 byte offsets; limit is a maximum byte count. kind=log returns command output, kind=result returns the complete original MCP result JSON, including images. Continue with next_offset until eof. Results expire after 24 hours and may be evicted earlier by the 128 MiB/128-result quota.",
    "inputSchema": {"type": "object", "properties": {
        "result_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "kind": {"type": "string", "enum": ["log", "result"], "default": "log"},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "limit": {"type": "integer", "minimum": 16, "maximum": 2048, "default": 2048},
    }, "required": ["result_id"], "additionalProperties": False,
    "examples": [{"result_id": "<result_id>", "kind": "log", "offset": 0, "limit": 2048}]},
}

ARTIFACT_EXPORT = {
    'name': 'export_eda_artifact',
    'description': 'Upload a finalized EDA artifact directly to the caller\'s private Cloud Run file storage. Pass job_id and artifact_id from list_eda_artifacts. Returns a file reference and authenticated download path; file bytes bypass Relay. Cloud Run access is required. Use read_eda_artifact for MCP-only downloads.',
    'inputSchema': {'type':'object','properties':{
        'job_id':{'type':'string','pattern':'^[0-9a-f]{32}$'},
        'artifact_id':{'type':'string','pattern':'^[0-9a-f]{24}$'}},
        'required':['job_id','artifact_id'],'additionalProperties':False,
        'examples':[{'job_id':'0'*32,'artifact_id':'0'*24}]},
}

def export_artifact(arguments, root='/work/eda'):
    jid,aid=arguments.get('job_id',''),arguments.get('artifact_id','')
    if not re.fullmatch('[0-9a-f]{32}',jid) or not re.fullmatch('[0-9a-f]{24}',aid):
        return object_result({'error':'INVALID_ARTIFACT_ID'},True)
    context=current_run()
    if not context.run_id or not context.display_asset_url:
        return object_result({'error':'CLOUD_RUN_REQUIRED','message':'Use read_eda_artifact for a download over MCP.'},True)
    directory=Path(root)/'jobs'/jid
    try:
        if not json.loads((directory/'state.json').read_text()).get('artifacts_ready'):raise ValueError('Artifacts not ready')
        manifest=json.loads((directory/'artifacts.json').read_text())
        entry=next((item for item in manifest if item['artifact_id']==aid),None)
        if not entry:raise ValueError('Artifact not found')
        path=directory/entry['path']
        if any(p.is_symlink() for p in [path,*path.parents]) or not path.resolve().is_relative_to(directory.resolve()):raise ValueError('Invalid artifact path')
        h=hashlib.sha256()
        with path.open('rb') as file:
            for chunk in iter(lambda:file.read(1024*1024),b''):h.update(chunk)
        if path.stat().st_size!=entry['size'] or h.hexdigest()!=entry['sha256']:raise ValueError('Artifact integrity check failed')
        reference=context.output.upload_file(path,content_type='application/zip' if path.suffix=='.zip' else 'application/octet-stream',timeout=180)
        endpoint=urlsplit(context.display_asset_url)
        download_path=f'/api/v1/agent-runs/{context.run_id}/files/{reference["file_id"]}/download/'
        return object_result({'job_id':jid,'artifact_id':aid,'run_id':context.run_id,'file':reference,
                              'download_path':download_path,'download_url':urlunsplit((endpoint.scheme,endpoint.netloc,download_path,'','')),
                              'authentication_required':True})
    except Exception as exc:
        return object_result({'error':'ARTIFACT_EXPORT_FAILED','message':str(exc)[:300],
                              'fallback':'The original artifact remains available through read_eda_artifact.'},True)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def result_bytes(result):
    return len(json_bytes(result.to_wire()))


def object_result(value, is_error=False):
    return McpToolResult(content=[{"type": "text", "text": json_bytes(value).decode("utf-8")}],
                         structured_content=value, is_error=is_error)


class ResultStore:
    """Bounded, private, persistent result storage; IDs never resolve user paths."""
    def __init__(self, root, max_bytes=128 * 1024 * 1024, max_items=128, ttl=86400):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.max_bytes, self.max_items, self.ttl = max_bytes, max_items, ttl
        self.lock = threading.Lock()

    def _prune(self, reserve=0):
        files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        count = len(files)
        for p in files:
            st = p.stat()
            if time.time() - st.st_mtime >= self.ttl or total + reserve > self.max_bytes or count >= self.max_items:
                p.unlink()
                total -= st.st_size
                count -= 1

    def bound(self, result):
        if result_bytes(result) <= RESPONSE_BUDGET:
            return result
        original = result.to_dict()
        is_error = original.get("isError", False)
        raw = json_bytes(original)
        data = original.get("structuredContent") or {}
        output = data.get("output")
        log = output if isinstance(output, str) else raw.decode("utf-8")
        result_id = uuid.uuid4().hex
        try:
            if len(raw) > min(self.max_bytes, 16 * 1024 * 1024):
                raise ValueError("Single result exceeds the 16 MiB storage limit")
            with self.lock:
                self._prune(len(raw))
                target = self.root / (result_id + ".json")
                # Exclusive creation avoids overwriting retained results.
                with target.open("xb") as file:
                    file.write(raw)
        except (OSError, ValueError) as exc:
            return object_result({"error": "RESULT_STORAGE_FAILED", "command_executed": True,
                                  "message": str(exc)[:300], "original_result_bytes": len(raw),
                                  "retry_guidance": "Do not blindly rerun the command; inspect the session first."}, True)
        summary = {"truncated": True, "result_id": result_id, "original_result_bytes": len(raw),
                   "log_bytes": len(log.encode("utf-8")), "original_is_error": is_error,
                   "message": "Full result saved. Call read_openroad_result with result_id and next_offset; do not rerun the command to read its output.",
                   "output_tail": log[-2048:]}
        for key in ("session_id", "execution_time", "command_count", "error"):
            if key in data:
                value = data[key]
                summary[key] = value[:256] if isinstance(value, str) else value if value is None or type(value) in (int, float, bool) else "See saved result"
        while result_bytes(object_result(summary, is_error)) > RESPONSE_BUDGET:
            if summary["output_tail"]:
                summary["output_tail"] = summary["output_tail"][len(summary["output_tail"]) // 2 + 1:]
            else:
                # Arbitrarily large error/session strings belong in the saved result.
                for key in ("session_id", "error"):
                    if isinstance(summary.get(key), str): summary[key] = summary[key][:128]
        return object_result(summary, is_error)

    def read(self, arguments):
        rid = arguments.get("result_id", "")
        kind = arguments.get("kind", "log")
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 2048)
        if (not isinstance(rid, str) or not re.fullmatch(r"[0-9a-f]{32}", rid)
                or kind not in ("log", "result") or type(offset) is not int or offset < 0
                or type(limit) is not int or not 16 <= limit <= 2048):
            return object_result({"error": "INVALID_ARGUMENTS", "message": "Use a result_id, nonnegative byte offset and limit 16..2048."}, True)
        try:
            with self.lock:
                target = self.root / (rid + ".json")
                if time.time() - target.stat().st_mtime >= self.ttl:
                    target.unlink()
                    raise FileNotFoundError()
                raw = target.read_bytes()
            if kind == "log":
                original = json.loads(raw)
                output = (original.get("structuredContent") or {}).get("output")
                if isinstance(output, str): raw = output.encode("utf-8")
            if offset > len(raw) or (offset < len(raw) and raw[offset] & 0xc0 == 0x80):
                return object_result({"error": "INVALID_OFFSET", "message": "Use next_offset from the previous page."}, True)
            while True:
                text = raw[offset:offset + limit].decode("utf-8", errors="ignore")
                next_offset = offset + len(text.encode("utf-8"))
                page = object_result({"result_id": rid, "kind": kind, "offset": offset,
                                      "next_offset": next_offset, "total_bytes": len(raw),
                                      "eof": next_offset == len(raw), "text": text})
                if result_bytes(page) <= RESPONSE_BUDGET: return page
                limit //= 2
        except FileNotFoundError:
            return object_result({"error": "RESULT_NOT_FOUND", "message": "Result expired, was evicted or does not exist."}, True)
        except OSError:
            return object_result({"error": "RESULT_READ_FAILED"}, True)


# Deployment guidance supplements the upstream catalog; it never filters it.
TOOL_USAGE = {
    "interactive_openroad_query": (
        "Query an existing session with command and session_id; omit session_id to create OpenROAD. "
        "Use read-only Tcl such as help, info commands, report_* or get_*. For Yosys passes use "
        "interactive_openroad_exec in a yosys -C session. Check error and truncated in the result.",
        {"command": "help report_wns", "session_id": "<session_id>"},
    ),
    "interactive_openroad_exec": (
        "Execute Tcl that may change a design or write files. Reuse session_id. In a Yosys Tcl "
        "session invoke passes with the yosys command, e.g. yosys help synth. Files use container "
        "paths; /work is writable. Check error and truncated. Large results return result_id; "
        "use read_openroad_result to page the saved log without rerunning commands.",
        {"command": "yosys help synth", "session_id": "<yosys_session_id>"},
    ),
    "create_interactive_session": (
        "Create OpenROAD with {} or Yosys Tcl with command=[\"yosys\",\"-Q\",\"-C\"]. "
        "command is argv, not a shell string. Save session_id for query/exec and terminate when "
        "finished. The executable allowlist does not make non-Tcl programs compatible with the "
        "MCP Tcl startup probe. cwd defaults to /work; env is a string-to-string map.",
        {"command": ["yosys", "-Q", "-C"], "cwd": "/work"},
    ),
    "list_interactive_sessions": ("Call with {} to list sessions, their IDs and liveness.", {}),
    "inspect_interactive_session": ("Pass an existing session_id to inspect state and resource metrics.", {"session_id": "<session_id>"}),
    "terminate_interactive_session": ("Pass session_id to stop its process; force=true requests forced termination. Session state is lost.", {"session_id": "<session_id>", "force": False}),
    "get_session_history": ("Pass session_id; limit bounds history and search filters command text.", {"session_id": "<session_id>", "limit": 10}),
    "get_session_metrics": ("Call with {} for metrics across all sessions; no session_id is required.", {}),
    "grep_session_output": ("Search retained output by session_id and pattern; this does not rerun commands.", {"session_id": "<session_id>", "pattern": "warning|error", "ignore_case": True}),
    "list_report_images": ("Use platform, design and run_slug (usually the ORFS variant, e.g. base). Call this first to obtain image_name for read_report_image.", {"platform": "nangate45", "design": "gcd", "run_slug": "base"}),
    "read_report_image": ("Use an image_name from list_report_images. Returns native MCP image content and text metadata; max_size_kb controls the base64 budget.", {"platform": "nangate45", "design": "gcd", "run_slug": "base", "image_name": "<image_name>"}),
    "read_orfs_metrics": ("Read existing ORFS results by design, platform and variant. stage defaults to all. Run the appropriate flow stage first if results are absent.", {"design": "gcd", "platform": "nangate45", "variant": "base", "stage": "all"}),
    "run_orfs_stage": ("Start a background ORFS job; save job_id and poll get_orfs_job. Use dry_run=true to preview. Omit wait_seconds for Relay calls; clean_* targets delete stage outputs. overrides values must be strings.", {"design": "gcd", "platform": "nangate45", "stage": "synth", "dry_run": True}),
    "get_orfs_job": ("Pass job_id from run_orfs_stage to poll progress and final results; {} lists jobs. recent_lines limits the log tail.", {"job_id": "<job_id>", "recent_lines": 20}),
    "cancel_orfs_job": ("Pass the job_id of a running ORFS job to stop it and its subprocesses.", {"job_id": "<job_id>"}),
}


def discover_tools(mcp):
    tools = {}
    cursor = None
    cursors = set()
    while True:
        page = mcp.request("tools/list", {"cursor": cursor} if cursor else {})
        for tool in page.get("tools", []):
            name = tool.get("name")
            if not name or name in tools:
                raise ValueError(f"Invalid or duplicate upstream MCP tool: {name!r}")
            tools[name] = tool
        cursor = page.get("nextCursor")
        if not cursor:
            break
        if cursor in cursors:
            raise ValueError("Upstream MCP tools/list repeated a pagination cursor")
        cursors.add(cursor)
    if not tools:
        raise ValueError("Upstream MCP published no tools")
    return tools


def describe_tool(upstream):
    name = upstream["name"]
    schema = copy.deepcopy(upstream["inputSchema"])
    original = upstream.get("description") or name
    usage, example = TOOL_USAGE.get(name, (original, None))
    # Router v1 bounds the top-level description to 511 bytes. Keep the full
    # upstream text in the standard schema description, never truncate it or
    # strip parameter constraints to fit. Oversized metadata fails explicitly.
    schema["description"] = "\n\n".join(filter(None, (schema.get("description"), original, usage if usage != original else "")))
    if example is not None:
        schema.setdefault("examples", []).append(copy.deepcopy(example))
    description = usage if name in TOOL_USAGE else original
    if len(description.encode("utf-8")) > 511:
        description = f"Call {name}. See inputSchema.description for complete upstream instructions and inputSchema for arguments."
    return McpToolDescriptor(
        name=name,
        title=upstream.get("title") or name.replace("_", " ").title(),
        description=description,
        input_schema=schema,
    )


def native_result(result):
    content = result.get("content", [])
    structured = result.get("structuredContent")
    if structured is None and len(content) == 1 and content[0].get("type") == "text":
        try:
            parsed = json.loads(content[0]["text"])
            if isinstance(parsed, dict):
                structured = parsed
        except (ValueError, KeyError):
            pass
    return McpToolResult(
        content=content, structured_content=structured,
        is_error=bool(result.get("isError") or (structured or {}).get("error")),
        meta=result.get("_meta"),
    )


def stream_call(handler, payload):
    """Keep transport live without executing a command more than once.

    OpenROAD stdio returns its log at completion; progress here reports elapsed
    waiting time, not a fabricated completion percentage.
    """
    yield SseEvent(event="progress", data=json.dumps({"progress": 0, "message": "Invocation accepted"}))
    outcome = queue.Queue(maxsize=1)
    context = contextvars.copy_context()
    def execute():
        try:
            outcome.put((True, context.run(handler, payload)))
        except Exception as exc:
            outcome.put((False, str(exc)))
    threading.Thread(target=execute, daemon=True).start()
    started = time.monotonic()
    while True:
        try:
            ok, value = outcome.get(timeout=5)
        except queue.Empty:
            yield SseEvent(event="progress", data=json.dumps({"progress": round(time.monotonic() - started, 1), "message": "Command is still running (elapsed seconds)"}))
            continue
        if ok:
            yield SseEvent(event="result", data=json_bytes({"result": value.to_wire()}).decode("utf-8"))
        else:
            yield SseEvent(event="error", data=json.dumps({"code": "OPENROAD_CALL_FAILED", "message": value[:500]}))
        return


def streaming_payload(tool_name, payload):
    # Cloud SSE framing has a smaller effective event budget than a JSON file
    # response. Return a shorter page, preserving next_offset/EOF semantics.
    if tool_name == 'read_eda_artifact':
        payload = dict(payload)
        limit = payload.get('limit', 262144)
        if type(limit) is int:
            payload['limit'] = min(limit, 49152)
    return payload


class OpenRoadMcp:
    def __init__(self, on_disconnect):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        for attempt in range(30):
            try:
                self.socket.connect("/ipc/mcp.sock")
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if attempt == 29:
                    raise
                time.sleep(1)
        self.reader = self.socket.makefile("r", encoding="utf-8")
        self.writer = self.socket.makefile("w", encoding="utf-8", buffering=1)
        self.next_id = 0
        self.pending = {}
        self.lock = threading.Lock()
        self.closing = False
        self.on_disconnect = on_disconnect
        threading.Thread(target=self._read, daemon=True).start()
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "nexus-openroad-agent", "version": "1.0.0"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self):
        for line in self.reader:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            message_id = message.get("id")
            with self.lock:
                waiting = self.pending.get(message_id)
            if waiting is not None:
                waiting.put(message)
        with self.lock:
            waiting_queues = list(self.pending.values())
        for waiting in waiting_queues:
            waiting.put({"error": {"message": "OpenROAD MCP exited"}})
        if not self.closing:
            self.on_disconnect()

    def _send(self, message):
        self.writer.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.writer.flush()

    def request(self, method, params=None, timeout=120):
        waiting = queue.Queue(maxsize=1)
        with self.lock:
            self.next_id += 1
            message_id = self.next_id
            self.pending[message_id] = waiting
            self._send({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}})
        try:
            response = waiting.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"OpenROAD MCP {method} timed out") from exc
        finally:
            with self.lock:
                self.pending.pop(message_id, None)
        if "error" in response:
            raise RuntimeError(str(response["error"]))
        return response.get("result", {})

    def call_tool(self, name, arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments}, timeout=360)
        return native_result(result)

    def close(self):
        self.closing = True
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.reader.close()
        self.writer.close()
        self.socket.close()


def main():
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    mcp = OpenRoadMcp(stop.set)
    try:
        results = ResultStore(os.environ.get("OPENROAD_RESULT_DIR", "/work/nexus-results"))
        available = discover_tools(mcp)
        if RESULT_READER["name"] in available:
            raise ValueError("Upstream tool collides with local result reader")
        available[RESULT_READER["name"]] = RESULT_READER
        if ARTIFACT_EXPORT['name'] in available:
            raise ValueError('Upstream tool collides with local artifact exporter')
        available[ARTIFACT_EXPORT['name']] = ARTIFACT_EXPORT
        descriptors = {name: describe_tool(tool) for name, tool in available.items()}
        agent = NexusAgent(
            runtime="openwrt",
            router=os.environ.get("NEXUS_ROUTER_URL", "http://router:7446"),
            tenant="eda",
            agent_id="openroad",
            advertise_address="auto",
            cloud_publish=True,
            cloud_name="OpenROAD MCP",
            lease_seconds=120,
        )
        agent.server.max_response_bytes = 1048576
        agent.server.max_stream_event_bytes = 1048576
        for name, descriptor in descriptors.items():

            def handler(payload, tool_name=name):
                if tool_name == RESULT_READER["name"]:
                    return results.read(payload)
                if tool_name == ARTIFACT_EXPORT['name']:
                    return export_artifact(payload)
                return results.bound(mcp.call_tool(tool_name, payload))

            agent.capability(f"openroad.{name}", public_ipv6=True, tool=descriptor)(handler)
            def streaming_handler(payload, call=handler, tool_name=name):
                yield from stream_call(call, streaming_payload(tool_name, payload))
            agent.stream_capability(f"openroad.{name}", public_ipv6=True, tool=descriptor)(streaming_handler)
        with agent.start() as handle:
            print(f"OpenROAD Agent ready: {agent.origin}; tools={len(descriptors)}; names={','.join(descriptors)}", flush=True)
            stop.wait()
    finally:
        mcp.close()


if __name__ == "__main__":
    main()
