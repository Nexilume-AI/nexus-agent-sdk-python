import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from nexus_agent.credentials import ProviderCredentials

class CredentialsTests(unittest.TestCase):
    def test_request_contains_no_secret_and_resolution_is_separate(self):
        aid='12345678-1234-1234-1234-123456789abc'
        context=SimpleNamespace(interaction_url='https://cloud.test/callback/',_interaction_request=Mock(return_value={'authorization_id':aid,'status':'pending'}))
        client=ProviderCredentials(context)
        result=client.authorize('anthropic',0)
        context._interaction_request.assert_called_once_with('credentials/',method='POST',payload={'provider':'anthropic'})
        self.assertNotIn('secret',result)
        client.resolve_for_runtime(aid)
        self.assertEqual(context._interaction_request.call_args.kwargs['method'],'POST')
    def test_no_insecure_or_unbounded_requests(self):
        context=SimpleNamespace(interaction_url='http://cloud.test/',_interaction_request=Mock())
        client=ProviderCredentials(context)
        with self.assertRaises(RuntimeError):client.authorize('openai',0)
        with self.assertRaises(ValueError):client.authorize('openai',21)
        with self.assertRaises(ValueError):client.authorize('unknown',0)
        context._interaction_request.assert_not_called()
