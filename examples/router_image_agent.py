"""Protected image roundtrip example; no external model API or Cloud credentials.

Run with --router pointing at an enrolled trusted LAN router. The router's
existing Direct/Relay choice is respected. Private Display requires a transport
supporting MCP tasks; the existing Cloud readiness message explains limitations.
"""
from __future__ import annotations

import argparse
import hashlib
import signal
import threading
from typing import Any, Dict

from nexus_agent import McpToolDescriptor, NexusAgent, NexusRunContext


IMAGE_TOOL = McpToolDescriptor(
    name="image_chat", title="Private image assistant",
    description="Read caller-attached images and return a protected copy with its SHA-256. This is a transport example, not a vision model.",
    input_schema={
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "attachments": {"type": "array", "maxItems": 4, "items": {
                "type": "object", "properties": {"asset_id": {"type": "string"}, "content_type": {"type": "string"}},
                "required": ["asset_id", "content_type"], "additionalProperties": False,
            }},
        },
        "required": ["message"], "additionalProperties": False,
    },
    chat=True, task=True, continuable=True, interactive=True,
    input_modalities=("text", "image"),
)


def image_chat(payload: Dict[str, Any], nexus: NexusRunContext) -> Dict[str, Any]:
    references = payload.get("attachments") or []
    if not references:
        nexus.chat.say("Attach an image and tell me what to do. This example verifies protected image transport; it does not call a vision model.")
        return {"message": "Waiting for an attached image."}
    results = []
    for reference in references:
        data = nexus.media.read_image(reference)
        digest = hashlib.sha256(data).hexdigest()
        block = nexus.output.image(data, content_type=reference["content_type"], title="Protected image copy")
        results.append({"sha256": digest, "bytes": len(data), "image": block})
    nexus.chat.say(f"Received {len(results)} image(s). The protected copies are shown below.")
    return {"message": "Image roundtrip complete.", "images": results}


def build_agent(router: str) -> NexusAgent:
    agent = NexusAgent(router=router, tenant="demo", agent_id="image-agent", advertise_address="auto",
        cloud_publish=True, cloud_name="Private image assistant")
    agent.capability("demo.image_chat", tool=IMAGE_TOOL)(image_chat)
    return agent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--router", default="http://192.168.250.1:7446")
    args = parser.parse_args()
    done = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: done.set())
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    handle = build_agent(args.router).start()
    try:
        print("Local image Agent registered. Cloud synchronization uses the router enrollment.")
        done.wait()
    finally:
        handle.close()


if __name__ == "__main__":
    main()
