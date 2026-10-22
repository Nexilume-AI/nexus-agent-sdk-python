import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from nexus_agent.interactive_views import InteractiveView, InteractiveViews, InteractiveViewError, _NoRedirect


class InteractiveViewTests(unittest.TestCase):
    def setUp(self):
        self.context = SimpleNamespace(interaction_url='https://cloud.test/api/run/interactions/', _cloud_opener=None)
        self.info = {'view_id':'00000000-0000-0000-0000-000000000001', 'url':'https://cloud.test/view/#ticket=browser',
                     '_runtime':{'url':'https://cloud.test/view/runtime/','token':'runtime-secret'}}

    def test_no_runtime_capability_in_result_or_repr_and_origin_check(self):
        view = InteractiveView(self.context, self.info, {})
        self.assertNotIn('_runtime',view.public_info)
        self.assertNotIn('runtime-secret',repr(view));self.assertNotIn('ticket=',repr(view))
        with self.assertRaises(InteractiveViewError):
            InteractiveView(self.context,{**self.info,'_runtime':{'url':'https://other.test/','token':'secret'}},{})
        with self.assertRaises(InteractiveViewError):
            _NoRedirect().redirect_request(None,None,302,'',{},'https://other.test/')

    def test_every_action_requires_handler(self):
        with self.assertRaises(ValueError):
            InteractiveViews(self.context).create(title='Test',actions={'run':{}},handlers={})

    def test_replayed_claim_does_not_execute_again(self):
        handler=Mock(return_value={'ok':True});view=InteractiveView(self.context,self.info,{'run':handler})
        view._request=Mock(side_effect=[{'claimed':True},{'status':'succeeded'},{'claimed':False}])
        action={'request_id':'one','name':'run','arguments':{}}
        view._dispatch(action);view._dispatch(action)
        handler.assert_called_once_with({},view)

    def test_ambiguous_claim_failure_never_executes_handler(self):
        handler=Mock();view=InteractiveView(self.context,self.info,{'run':handler})
        view._request=Mock(side_effect=OSError('connection lost'))
        with self.assertRaises(OSError):view._dispatch({'request_id':'one','name':'run','arguments':{}})
        handler.assert_not_called()

    def test_completion_retry_does_not_repeat_handler_and_hides_exceptions(self):
        handler=Mock(side_effect=ValueError('secret api key'))
        view=InteractiveView(self.context,self.info,{'run':handler})
        view._request=Mock(side_effect=[{'claimed':True},OSError('lost'),{'status':'failed'}])
        with self.assertRaises(OSError):view._dispatch({'request_id':'one','name':'run','arguments':{}})
        self.assertNotIn('secret api key',str(view._pending_completions))
        view._complete('one');handler.assert_called_once();self.assertFalse(view._pending_completions)

    def test_update_retries_only_revision_conflicts(self):
        view=InteractiveView(self.context,self.info,{})
        view._request=Mock(side_effect=[{'revision':2},HTTPError('',409,'conflict',{},None),{'revision':3},{'revision':4}])
        self.assertEqual(view.set_state({'count':1}),4)


if __name__=='__main__':unittest.main()
