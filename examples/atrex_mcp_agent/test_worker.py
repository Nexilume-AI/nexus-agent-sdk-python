import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import worker


class WorkerStartupTests(unittest.TestCase):
    def test_persistent_codex_home_exists_before_optimizer_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'work').mkdir()
            (root / 'input').mkdir()
            (root / 'input/spec.json').write_text(json.dumps({
                'provider': 'custom_openai', 'protocol': 'openai', 'model': 'test-model',
            }))

            def mapped_path(value):
                return root / value.lstrip('/')

            class SetupReached(Exception):
                pass

            def check_setup(*args, **kwargs):
                self.assertEqual(os.environ['CODEX_HOME'], '/work/home/.codex')
                self.assertTrue((root / 'work/home/.codex').is_dir())
                raise SetupReached

            with patch.object(worker, 'Path', side_effect=mapped_path), \
                    patch.object(worker, 'ThreadingHTTPServer'), \
                    patch.object(worker.threading, 'Thread'), \
                    patch.object(worker.shutil, 'copytree', side_effect=check_setup), \
                    patch.dict(os.environ):
                for _ in range(2):
                    with self.assertRaises(SetupReached):
                        worker.main()


if __name__ == '__main__':
    unittest.main()
