"""Explicit MCP results for edge capabilities, without an MCP dependency."""
import base64
import json
from typing import Any, Mapping, Optional, Sequence


class McpToolResult:
    """Return native MCP content rather than a JSON business object.

    Ordinary dictionaries retain the existing JSON/text contract. The internal
    versioned wire wrapper is reserved for SDK/adapter use, including SSE.
    """

    def __init__(self, *, content: Sequence[Mapping[str, Any]],
                 structured_content: Optional[Mapping[str, Any]] = None,
                 is_error: bool = False, meta: Optional[Mapping[str, Any]] = None):
        if not isinstance(content, (list, tuple)) or len(content) > 256:
            raise ValueError("MCP content must contain at most 256 blocks")
        if type(is_error) is not bool:
            raise ValueError("MCP is_error must be a boolean")
        result = {"content": list(content), "isError": is_error}
        for key, value in (("structuredContent", structured_content), ("_meta", meta)):
            if value is not None:
                if not isinstance(value, Mapping):
                    raise ValueError("MCP " + key + " must be an object")
                result[key] = dict(value)
        try:
            self._json = json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("MCP result must contain finite JSON values") from exc
        normalized = json.loads(self._json)
        for block in normalized["content"]:
            if not isinstance(block, dict):
                raise ValueError("MCP content blocks must be objects")
            kind = block.get("type")
            if kind == "text":
                fields = ("text",)
            elif kind in ("image", "audio"):
                fields = ("data", "mimeType")
                try:
                    base64.b64decode(block.get("data", ""), validate=True)
                except (TypeError, ValueError) as exc:
                    raise ValueError("MCP media data must be base64") from exc
            elif kind == "resource_link":
                fields = ("uri", "name")
            elif kind == "resource":
                resource = block.get("resource")
                if not isinstance(resource, dict) or not isinstance(resource.get("uri"), str):
                    raise ValueError("MCP resource must have a URI")
                if not isinstance(resource.get("text"), str) and not isinstance(resource.get("blob"), str):
                    raise ValueError("MCP resource must have text or blob")
                fields = ()
            else:
                raise ValueError("Unsupported MCP content type")
            if any(not isinstance(block.get(key), str) for key in fields):
                raise ValueError("MCP content fields must be strings")
            # MCP/Pydantic models may dump optional annotations as null.
            if block.get("annotations") is None:
                block.pop("annotations", None)
            if "annotations" in block and not isinstance(block["annotations"], dict):
                raise ValueError("MCP annotations must be an object")
        self._json = json.dumps(normalized, ensure_ascii=False, allow_nan=False)

    def to_dict(self):
        return json.loads(self._json)

    def to_wire(self):
        return {"nexus_mcp_result_version": 1, "result": self.to_dict()}
