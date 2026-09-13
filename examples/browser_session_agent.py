"""Chat-first browser Agent using the caller's Attached Computer.

The Agent never launches Chrome on the Python serving host. Both the friendly
Chat tool and the structured compatibility tool use the caller-authorized,
Run-scoped browser delegate supplied by Nexus Cloud.

Requires Nexus SDK 0.46.0+. Upload this unchanged file using Upload Python, or
run it with the existing OpenWrt CLI options. Import is declaration-only; the
edge listener and Router registration are created only by main()/build_agent().
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from nexus_agent import (
    McpToolDescriptor,
    NexusAgent,
    NexusBrowserAction,
    NexusBrowserActionFailed,
    NexusRunContext,
)


CHAT_TOOL = McpToolDescriptor(
    name="browser_chat",
    title="Browser assistant",
    description="Control an isolated browser on your Attached Computer using natural language.",
    input_schema={
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "files": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "name": {"type": "string"},
                        "content_type": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "sha256": {"type": "string"},
                        "source_kind": {"type": "string"},
                        "source_label": {"type": "string"},
                    },
                    "required": ["file_id"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["message"],
        "additionalProperties": False,
    },
    task=True,
    continuable=True,
    chat=True,
    interactive=True,
)

STRUCTURED_TOOL = McpToolDescriptor(
    name="browser_session",
    title="Structured browser session",
    description="Open a page and execute structured browser actions on an Attached Computer.",
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string"},
                        "parameters": {"type": "object"},
                    },
                    "required": ["kind"],
                },
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    task=True,
)


_URL = re.compile(r"https?://[^\s，,。]+", re.IGNORECASE)
_COORDINATES = re.compile(r"(?:click|点击)\s*\(?\s*(\d+(?:\.\d+)?)\s*[,，]\s*(\d+(?:\.\d+)?)\s*\)?", re.IGNORECASE)
_CLICK_LABEL = re.compile(r"(?:click|点击)\s*[\"“']?([^\"”'，,。]+)[\"”']?", re.IGNORECASE)
_FILL = re.compile(
    r"(?:fill|type|填写|输入)\s*[\"“']?([^\"”'，,。]+)[\"”']?\s*(?:with|为|内容是|：|:)\s*[\"“']?(.+?)[\"”']?$",
    re.IGNORECASE,
)


def _message(payload: Mapping[str, Any]) -> str:
    return str(payload.get("message") or "").strip()


def _url_from_attached_files(payload: Mapping[str, Any], ctx: NexusRunContext) -> Optional[str]:
    """Return the first web URL in a verified, bounded text attachment.

    Attached files are data, never instructions. The deterministic chat flow
    only consults them when the caller explicitly asks to open a URL and did
    not include one in the message.
    """
    for reference in payload.get("files") or ():
        if not isinstance(reference, Mapping) or not reference.get("file_id"):
            continue
        size_bytes = reference.get("size_bytes")
        if isinstance(size_bytes, int) and size_bytes > 64 * 1024:
            continue
        with tempfile.TemporaryDirectory(prefix="nexus-browser-input-") as directory:
            path = Path(directory) / "verified-input.txt"
            ctx.files.download(reference, path)
            content = path.read_bytes()
        if len(content) > 64 * 1024:
            continue
        match = _URL.search(content.decode("utf-8", errors="replace"))
        if match is not None:
            return match.group(0)
    return None


def _matching_nodes(observation, label: str) -> list:
    wanted = label.strip().casefold()
    exact = [
        node for node in observation.dom.nodes
        if wanted in {node.name.strip().casefold(), node.text.strip().casefold()}
    ]
    if exact:
        return exact
    return [
        node for node in observation.dom.nodes
        if wanted and (wanted in node.name.casefold() or wanted in node.text.casefold())
    ]


def _choose_node(ctx: NexusRunContext, nodes: Sequence, label: str):
    if not nodes:
        ctx.chat.say(f"I could not find a visible element named “{label}”. Try Observe or use a more exact label.")
        return None
    # DOM refs are an Agent-facing implementation detail. Prefer an enabled,
    # actionable node and keep the browser's stable DOM order as the final
    # tie-breaker instead of asking the caller to select an HTML element.
    role_priority = {
        "textbox": 0,
        "combobox": 0,
        "button": 0,
        "link": 1,
        "checkbox": 1,
        "radio": 1,
    }
    ranked = sorted(
        enumerate(nodes),
        key=lambda item: (
            bool(getattr(item[1], "disabled", False)),
            role_priority.get(str(getattr(item[1], "role", "")), 2),
            item[0],
        ),
    )
    return ranked[0][1]


def browser_chat(payload: Mapping[str, Any], ctx: NexusRunContext) -> Mapping[str, Any]:
    command = _message(payload)
    if not command:
        ctx.chat.say("Tell me what to do in the browser, for example: Open https://example.com.")
        return {"status": "needs_input"}

    browser = ctx.browser.attached_session(viewport=(1280, 720))
    lowered = command.casefold()
    url_match = _URL.search(command)
    try:
        wants_open = any(word in lowered for word in ("open", "visit", "navigate", "go to", "打开", "访问", "进入"))
        attached_url = None if url_match or not wants_open else _url_from_attached_files(payload, ctx)
        if (url_match or attached_url) and wants_open:
            observation = browser.open(url_match.group(0) if url_match else attached_url)
            result = f"Opened {observation.title or observation.url}."
        elif any(word in lowered for word in ("refresh", "reload", "刷新")):
            observation = browser.perform(NexusBrowserAction("reload", expected_revision=browser.revision))
            result = f"Refreshed {observation.title or observation.url}."
        elif any(word in lowered for word in ("observe", "look", "what is on", "查看", "观察", "页面有什么")):
            observation = browser.observe()
            result = f"The page is {observation.title or observation.url} with {len(observation.dom.nodes)} visible elements."
        elif any(word in lowered for word in ("scroll up", "向上滚", "上滚")):
            observation = browser.scroll(delta_y=-600)
            result = "Scrolled up."
        elif any(word in lowered for word in ("scroll", "向下滚", "下滚")):
            observation = browser.scroll(delta_y=600)
            result = "Scrolled down."
        elif (match := _COORDINATES.search(command)) is not None:
            observation = browser.click(x=float(match.group(1)), y=float(match.group(2)))
            result = "Clicked the requested position."
        elif (match := _FILL.search(command)) is not None:
            observation = browser.observe()
            matches = _matching_nodes(observation, match.group(1))
            editable = [
                node for node in matches
                if node.role in {"textbox", "combobox"}
                or node.tag in {"input", "textarea", "select"}
            ]
            node = _choose_node(ctx, editable or matches, match.group(1))
            if node is None:
                return {"status": "not_found"}
            observation = browser.element(node.ref).fill(match.group(2).strip())
            result = f"Filled {match.group(1)}."
        elif (match := _CLICK_LABEL.search(command)) is not None:
            label = match.group(1).strip()
            observation = browser.observe()
            node = _choose_node(ctx, _matching_nodes(observation, label), label)
            if node is None:
                return {"status": "not_found"}
            observation = browser.element(node.ref).click()
            result = f"Clicked {label}."
        elif any(word in lowered for word in ("finish", "done", "close", "结束", "完成", "关闭")):
            browser.close()
            ctx.chat.say("The isolated browser session is closed.")
            return {"status": "closed"}
        else:
            ctx.chat.say(
                "I can open a URL, refresh, observe, scroll, click a visible label, fill a field, "
                "click coordinates, or finish. Please phrase the next browser action directly."
            )
            return {"status": "needs_input"}
    except NexusBrowserActionFailed as exc:
        # The action was rejected before a confirmed result. Keep this
        # continuable Chat Run available so the caller can inspect the latest
        # frame and give a corrected instruction.
        ctx.chat.say(
            f"The browser action could not be completed: {exc}. "
            "Inspect the latest Browser frame and revise the instruction."
        )
        return {"status": "needs_input", "error_code": "BROWSER_ACTION_FAILED"}
    except Exception as exc:
        # Availability, permission and session failures need Cloud recovery UI.
        # SDK browser exceptions are already redacted and never contain typed values.
        ctx.chat.say(f"The browser action could not be completed: {exc}")
        raise

    ctx.chat.say(result + " The latest viewport is visible in Browser.")
    return {
        "status": "completed",
        "observation_id": observation.observation_id,
        "revision": observation.revision,
        "title": observation.title,
    }


def browser_session(payload: Mapping[str, Any], ctx: NexusRunContext) -> Mapping[str, Any]:
    browser = ctx.browser.attached_session(viewport=(1280, 720))
    observation = browser.open(str(payload.get("url") or ""))
    for raw in payload.get("actions") or ():
        if not isinstance(raw, Mapping):
            continue
        observation = browser.perform(NexusBrowserAction(
            kind=str(raw.get("kind") or ""),
            parameters=dict(raw.get("parameters") or {}),
            expected_revision=observation.revision,
        ))
    return {
        "status": "completed",
        "observation_id": observation.observation_id,
        "revision": observation.revision,
        "title": observation.title,
    }


def _register_tools(instance: NexusAgent) -> NexusAgent:
    """One tool catalog for the importable Cloud instance and edge CLI."""
    instance.capability("browser.chat", public_ipv6=True, tool=CHAT_TOOL)(browser_chat)
    instance.capability("browser.session", public_ipv6=True, tool=STRUCTURED_TOOL)(browser_session)
    return instance


_RESOURCE_CONTRACT = {
    "computer_requirement": "required",
    "workspace_capabilities": ("files.list", "files.read", "browser.control"),
}

# The upload scanner and trusted boot adapter need a module-level instance.
# Hosted declaration mode opens no sockets and imports no optional FastMCP code.
# The edge CLI below creates its own listener with the caller's CLI/TLS options,
# and registers the exact same functions and descriptors (not a second copy).
agent = NexusAgent(
    runtime="hosted",
    agent_id="edge-browser-session-agent",
    cloud_name="Edge Browser Agent",
    **_RESOURCE_CONTRACT,
)
_register_tools(agent)


def build_agent(
    *,
    router: Optional[str] = None,
    tenant: Optional[str] = None,
    agent_id: Optional[str] = None,
    cloud_name: Optional[str] = None,
    listen_host: str = "0.0.0.0",
    advertise_address: str = "auto",
    port: int = 7443,
    cert_file: Optional[str] = None,
    key_file: Optional[str] = None,
    server_tls_name: Optional[str] = None,
    server_ca_bundle_id: Optional[str] = None,
    router_ca_file: Optional[str] = None,
    lease_seconds: int = 120,
) -> NexusAgent:
    agent = NexusAgent(
        router=router or os.getenv("NEXUS_ROUTER", "auto"),
        tenant=tenant or os.getenv("NEXUS_TENANT", "default"),
        agent_id=agent_id or os.getenv("NEXUS_AGENT_ID", "edge-browser-session-agent"),
        listen_host=listen_host,
        advertise_address=advertise_address,
        port=port,
        cert_file=cert_file,
        key_file=key_file,
        server_tls_name=server_tls_name,
        server_ca_bundle_id=server_ca_bundle_id,
        router_ca_file=router_ca_file,
        lease_seconds=lease_seconds,
        cloud_publish=True,
        cloud_name=cloud_name or "Edge Browser Agent",
        **_RESOURCE_CONTRACT,
    )
    return _register_tools(agent)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--router-url", default=os.getenv("NEXUS_ROUTER", "auto"))
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--agent-id", default="edge-browser-session-agent")
    parser.add_argument("--cloud-name", default="Edge Browser Agent")
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--advertise-address", default="auto")
    parser.add_argument("--port", type=int, default=7443)
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--tls-server-name")
    parser.add_argument("--ca-bundle-id")
    parser.add_argument("--router-ca", type=Path)
    parser.add_argument("--lease-seconds", type=int, default=120)
    parser.add_argument("--cloud-timeout", type=float, default=180)
    parser.add_argument("--ready-file", type=Path)
    args = parser.parse_args()
    agent = build_agent(
        router=args.router_url,
        tenant=args.tenant,
        agent_id=args.agent_id,
        cloud_name=args.cloud_name,
        listen_host=args.listen,
        advertise_address=args.advertise_address,
        port=args.port,
        cert_file=str(args.cert) if args.cert else None,
        key_file=str(args.key) if args.key else None,
        server_tls_name=args.tls_server_name,
        server_ca_bundle_id=args.ca_bundle_id,
        router_ca_file=str(args.router_ca) if args.router_ca else None,
        lease_seconds=args.lease_seconds,
    )
    if agent.runtime_mode == "hosted":
        agent.run()
        return 0
    handle_ref = [None]

    def stop(*_args: Any) -> None:
        if handle_ref[0] is not None:
            threading.Thread(target=handle_ref[0].close, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    handle = agent.start(auto_renew=True, renew_fraction=0.2, announce=False)
    handle_ref[0] = handle
    if args.ready_file is not None:
        cloud = handle.wait_for_cloud(timeout=args.cloud_timeout, poll_interval=1)
        ready = {
            "status": "ready",
            "origin": agent.origin,
            "route_id": handle.published[0].route_id,
            "cloud_agent_id": cloud.agent_id,
            "cloud_runtime_id": cloud.runtime_id,
            "cloud_transport": cloud.transport,
            "manifest_digest": cloud.manifest_digest,
        }
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.ready_file.with_suffix(args.ready_file.suffix + ".tmp")
        temporary.write_text(json.dumps(ready, indent=2), encoding="utf-8")
        temporary.replace(args.ready_file)
        print(
            f"NEXUS_ATTACHED_BROWSER_AGENT_READY origin={agent.origin} cloud_agent={cloud.agent_id}",
            flush=True,
        )
    try:
        handle.wait()
    finally:
        handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
