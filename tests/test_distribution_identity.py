"""The public package rename must include installation and service entry points."""
import ast
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DistributionIdentityTests(unittest.TestCase):
    def test_version_and_public_identity(self):
        import nexus_agent
        self.assertEqual(nexus_agent.__version__, '0.47.1')
        text = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
        self.assertIn('name = "nexilume"', text)
        self.assertIn('version = "0.47.1"', text)
        self.assertIn('LicenseRef-Nexus-Additional-Terms-1.0', text)
        owners = re.findall(r'https://github\.com/([^/\s"]+)/', text)
        self.assertTrue(owners, 'Public repository links must be present')
        self.assertEqual(set(owners), {'Nexilume-AI'})

    def test_runtime_update_and_windows_service_target_public_distribution(self):
        for name, expected in (
            ('computer_runtime.py', 'nexilume[computer,browser]'),
            ('windows_service.py', 'nexilume'),
        ):
            tree = ast.parse((ROOT / 'src/nexus_agent' / name).read_text(encoding='utf-8'))
            values = [node.value for node in ast.walk(tree)
                      if isinstance(node, ast.Constant) and isinstance(node.value, str)]
            self.assertIn(expected, values)
            self.assertFalse(any('nexus-openwrt-agent-sdk' in value for value in values))

    def test_legal_documents_are_in_source_manifest(self):
        manifest = (ROOT / 'MANIFEST.in').read_text(encoding='utf-8')
        for name in ('LICENSE', 'LICENSING.md', 'CONTRIBUTOR_LICENSE_AGREEMENT.md'):
            self.assertIn(name, manifest)
            self.assertTrue((ROOT / name).is_file())
