import importlib.util
import json
import pathlib
import struct
import sys
import unittest
import zlib
from types import SimpleNamespace
from unittest.mock import MagicMock


SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))


class EdgePrivateRunAgentExampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = SDK_ROOT / "examples" / "edge_private_run_agent.py"
        spec = importlib.util.spec_from_file_location("edge_private_run_agent", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        cls.path = path
        cls.module = module

    def test_example_is_single_file_auto_discovered_and_public_ipv6(self):
        source = self.path.read_text(encoding="utf-8")
        self.assertIn('router="http://192.168.250.1:7446/"', source)
        self.assertIn('advertise_address="auto"', source)
        self.assertIn("public_ipv6=True", source)
        for forbidden in (
            "argparse",
            "--password",
            "--private-key",
            "--cert",
            "--key",
            "NEXUS_API_KEY",
        ):
            self.assertNotIn(forbidden, source)
        for surface in (
            "display.title",
            "plan.set",
            "browser.frame",
            "chat.ask",
            "chat.say",
            "workspace.list",
            "workspace.read_text",
            "workspace.write_text",
            "terminal.run",
            "output.write_text",
            "output.ready",
        ):
            self.assertIn(surface, source)

    def test_handler_exercises_the_private_run_surfaces(self):
        note = "Instruction: inspect my workspace\nEntries inspected: 1\n"
        workspace = SimpleNamespace(
            list=MagicMock(
                return_value=(
                    SimpleNamespace(name="input.txt", kind="file", size=12),
                )
            ),
            write_text=MagicMock(return_value={"path": self.module.NOTE_PATH}),
            read_text=MagicMock(return_value=note),
        )
        chat = SimpleNamespace(
            say=MagicMock(return_value=True),
            history=MagicMock(return_value=[]),
            ask=MagicMock(
                side_effect=(
                    SimpleNamespace(value="continue", text="continue"),
                    SimpleNamespace(value="list", text="list"),
                    SimpleNamespace(value="pwd", text="pwd"),
                    SimpleNamespace(value="done", text="done"),
                )
            ),
        )
        ctx = SimpleNamespace(
            run_id="run-edge-example",
            turn_index=1,
            is_resumed=False,
            computer=SimpleNamespace(enabled=True),
            display=SimpleNamespace(title=MagicMock(return_value=True)),
            plan=SimpleNamespace(set=MagicMock(), update=MagicMock()),
            chat=chat,
            run=SimpleNamespace(messages=MagicMock(return_value=[
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "list"},
            ])),
            workspace=workspace,
            shell=SimpleNamespace(write=MagicMock()),
            terminal=SimpleNamespace(
                run=MagicMock(
                    return_value=SimpleNamespace(exit_code=0, stdout="input.txt\n")
                )
            ),
            browser=SimpleNamespace(frame=MagicMock()),
            output=SimpleNamespace(
                write_text=MagicMock(return_value={"path": self.module.OUTPUT_PATH}),
                ready=MagicMock(return_value=True),
            ),
        )

        result = self.module.operate_workspace(
            {"message": "inspect my workspace"}, ctx
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["run_id"], "run-edge-example")
        self.assertEqual(result["entry_count"], 1)
        self.assertEqual(result["chat_turns"], 3)
        self.assertEqual(workspace.list.call_count, 2)
        workspace.write_text.assert_called_once_with(self.module.NOTE_PATH, note)
        workspace.read_text.assert_called_once_with(self.module.NOTE_PATH)
        self.assertEqual(ctx.terminal.run.call_count, 2)
        ctx.terminal.run.assert_called_with("pwd", cwd=".", timeout=30)
        ctx.browser.frame.assert_called_once()
        self.assertEqual(chat.ask.call_count, 4)
        ctx.output.write_text.assert_called_once()
        rendered = ctx.output.write_text.call_args.args[1]
        self.assertEqual(json.loads(rendered)["instruction"], "inspect my workspace")
        ctx.output.ready.assert_called_once()

    def test_resumed_input_reuses_run_id_and_run_scoped_history(self):
        chat = SimpleNamespace(
            say=MagicMock(return_value=True),
        )
        run = SimpleNamespace(
            messages=MagicMock(
                return_value=[
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "first answer"},
                    {"role": "user", "content": "list"},
                ]
            )
        )
        ctx = SimpleNamespace(
            run_id="same-run",
            turn_index=2,
            is_resumed=True,
            computer=SimpleNamespace(enabled=True),
            plan=SimpleNamespace(set=MagicMock(), update=MagicMock()),
            chat=chat,
            run=run,
            workspace=SimpleNamespace(
                list=MagicMock(
                    return_value=(SimpleNamespace(name="input.txt", kind="file", size=12),)
                )
            ),
        )

        result = self.module.operate_workspace({"message": "list"}, ctx)

        self.assertEqual(result["run_id"], "same-run")
        self.assertEqual(result["turn_index"], 2)
        self.assertEqual(result["message_count"], 3)
        run.messages.assert_called_once_with()
        self.assertIn("input.txt", chat.say.call_args.args[0])

    def test_demo_browser_frame_is_visible_and_not_a_black_pixel(self):
        frame = self.module.DEMO_FRAME

        self.assertTrue(frame.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(frame), 500)
        self.assertEqual(
            struct.unpack(">II", frame[16:24]),
            (self.module.DEMO_FRAME_WIDTH, self.module.DEMO_FRAME_HEIGHT),
        )
        offset = 8
        compressed = bytearray()
        while offset < len(frame):
            size = struct.unpack(">I", frame[offset : offset + 4])[0]
            chunk_type = frame[offset + 4 : offset + 8]
            chunk_data = frame[offset + 8 : offset + 8 + size]
            if chunk_type == b"IDAT":
                compressed.extend(chunk_data)
            offset += size + 12
        pixels = zlib.decompress(bytes(compressed))
        self.assertIn(bytes((189, 252, 115)), pixels)  # Nexilume Lume
        self.assertIn(bytes((247, 247, 244)), pixels)  # Nexilume Paper


if __name__ == "__main__":
    unittest.main()
