"""Real terminal acceptance, including macOS zsh in the existing CI matrix."""

import asyncio
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nexus_agent.computer_runtime import NexusComputerRuntime, _TerminalProcess


@unittest.skipIf(os.name == "nt", "POSIX PTY acceptance")
class TerminalPtyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        # Isolate from the CI/user's shell rc files and make the prompt observable.
        (self.root / ".zshrc").write_text("PROMPT='NEXUS_PTY> '\nRPROMPT=''\n", encoding="utf-8")
        (self.root / ".bashrc").write_text("PS1='NEXUS_PTY> '\n", encoding="utf-8")
        self.environment = mock.patch.dict(os.environ, {
            "HOME": str(self.root), "ZDOTDIR": str(self.root), "PS1": "NEXUS_PTY> ",
        })
        self.environment.start()
        self.terminals = []

    def tearDown(self):
        for terminal in self.terminals:
            terminal.close()
            self.assertIsNotNone(terminal.process.poll())
            self.assertFalse(terminal.reader.is_alive())
            self.assertIsNone(terminal._master_fd)
        self.environment.stop()
        self.directory.cleanup()

    def open_terminal(self, **kwargs):
        terminal = _TerminalProcess(cwd=self.root, **kwargs)
        self.terminals.append(terminal)
        self.wait_for(terminal, r"NEXUS_PTY> ")
        return terminal

    def wait_for(self, terminal, pattern, timeout=8):
        output = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            output += terminal.read(0.1)
            if re.search(pattern, output):
                return output
        self.fail(f"Missing {pattern!r} in terminal output: {output!r}")

    def test_prompt_echo_tty_resize_and_control_c(self):
        terminal = self.open_terminal(shell="auto", rows=31, cols=97)
        terminal.write("test -t 0 && test -t 1 && test -t 2 && printf 'TTY_%s\\n' OK\r")
        self.wait_for(terminal, r"TTY_OK\r?\n")
        terminal.write("stty size\r")
        self.wait_for(terminal, r"31 97\r?\n")
        terminal.resize(rows=42, cols=103)
        terminal.write("stty size\r")
        self.wait_for(terminal, r"42 103\r?\n")
        # Typed text must be echoed before Enter, not only after command execution.
        terminal.write("printf 'ECHO_%s\\n' OK")
        self.wait_for(terminal, "ECHO_")
        terminal.write("\r")
        self.wait_for(terminal, r"ECHO_OK\r?\n")
        terminal.write("sleep 30\r")
        self.wait_for(terminal, "sleep 30")
        time.sleep(0.2)
        terminal.write("\x03")
        self.wait_for(terminal, r"NEXUS_PTY> ", timeout=4)
        terminal.write("printf 'ALIVE_%s\\n' OK\r")
        self.wait_for(terminal, r"ALIVE_OK\r?\n")
        terminal.write("exit\r")
        self.assertEqual(terminal.process.wait(timeout=5), 0)

    @unittest.skipUnless(shutil.which("zsh"), "Requires zsh")
    def test_macos_shell_selection_has_prompt_and_zsh_line_editing(self):
        with mock.patch("nexus_agent.computer_runtime.platform_module.system", return_value="Darwin"):
            terminal = self.open_terminal(shell="auto")
        # Verify execution rather than mistaking echoed command text for output.
        terminal.write("printf 'ZSH_%s\\n' \"$ZSH_VERSION\"\r")
        self.wait_for(terminal, r"ZSH_\d+\.")
        terminal.write("printf 'EDIT_%s\\n' OX\x7fK\r")
        self.wait_for(terminal, r"EDIT_OK\r?\n")

    def test_close_stops_foreground_job_and_releases_pty(self):
        terminal = self.open_terminal(shell="auto")
        terminal.write("sleep 30\r")
        self.wait_for(terminal, "sleep 30")
        time.sleep(0.2)
        foreground = os.tcgetpgrp(terminal._master_fd)
        self.assertNotEqual(foreground, terminal.process.pid)
        terminal.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.killpg(foreground, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("Foreground process group survived terminal close")

    def test_rpc_and_stream_apply_initial_size_and_resize(self):
        runtime = NexusComputerRuntime(root=self.root / "runtime")
        with mock.patch.object(runtime, "_resolved_root", return_value=self.root):
            opened = runtime._terminal("terminal.open", {"cols": 80, "rows": 25})
            session_id = opened["session_id"]
            terminal = runtime._terminals[session_id]
            self.terminals.append(terminal)
            self.wait_for(terminal, "NEXUS_PTY> ")
            runtime._terminal("terminal.resize", {"session_id": session_id, "cols": 90, "rows": 35})
            terminal.write("stty size\r")
            self.wait_for(terminal, r"35 90\r?\n")
            runtime._terminal("terminal.close", {"session_id": session_id})

            async def stream():
                with mock.patch.object(runtime, "_send", new_callable=mock.AsyncMock) as send, mock.patch.object(
                    runtime, "_pump_terminal_stream", new_callable=mock.AsyncMock
                ):
                    await runtime._handle_terminal_stream_frame(None, {
                        "type": "terminal_stream_open", "stream_id": "pty", "cols": 82, "rows": 27,
                    })
                    self.assertEqual(send.call_args.args[1]["type"], "terminal_stream_opened")
                    terminal = runtime._terminals["pty"]
                    self.terminals.append(terminal)
                    self.wait_for(terminal, "NEXUS_PTY> ")
                    terminal.write("stty size\r")
                    self.wait_for(terminal, r"27 82\r?\n")
                    await runtime._handle_terminal_stream_frame(None, {
                        "type": "terminal_stream_resize", "stream_id": "pty", "cols": 101, "rows": 41,
                    })
                    terminal.write("stty size\r")
                    self.wait_for(terminal, r"41 101\r?\n")
                    await runtime._close_terminal_stream("pty")
            asyncio.run(stream())

    def test_live_stream_delivers_prompt_output_and_exit_then_cleans_up(self):
        async def scenario():
            runtime = NexusComputerRuntime(root=self.root / "runtime")
            with mock.patch.object(runtime, "_resolved_root", return_value=self.root), mock.patch.object(
                runtime, "_send", new_callable=mock.AsyncMock
            ) as send:
                async def wait_for_output(pattern):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        output = "".join(
                            call.args[1].get("data", "") for call in send.call_args_list
                            if call.args[1].get("type") == "terminal_stream_output"
                        )
                        if re.search(pattern, output):
                            return
                        await asyncio.sleep(0.05)
                    self.fail(f"Missing streamed {pattern!r}: {output!r}")

                await runtime._handle_terminal_stream_frame(None, {
                    "type": "terminal_stream_open", "stream_id": "live",
                })
                terminal = runtime._terminals["live"]
                self.terminals.append(terminal)
                pump = runtime._terminal_stream_tasks["live"]
                try:
                    await wait_for_output("NEXUS_PTY> ")
                    await runtime._handle_terminal_stream_frame(None, {
                        "type": "terminal_stream_input", "stream_id": "live",
                        "data": "printf 'STREAM_%s\\n' OK; exit\r",
                    })
                    await asyncio.wait_for(pump, timeout=8)
                    await wait_for_output(r"STREAM_OK\r?\n")
                    self.assertEqual(send.call_args.args[1]["type"], "terminal_stream_closed")
                    self.assertNotIn("live", runtime._terminals)
                    self.assertFalse(terminal.reader.is_alive())
                    self.assertIsNone(terminal._master_fd)
                finally:
                    await runtime._close_terminal_stream("live")
        asyncio.run(scenario())
