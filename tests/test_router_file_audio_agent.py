from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


EXAMPLE = Path(__file__).parents[1] / "examples" / "router_file_audio_agent.py"
SPEC = importlib.util.spec_from_file_location("router_file_audio_agent", EXAMPLE)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Recorder:
    def __init__(self) -> None:
        self.values = []

    def set(self, value):
        self.values.append(("set", value))

    def update(self, item_id, **value):
        self.values.append(("update", item_id, value))

    def title(self, value):
        self.values.append(value)

    def say(self, value):
        self.values.append(value)


class FilePlane:
    def __init__(self, values):
        self.values = values

    def iter_bytes(self, reference):
        yield self.values[reference["file_id"]]


class OutputPlane:
    def __init__(self) -> None:
        self.manifest = None

    def upload_file(self, path, **options):
        self.manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        return {"file_id": "output-id", "name": Path(path).name, **options}


def test_example_declares_and_reads_file_and_audio_inputs():
    regular = {
        "file_id": "file-1",
        "name": "notes.txt",
        "content_type": "text/plain",
        "size_bytes": 5,
        "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        "source_kind": "upload",
    }
    recording = {
        "file_id": "audio-1",
        "name": "voice.webm",
        "content_type": "audio/webm",
        "size_bytes": 4,
        "sha256": "3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7",
        "source_kind": "audio",
    }
    output = OutputPlane()
    ctx = SimpleNamespace(
        run_id="run-1",
        input=SimpleNamespace(files=[regular, recording], audio=[recording]),
        files=FilePlane({"file-1": b"hello", "audio-1": b"data"}),
        output=output,
        display=Recorder(),
        plan=Recorder(),
        chat=Recorder(),
    )

    result = MODULE.inspect_uploads({"message": "Check these"}, ctx)

    assert MODULE.TOOL.input_modalities == ("text", "audio")
    assert MODULE.TOOL.chat is True
    assert result["file_count"] == 1
    assert result["audio_count"] == 1
    assert output.manifest["files"][0]["integrity"] is True
    assert output.manifest["audio"][0]["integrity"] is True
    assert "Voice recordings: **1**" in ctx.chat.values[-1]


def test_example_uses_its_own_stable_cloud_identity(monkeypatch):
    monkeypatch.setenv("NEXUS_ROUTER_URL", "http://127.0.0.1:7446/")
    monkeypatch.setenv("NEXUS_AGENT_ADDRESS", "192.0.2.20")  # No host-network discovery in contract tests.
    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)
    monkeypatch.delenv("NEXUS_AGENT_CLOUD_NAME", raising=False)

    agent = MODULE.build_agent()
    try:
        registration = agent.registrations()[0]
        assert agent.agent_id == "router-file-audio-agent"
        assert registration.origin == "agent://default/router-file-audio-agent"
        assert registration.cloud is not None
        assert registration.cloud.agent_name == "File and Voice Inspector"
    finally:
        agent.server.server_close()
