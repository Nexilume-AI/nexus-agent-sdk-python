import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from nexus_agent.credentials import ProviderCredentials

class CredentialsTests(unittest.TestCase):
    def test_config_preauthorization_returns_approved_scope_without_polling(self):
        approved={'status':'authorized','authorization_id':'12345678-1234-1234-1234-123456789abc',
            'preauthorized':True,'provider':'custom_openai','base_url':'https://caller.example/v1','model':'caller-model'}
        context=SimpleNamespace(interaction_url='https://cloud.test/callback/',_interaction_request=Mock(return_value=approved))
        value=ProviderCredentials(context).authorize('nexus',20,model='tool-default',force_new=True)
        self.assertEqual(value,approved)
        self.assertNotIn('secret',value)
        context._interaction_request.assert_called_once_with('credentials/',method='POST',
            payload={'provider':'nexus','model':'tool-default','force_new':True})

    def test_force_new_is_explicit_and_typed(self):
        context=SimpleNamespace(interaction_url='https://cloud.test/callback/',_interaction_request=Mock(return_value={'status':'pending'}))
        client=ProviderCredentials(context)
        client.authorize('nexus',0,force_new=True)
        self.assertEqual(context._interaction_request.call_args.kwargs['payload'],{'provider':'nexus','force_new':True})
        with self.assertRaises(ValueError):client.authorize('nexus',0,force_new='true')
    def test_custom_endpoint_and_model_are_nonsecret_request_metadata(self):
        context=SimpleNamespace(interaction_url='https://cloud.test/callback/',_interaction_request=Mock(return_value={'status':'pending'}))
        ProviderCredentials(context).authorize('custom_openai',0,base_url='https://example.com/v1',model='gpt-6-sol')
        self.assertEqual(context._interaction_request.call_args.kwargs['payload'],{'provider':'custom_openai','base_url':'https://example.com/v1','model':'gpt-6-sol'})
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
