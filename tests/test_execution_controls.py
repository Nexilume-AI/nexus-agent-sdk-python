import json
import pathlib
import sys
import unittest

SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent import McpToolDescriptor, NexusExecutionProfile, NexusRunContext


class ExecutionControlsTest(unittest.TestCase):
    def test_profile_slash_audio_manifest_is_bounded(self):
        profile = NexusExecutionProfile(
            id="balanced",
            label="Balanced",
            model="gpt-5",
            is_default=True,
            reasoning_efforts=("low", "medium", "high"),
            default_reasoning_effort="medium",
            context_window=400_000,
        )
        descriptor = McpToolDescriptor(
            name="inspect",
            task=True,
            slash_command="inspect",
            slash_description="Inspect attached files",
            input_modalities=("text", "audio"),
            execution_profiles=(profile,),
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "audio": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["content"],
            },
        )
        value = descriptor.to_dict(intent="inspect", intent_version=1)
        self.assertEqual(value["slash_command"], "inspect")
        self.assertEqual(value["input_modalities"], ["text", "audio"])
        self.assertEqual(value["execution_profiles"][0]["context_window"], 400_000)
        self.assertTrue(value["execution_profiles"][0]["is_default"])

    def test_profiles_allow_model_managed_reasoning_and_only_one_publisher_default(self):
        fixed = NexusExecutionProfile(id="fixed", label="Fixed", model="local-model")
        self.assertEqual(fixed.reasoning_efforts, ())
        self.assertEqual(fixed.default_reasoning_effort, "")
        with self.assertRaisesRegex(ValueError, "one default"):
            McpToolDescriptor(
                name="inspect",
                task=True,
                execution_profiles=(
                    NexusExecutionProfile(id="a", label="A", model="model-a", is_default=True),
                    NexusExecutionProfile(id="b", label="B", model="model-b", is_default=True),
                ),
            )

    def test_run_context_exposes_execution_and_private_input_files(self):
        files = [
            {"file_id": "audio-1", "name": "voice.webm", "source_kind": "audio"},
            {"file_id": "file-1", "name": "notes.txt", "source_kind": "computer"},
        ]
        context = NexusRunContext.from_env(headers={
            "X-Nexus-AGUI-Run-Id": "run-1",
            "X-Nexus-AGUI-Events-Url": "https://cloud.invalid/events",
            "X-Nexus-AGUI-Token": "secret",
            "X-Nexus-Interaction-Token": "usage-secret",
            "X-Nexus-Usage-Url": "https://cloud.invalid/usage",
            "X-Nexus-Execution-Profile": "balanced",
            "X-Nexus-Execution-Model": "gpt-5",
            "X-Nexus-Reasoning-Effort": "high",
            "X-Nexus-Execution-Context-Window": "400000",
            "X-Nexus-Input-Files": json.dumps(files),
        })
        self.assertEqual(context.execution.profile, "balanced")
        self.assertEqual(context.execution.reasoning_effort, "high")
        self.assertEqual(context.input.audio[0]["name"], "voice.webm")
        self.assertEqual(context.input.files[1]["source_kind"], "computer")
        self.assertNotIn("secret", repr(context))
        headers = context.usage.gateway_headers(event_id="gateway-call-1")
        self.assertEqual(headers["X-Nexus-Agent-Run-Id"], "run-1")
        self.assertEqual(headers["X-Nexus-Agent-Context-Window"], "400000")
        self.assertEqual(headers["X-Nexus-Agent-Usage-Event-Id"], "gateway-call-1")
        self.assertNotIn("usage-secret", repr(context.usage))

    def test_audio_declaration_requires_matching_schema(self):
        with self.assertRaisesRegex(ValueError, "audio array"):
            McpToolDescriptor(
                name="voice",
                task=True,
                input_modalities=("text", "audio"),
                input_schema={"type": "object", "properties": {"content": {"type": "string"}}},
            )

    def test_image_declaration_requires_matching_schema(self):
        with self.assertRaisesRegex(ValueError, "attachments array"):
            McpToolDescriptor(
                name="vision",
                task=True,
                input_modalities=("text", "image"),
                input_schema={"type": "object", "properties": {"content": {"type": "string"}}},
            )

        descriptor = McpToolDescriptor(
            name="vision",
            task=True,
            input_modalities=("text", "image"),
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "attachments": {"type": "array", "items": {"type": "object"}},
                },
            },
        )
        self.assertEqual(descriptor.input_modalities, ("text", "image"))


if __name__ == "__main__":
    unittest.main()
