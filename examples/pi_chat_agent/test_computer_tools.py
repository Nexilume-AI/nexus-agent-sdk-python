import json
import unittest
from unittest.mock import Mock
from computer_tools import descriptors, invoke
from nexus_agent.reporting import NexusComputerError


class ComputerToolsTests(unittest.TestCase):
    def context(self):
        ctx = Mock()
        ctx.computer.enabled = True
        ctx.workspace_capabilities = {'files.list', 'files.read', 'files.write', 'command.execute'}
        return ctx

    def test_tools_follow_current_attachment_and_scopes(self):
        ctx = self.context()
        self.assertEqual(len(descriptors(ctx)), 4)
        ctx.workspace_capabilities = {'files.read'}
        self.assertEqual([t['name'] for t in descriptors(ctx)], ['computer_read'])
        with self.assertRaisesRegex(ValueError, 'CAPABILITY_DENIED'):
            invoke(ctx, 'computer_exec', {'command': 'pwd'})
        ctx.computer.enabled = False
        self.assertEqual(descriptors(ctx), [])
        with self.assertRaisesRegex(ValueError, 'NOT_ATTACHED'):
            invoke(ctx, 'computer_read', {'path': 'test.txt'})
        ctx.workspace.read_text.assert_not_called()
        ctx.terminal.run.assert_not_called()

    def test_command_uses_current_context_and_bounded_timeout(self):
        ctx = self.context()
        ctx.terminal.run.return_value = {'stdout': 'LOCAL_COMPUTER', 'exit_code': 0}
        value = invoke(ctx, 'computer_exec', {'command': 'pwd', 'cwd': 'src', 'timeout': 20})
        ctx.terminal.run.assert_called_once_with('pwd', cwd='src', timeout=20)
        self.assertEqual(json.loads(value['content'][0]['text'])['stdout'], 'LOCAL_COMPUTER')
        for timeout in (True, 0, 61, '20'):
            with self.assertRaisesRegex(ValueError, 'INVALID_COMPUTER_ARGUMENTS'):
                invoke(ctx, 'computer_exec', {'command': 'pwd', 'timeout': timeout})

    def test_cannot_choose_another_device_or_supply_credentials(self):
        ctx = self.context()
        for key in ('connection_id', 'url', 'token', 'binding_id'):
            with self.assertRaisesRegex(ValueError, 'INVALID_COMPUTER_ARGUMENTS'):
                invoke(ctx, 'computer_read', {'path': 'file', key: 'other'})
        ctx.workspace.read_text.assert_not_called()

    def test_device_denial_is_not_retried_or_rerouted(self):
        ctx = self.context()
        ctx.workspace.write_text.side_effect = NexusComputerError('Computer unavailable')
        value = invoke(ctx, 'computer_write', {'path': 'file', 'content': ''})
        self.assertTrue(value['isError'])
        ctx.workspace.write_text.assert_called_once_with('file', '')
        ctx.terminal.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
