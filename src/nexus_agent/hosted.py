"""Transport-neutral capability registry and optional hosted MCP adapter.

Importing this module never imports FastMCP, opens a socket or discovers a Router.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from typing import Any, Dict

from .errors import NexusAgentError
from .models import AgentEnvelope, AgentResponse, SseEvent
from .reporting import NexusRunContext, reset_current_run, set_current_run


def _failure(exc):
    """Only typed SDK causes and allowlisted codes; never arbitrary exception prose."""
    from . import browser
    from .auth import _certificate_failure
    codes = {
        "RUN_CONTEXT_TLS_FAILED", "RUN_CONTEXT_TRUST_UNAVAILABLE", "RUN_CONTEXT_ORIGIN_MISMATCH",
        "RUN_CONTEXT_EXCHANGE_FAILED", "RUN_DELEGATE_UNAVAILABLE", "HANDLER_FAILED",
        "COMPUTER_RUNTIME_OFFLINE", "COMPUTER_RUNTIME_REVOKED", "COMPUTER_CAPABILITY_UNAVAILABLE",
        "COMPUTER_PERMISSION_REQUIRED", "WORKSPACE_PERMISSION_REQUIRED", "WORKSPACE_UNAVAILABLE",
        "MOBILE_ACTION_FAILED", "MOBILE_ACTION_REJECTED", "MOBILE_ACTION_CANCELLED",
        "BROWSER_UNAVAILABLE", "BROWSER_SESSION_LOST", "BROWSER_ACTION_FAILED",
        "BROWSER_PERMISSION_REQUIRED", "BROWSER_COMPUTER_REQUIRED", "BROWSER_TUNNEL_UNAVAILABLE",
        "INVALID_TOOL_ARGUMENTS", "TOOL_EXECUTION_FAILED",
    }
    code = getattr(exc, "code", "")
    if _certificate_failure(exc):
        code = "RUN_CONTEXT_TLS_FAILED"
    elif not isinstance(code, str) or code not in codes:
        code = "HANDLER_FAILED"
        for kind, value in ((browser.NexusBrowserSessionLost, "BROWSER_SESSION_LOST"),
                            (browser.NexusBrowserComputerRequired, "BROWSER_COMPUTER_REQUIRED"),
                            (browser.NexusBrowserPermissionRequired, "BROWSER_PERMISSION_REQUIRED"),
                            (browser.NexusBrowserTunnelUnavailable, "BROWSER_TUNNEL_UNAVAILABLE"),
                            (browser.NexusBrowserActionFailed, "BROWSER_ACTION_FAILED"),
                            (browser.NexusBrowserUnavailable, "BROWSER_UNAVAILABLE")):
            if isinstance(exc, kind):
                code = value
                break
    return {"code": code, "message": "Agent operation failed (" + code + ")."}


class HostedCapabilityRegistry:
    """The declaration subset of NexusAgentServer; no listener or credentials."""

    def __init__(self) -> None:
        self.handlers: Dict[str, Any] = {}
        self.stream_handlers: Dict[str, Any] = {}
        self.frozen = False

    def add_handler(self, intent: str, handler: Any) -> None:
        if self.frozen:
            raise NexusAgentError("Declare all capabilities before exporting the MCP server")
        self.handlers[intent] = handler

    def stream_handler(self, intent: str) -> Any:
        def register(handler: Any) -> Any:
            if self.frozen:
                raise NexusAgentError("Declare all capabilities before exporting the MCP server")
            self.stream_handlers[intent] = handler
            return handler
        return register


def create_mcp_server(agent: Any) -> Any:
    # Use FastMCP's real server, tool validation, transport and session machinery.
    # Only the business-call adapter below is Nexus-specific.
    from .fastmcp import NexusMCPServer
    server = NexusMCPServer(agent.cloud_name, mask_error_details=True)
    from fastmcp.tools import Tool, ToolResult
    from fastmcp.server.dependencies import get_http_headers
    from fastmcp.exceptions import ToolError
    from jsonschema import Draft202012Validator
    from pydantic import PrivateAttr

    def failed_result(exc):
        failure = _failure(exc)
        return ToolResult(content=failure["message"], is_error=True,
                          meta={"nexus": {"failure": failure}})

    registry = agent.server
    contract = {"computer": agent.computer.to_dict(), "mobile": agent.mobile.to_dict()}

    class CapabilityTool(Tool):
        _intent: str = PrivateAttr()
        _spec: Any = PrivateAttr()
        _validator: Any = PrivateAttr()

        async def run(self, arguments: dict[str, Any]) -> ToolResult:
            # Validation errors must not echo user input or schema fragments.
            if not self._validator.is_valid(arguments):
                raise ToolError("Invalid capability arguments")
            try:
                context = NexusRunContext.from_env(headers=get_http_headers())
            except Exception as exc:
                return failed_result(exc)
            token = set_current_run(context)
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": self.name, "arguments": arguments}}
            payload = {"protocol": "mcp", "selector": self.name, "request": request}
            envelope = AgentEnvelope(
                version="1.0", intent=self._intent, intent_version=self._spec.version,
                task_id=context.run_id or str(uuid.uuid4()), source_agent="agent://nexus/caller",
                tenant=agent.tenant, hop_limit=1, payload=payload, run_context=context,
                raw={"version": "1.0", "intent": self._intent, "payload": payload},
            )
            try:
                handler = registry.handlers.get(self._intent)
                if handler is not None:
                    # Sync handlers may block on chat.ask or SSH. Never block the
                    # MCP event loop; to_thread preserves ContextVars.
                    result = await asyncio.to_thread(handler, envelope)
                    if inspect.isawaitable(result):
                        result = await result
                else:
                    stream = registry.stream_handlers[self._intent](envelope)
                    result = {"ok": True}
                    if inspect.isawaitable(stream):
                        stream = await stream
                    if hasattr(stream, "__aiter__"):
                        try:
                            async for item in stream:
                                result = _stream_result(item, result)
                        finally:
                            await stream.aclose()
                    else:
                        # Consume a sync generator in one worker so its context
                        # is stable throughout iteration and generator cleanup.
                        def consume() -> Any:
                            final = {"ok": True}
                            try:
                                for item in stream:
                                    final = _stream_result(item, final)
                                return final
                            finally:
                                close = getattr(stream, "close", None)
                                if close is not None:
                                    close()
                        result = await asyncio.to_thread(consume)
                if isinstance(result, AgentResponse):
                    if result.status >= 400:
                        raise ToolError("Agent capability rejected the request")
                    result = result.body
                # Let the existing SDK serializer preserve MCP content and handle
                # dataclasses/bytes consistently with the edge bridge.
                from .fastmcp import _json_value
                result = _json_value(result)
                if isinstance(result, dict) and isinstance(result.get("content"), list):
                    if result.get("isError"):
                        raise ToolError("Agent capability rejected the request")
                    from mcp.types import CallToolResult
                    content = CallToolResult.model_validate(result)
                    return ToolResult(content=content.content, structured_content=content.structuredContent)
                return ToolResult(content=json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result,
                                  structured_content=result if isinstance(result, dict) else None)
            except Exception as exc:
                # Never leak arbitrary exception repr, input, token or endpoint.
                return failed_result(exc)
            finally:
                reset_current_run(token)
                await asyncio.to_thread(context.close)

    names = set()
    for intent, spec in agent._specs.items():
        descriptor = spec.tool
        if descriptor is None:
            continue  # tool=False/cloud_publish=False stays private to the edge.
        if descriptor.name in names:
            raise NexusAgentError("MCP capability names must be unique")
        names.add(descriptor.name)
        Draft202012Validator.check_schema(descriptor.input_schema)
        policy = descriptor.to_dict(intent=intent, intent_version=spec.version)
        policy = {key: value for key, value in policy.items() if key not in {"name", "title", "description", "input_schema", "intent", "intent_version"}}
        tool = CapabilityTool(name=descriptor.name, title=descriptor.title,
                              description=descriptor.description,
                              parameters=dict(descriptor.input_schema),
                              meta={"nexus": {**policy, "agent_contract": contract}})
        tool._intent, tool._spec = intent, spec
        tool._validator = Draft202012Validator(descriptor.input_schema)
        server.fastmcp.add_tool(tool)
        server._nexus_tool_policy[descriptor.name] = policy
    if not names:
        raise NexusAgentError("Declare at least one exported @agent.capability or @agent.stream_capability")
    registry.frozen = True
    return server


def _stream_result(item: Any, previous: Any) -> Any:
    """Match the native invoke stream's final-result contract, not its envelope."""
    if isinstance(item, SseEvent):
        return json.loads(item.data) if item.event == "result" else previous
    if isinstance(item, dict) and item.get("event") == "result":
        data = item.get("data")
        return json.loads(data) if isinstance(data, str) else data
    return previous
