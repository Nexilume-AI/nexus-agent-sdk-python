import unittest
from unittest.mock import patch
from nexus_agent.reporting import NexusRunContext, NexusRunContextUnavailable


class CallerIdentityTests(unittest.TestCase):
    def test_verified_callback_identity_only(self):
        context=NexusRunContext()
        identity=dict(version=1,principal_type='api_key',owner_scope='a'*64,quota_scope='b'*64)
        with patch.object(context,'_load_run_context',return_value={'caller_identity':identity}):
            self.assertEqual(context.caller_identity(),identity)
        for value in (None,{},dict(identity,version=True),dict(identity,owner_scope='client-label'),dict(identity,principal_type='anonymous')):
            with patch.object(context,'_load_run_context',return_value={'caller_identity':value}):
                with self.assertRaises(NexusRunContextUnavailable):context.caller_identity()
        with self.assertRaises(NexusRunContextUnavailable):context.caller_identity()
