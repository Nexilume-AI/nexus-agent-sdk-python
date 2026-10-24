import json
import unittest
from unittest.mock import Mock
from browser_tools import invoke, descriptors
from nexus_agent.browser import NexusBrowserObservation, NexusBrowserDOMSnapshot, NexusBrowserNode


class BrowserToolsTests(unittest.TestCase):
    def context(self):
        ctx = Mock(browser_enabled=True, workspace_capabilities={'browser.control'})
        obs = NexusBrowserObservation('one', 3, 'https://example.com/', 'Example', (1280, 720), b'image',
            dom=NexusBrowserDOMSnapshot(3, (NexusBrowserNode('e1', 'a', 'link', 'More', 'More'),)))
        session = ctx.browser.attached_session.return_value
        session.open.return_value = session.observe.return_value = session.perform.return_value = obs
        return ctx

    def test_search_and_revision_bound_actions(self):
        ctx = self.context()
        result = invoke(ctx, 'computer_browser_search', {'query': '中文 & example'})
        target = ctx.browser.attached_session.return_value.open.call_args.args[0]
        self.assertIn('%26', target)
        self.assertEqual(json.loads(result['content'][0]['text'])['revision'], 3)
        invoke(ctx, 'computer_browser_click', {'ref': 'e1', 'revision': 3})
        action = ctx.browser.attached_session.return_value.perform.call_args.args[0]
        self.assertEqual(action.expected_revision, 3)
        self.assertEqual(action.parameters['target'], 'e1')

    def test_permissions_and_untrusted_target_arguments(self):
        ctx = self.context()
        for args in ({'ref': 'body', 'revision': 3}, {'ref': 'e1', 'revision': True}, {'ref': 'e1', 'revision': 3, 'connection_id': 'other'}):
            with self.assertRaises(ValueError): invoke(ctx, 'computer_browser_click', args)
        ctx.browser_enabled = False
        self.assertEqual(descriptors(ctx), [])
        with self.assertRaises(ValueError): invoke(ctx, 'computer_browser_observe', {})
        ctx.browser.attached_session.assert_not_called()


if __name__ == '__main__': unittest.main()
