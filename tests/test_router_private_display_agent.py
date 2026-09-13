import importlib.util
import hashlib
import json
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))


class RouterPrivateDisplayExampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = SDK_ROOT / "examples" / "router_private_display_agent.py"
        spec = importlib.util.spec_from_file_location("router_private_display_agent", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        cls.path = path
        cls.module = module

    def test_example_declares_chat_task_and_computer_workspace_contract(self):
        module = self.module

        self.assertTrue(module.PRIVATE_DISPLAY_TOOL.chat)
        self.assertTrue(module.PRIVATE_DISPLAY_TOOL.task)
        self.assertTrue(module.PRIVATE_DISPLAY_TOOL.continuable)
        self.assertTrue(module.PRIVATE_DISPLAY_TOOL.interactive)
        self.assertFalse(module.PRIVATE_DISPLAY_TOOL.demo)
        source = self.path.read_text(encoding="utf-8")
        for value in ("files.list", "files.read", "files.write", "command.execute"):
            self.assertIn(value, source)
        for surface in (
            "display.title", "plan.set", "shell.write", "browser.frame", "chat.ask",
            "chat.say", "workspace.read_text", "workspace.write_text",
            "terminal.run", "output.write_text", "output.ready",
        ):
            self.assertIn(surface, source)

    def test_resumed_turn_exercises_every_private_display_surface(self):
        sentinel = "nexus-private-display-sentinel-e2e-unit"
        workspace = SimpleNamespace(
            list=MagicMock(return_value=(SimpleNamespace(name="private-display-input.txt"),)),
            read_text=MagicMock(return_value=sentinel),
            write_text=MagicMock(return_value={"path": "private-display-result.json"}),
        )
        terminal = SimpleNamespace(
            run=MagicMock(return_value=SimpleNamespace(
                exit_code=0,
                stdout="7.5.0\n",
            )),
        )
        chat = SimpleNamespace(
            say=MagicMock(return_value=True),
            ask=MagicMock(side_effect=(
                SimpleNamespace(value="confirm"),
                SimpleNamespace(value="detailed"),
            )),
        )
        nexus = SimpleNamespace(
            run_id="run-private-display",
            turn_index=2,
            display=SimpleNamespace(title=MagicMock(return_value=True)),
            workspace=workspace,
            terminal=terminal,
            chat=chat,
            run=SimpleNamespace(messages=MagicMock(return_value=(
                {"role": "user", "content": self.module.RECOVERY_PROBE_MESSAGE},
                {"role": "user", "content": "inspect my computer"},
            ))),
            plan=SimpleNamespace(set=MagicMock(), update=MagicMock()),
            shell=SimpleNamespace(write=MagicMock()),
            browser=SimpleNamespace(frame=MagicMock()),
            output=SimpleNamespace(
                write_text=MagicMock(return_value={"path": "private-display-result.json"}),
                ready=MagicMock(return_value=True),
            ),
        )

        result = self.module.private_display({"message": "inspect my computer"}, nexus)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["run_id"], "run-private-display")
        self.assertEqual(result["detail_level"], "detailed")
        self.assertEqual(result["turn_index"], 2)
        self.assertEqual(len(result["history_messages"]), 2)
        self.assertEqual(
            result["sentinel_sha256"],
            hashlib.sha256(sentinel.encode("utf-8")).hexdigest(),
        )
        self.assertEqual([call.kwargs["key"] for call in chat.ask.call_args_list], [
            "workspace-folder-confirmation", "result-detail-level",
        ])
        terminal.run.assert_called_once()
        nexus.browser.frame.assert_called_once()
        workspace.write_text.assert_called_once()
        nexus.output.write_text.assert_called_once()
        rendered = workspace.write_text.call_args.args[1]
        self.assertEqual(json.loads(rendered)["run_id"], "run-private-display")
        self.assertEqual(nexus.output.write_text.call_args.args[1], rendered)
        nexus.output.ready.assert_called_once()
        self.assertIn("private-display-result.json is ready", chat.say.call_args_list[-1].args[0])
        nexus.display.title.assert_called_once_with("Private workspace inspection")

    def test_disconnect_probe_publishes_checkpoint_and_holds_the_invocation(self):
        nexus = SimpleNamespace(
            display=SimpleNamespace(title=MagicMock()),
            plan=SimpleNamespace(set=MagicMock()),
            chat=SimpleNamespace(say=MagicMock()),
        )
        with patch.object(
            self.module,
            "_wait_for_disconnect",
            side_effect=RuntimeError("terminated by test harness"),
        ):
            with self.assertRaisesRegex(RuntimeError, "terminated by test harness"):
                self.module.private_display(
                    {"message": self.module.RECOVERY_PROBE_MESSAGE},
                    nexus,
                )

        nexus.chat.say.assert_called_once_with(self.module.RECOVERY_CHECKPOINT_MESSAGE)
        self.assertEqual(
            nexus.plan.set.call_args.args[0][0]["id"],
            "disconnect",
        )


if __name__ == "__main__":
    unittest.main()
