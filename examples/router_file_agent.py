"""Private large-file roundtrip through Nexus. Does not parse or execute files."""
import argparse
import signal
import tempfile
import threading
from pathlib import Path

from nexus_agent import McpToolDescriptor, NexusAgent, NexusRunContext

FILE_TOOL = McpToolDescriptor(name="file_chat", title="Private file assistant",
    description="Verify private file transfer and return an immutable copy; no document parsing.",
    input_schema={"type": "object", "properties": {
        "message": {"type": "string"}, "files": {"type": "array", "maxItems": 8,
            "items": {"type": "object", "properties": {"file_id": {"type": "string"},
                "name": {"type": "string"}, "content_type": {"type": "string"},
                "size_bytes": {"type": "integer"}, "sha256": {"type": "string"},
                "source_kind": {"type": "string"}, "source_label": {"type": "string"}},
                "required": ["file_id"], "additionalProperties": False}}},
        "required": ["message"], "additionalProperties": False},
    chat=True, task=True, continuable=True, interactive=True)


def file_chat(payload, ctx: NexusRunContext):
    results = []
    for reference in payload.get("files", []):
        # Never trust a caller-supplied name as a filesystem path.
        with tempfile.TemporaryDirectory(prefix="nexus-file-") as directory:
            path = Path(directory) / "verified-copy.bin"
            with ctx.trace.step("download-and-verify"):
                ctx.files.download(reference, path)
            with ctx.trace.step("upload-output"):
                results.append(ctx.output.upload_file(path))
    ctx.chat.say(f"Verified {len(results)} file(s). Download the immutable copies from Run context → Files.")
    return {"message": "File transfer complete.", "outputs": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--router", default="http://192.168.250.1:7446")
    args = parser.parse_args()
    agent = NexusAgent(router=args.router, tenant="demo", agent_id="file-agent", advertise_address="auto",
        cloud_publish=True, cloud_name="Private file assistant")
    agent.capability("demo.file_chat", tool=FILE_TOOL)(file_chat)
    done = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: done.set())
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    handle = agent.start()
    try:
        print("File Agent registered; use its Nexus Private Display to attach a file.")
        done.wait()
    finally:
        handle.close()


if __name__ == "__main__":
    main()
