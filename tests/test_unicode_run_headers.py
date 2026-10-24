import json
import pathlib
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))
from nexus_agent.reporting import NexusRunContext, NexusComputerError


class UnicodeRunHeadersTest(unittest.TestCase):
    def test_json_companions_preserve_paths_and_name(self):
        expected={'Workspace-Root': 'D:/实验/工作区', 'Output-Root': 'D:/实验/结果', 'Browser-Computer-Name': '测试电脑'}
        headers={'x-nexus-'+k.lower()+'-json': json.dumps(v, ensure_ascii=True) for k,v in expected.items()}
        with patch.dict('os.environ', {}, clear=True):
            context=NexusRunContext.from_env(headers=headers)
        self.assertEqual(context.workspace_root, expected['Workspace-Root'])
        self.assertEqual(context.output_root, expected['Output-Root'])
        self.assertEqual(context.browser_computer_name, expected['Browser-Computer-Name'])
        context.close()

    def test_ascii_fallback_and_malformed_companions(self):
        with patch.dict('os.environ', {}, clear=True):
            context=NexusRunContext.from_env(headers={'X-Nexus-Workspace-Root': '/legacy'})
            self.assertEqual(context.workspace_root, '/legacy'); context.close()
            for value in ('not json', '{}', 'null', '42'):
                with self.subTest(value=value), self.assertRaises(NexusComputerError):
                    NexusRunContext.from_env(headers={'X-Nexus-Workspace-Root-Json': value})

if __name__=='__main__':unittest.main()
