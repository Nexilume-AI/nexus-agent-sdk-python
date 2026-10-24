"""Managed Mobile IDs must survive retry/recovery and pass the UUID wire type."""
import pathlib
import sys
import unittest
import uuid
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))
from nexus_agent.reporting import NexusRunContext, _NexusMobileTransportUnavailable


class ManagedMobileIds(unittest.TestCase):
    def invoke(self, key, accepted, *, lose_first_response=False):
        context = NexusRunContext(run_id='fixture-run', mobile_enabled=True,
            mobile_delegate_url='https://mobile.invalid/', mobile_delegate_token='fixture',
            mobile_capabilities=('mobile.observe',), recovery_url='https://cloud.invalid/operations/',
            interaction_token='fixture', recovery_managed=True)
        calls = []
        def request(path, *, method, payload):
            identifier = str(uuid.UUID(payload['client_request_id']))
            calls.append(identifier)
            # Model the API's UUID validation and same-ID admission semantics.
            accepted.setdefault(identifier, {'id':identifier, 'status':'succeeded', 'result':{'packageName':'fixture'}})
            if lose_first_response and len(calls)==1:
                raise _NexusMobileTransportUnavailable('lost response')
            return accepted[identifier]
        with mock.patch.object(context.recovery, '_prepare', return_value={
            'id':'fixture-operation','execute':True,'idempotency_key':key}), \
            mock.patch.object(context.recovery, '_finish'), \
            mock.patch.object(context, '_mobile_request', side_effect=request), \
            mock.patch('nexus_agent.reporting.time.sleep'):
            result=context.mobile.observe(timeout=30)
        self.assertEqual(result.data, {'packageName':'fixture'})
        return calls

    def test_opaque_journal_key_has_one_uuid_across_lost_response_and_fresh_context(self):
        accepted={}
        first=self.invoke('nxo_'+'a'*64,accepted,lose_first_response=True)
        recovered=self.invoke('nxo_'+'a'*64,accepted)
        self.assertEqual(len(first),2)
        self.assertEqual(first[0],first[1])
        self.assertEqual(first[0],recovered[0])
        self.assertEqual(len(accepted),1)

    def test_distinct_operation_keys_do_not_share_a_command(self):
        accepted={}
        first=self.invoke('nxo_'+'a'*64,accepted)
        second=self.invoke('nxo_'+'b'*64,accepted)
        self.assertNotEqual(first[0],second[0])
        self.assertEqual(len(accepted),2)


if __name__=='__main__':unittest.main()
