import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nexus_agent.computer_runtime import NexusComputerRuntime, _decode_command_output


class CommandEncodingTests(unittest.TestCase):
    def test_utf8_limit_does_not_split_character(self):
        self.assertEqual(_decode_command_output("中文🙂".encode(), 7), "中文")
        self.assertEqual(_decode_command_output("中文🙂".encode(), 10), "中文🙂")

    @unittest.skipUnless(os.name == "nt", "Windows pipe encodings")
    def test_legacy_windows_output(self):
        self.assertEqual(_decode_command_output("中文错误".encode("mbcs"), 100), "中文错误")

    @unittest.skipUnless(os.name == "nt", "Windows shell")
    def test_real_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = NexusComputerRuntime(root / "state")
            try:
                with patch.object(runtime, "_resolved_root", return_value=root):
                    for command, stdout, stderr in [
                        ("echo 中文输出 & echo 中文错误 1>&2", "中文输出", "中文错误"),
                        (f'"{sys.executable}" -c "print(\'中文🙂\')"', "中文🙂", ""),
                        ('powershell.exe -NoProfile -Command "Write-Output \'中文输出\'"', "中文输出", ""),
                        ('chcp 936 >nul & echo 中文输出', "中文输出", ""),
                    ]:
                        result = runtime._run_command({"command": command})
                        self.assertEqual(result["exit_code"], 0)
                        self.assertEqual(result["stdout"].strip(), stdout)
                        self.assertEqual(result["stderr"].strip(), stderr)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
