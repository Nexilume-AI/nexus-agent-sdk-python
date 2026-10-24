import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from nexus_agent.computer_runtime import NexusComputerRuntime, RuntimeOperationError


class WorkspaceSelectionTests(unittest.TestCase):
    def test_run_grant_is_ephemeral_and_browse_is_not_a_grant(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); paired = root/'paired'; chosen = root/'chosen'; chosen.mkdir()
            (chosen/'hello.txt').write_text('hello', encoding='utf-8')
            runtime = NexusComputerRuntime(root/'state')
            runtime.config['workspace_root'] = str(paired)
            try:
                listing = runtime.dispatch('workspace.browse_directories', 'workspace.select', {'path': str(root)})
                self.assertIn('chosen', [d['name'] for d in listing['directories']])
                with self.assertRaises(RuntimeOperationError):
                    runtime.dispatch('workspace.browse_directories', 'files.list', {'path': str(root)})
                payload = {'workspace_root': str(chosen), 'path': 'hello.txt'}
                with self.assertRaises(RuntimeOperationError):
                    runtime.dispatch('workspace.read_file', 'files.read', payload)
                result = runtime.dispatch('workspace.read_file', 'files.read', {**payload, '_authorized_workspace_roots': [str(chosen)]})
                self.assertEqual(result['content'], 'hello')
                with self.assertRaises(RuntimeOperationError):
                    runtime.dispatch('workspace.read_file', 'files.read', payload)
                with self.assertRaises(RuntimeOperationError):
                    runtime.dispatch('workspace.read_file', 'files.read', {**payload, 'path': '../outside', '_authorized_workspace_roots': [str(chosen)]})
                missing = root/'not-created'
                with self.assertRaises(RuntimeOperationError):
                    runtime.dispatch('workspace.browse_directories', 'workspace.select', {'path': str(missing)})
                self.assertFalse(missing.exists())
            finally:
                runtime.close()


if __name__ == '__main__': unittest.main()
