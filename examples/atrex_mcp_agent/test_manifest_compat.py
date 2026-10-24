import json
from pathlib import Path
import tempfile
import unittest
from manifest_compat import repair_cuda_manifest


class ManifestTests(unittest.TestCase):
    def test_native_libraries_are_declared_and_repair_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'kernel.py').write_text('nvrtc.nvrtcCompileProgram(); driver.cuLaunchKernel()')
            path = root / 'solution.json'
            path.write_text(json.dumps({'spec': {'dependencies': ['torch', 'ctypes']},
                                        'dependency_roles': {'torch': 'allocation only'}}))
            self.assertTrue(repair_cuda_manifest(root))
            result = json.loads(path.read_text())
            self.assertEqual(result['spec']['dependencies'], ['torch', 'ctypes', 'nvrtc', 'cuda-driver'])
            self.assertEqual(result['dependency_roles']['torch'], 'allocation only')
            self.assertIn('nvrtc', result['dependency_roles'])
            self.assertFalse(repair_cuda_manifest(root))

    def test_unrelated_kernel_does_not_gain_unused_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'kernel.py').write_text('return x + y')
            path = root / 'solution.json'
            original = json.dumps({'spec': {'dependencies': ['torch']}, 'dependency_roles': {}})
            path.write_text(original)
            self.assertFalse(repair_cuda_manifest(root))
            self.assertEqual(path.read_text(), original)
