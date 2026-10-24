"""Regression checks for real HTTP streaming and Relay cancellation semantics."""
import http.client
import io
import ast
import tempfile
import json
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from model_transport import Response
from runtime import check_control, Runtime, maintenance_restore


class RuntimeContractTests(unittest.TestCase):
    def test_only_fresh_maintenance_snapshots_are_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            self.assertIsNone(maintenance_restore(path))
            (path/'.workspace-restore').mkdir()
            marker=path/'.maintenance-restore.json'
            marker.write_text(json.dumps({'expires_at':time.time()+300,'turns':3}))
            self.assertEqual(maintenance_restore(path)['turns'],3)
            marker.write_text(json.dumps({'expires_at':time.time()-1,'turns':3}))
            self.assertIsNone(maintenance_restore(path))

    def test_published_tool_descriptors_satisfy_sdk_contract(self):
        from nexus_agent import McpToolDescriptor
        tree=ast.parse(Path(__file__).with_name('agent.py').read_text())
        assignment=next(n for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='tools' for t in n.targets))
        descriptors=eval(compile(ast.Expression(assignment.value),'agent-tools','eval'),{'McpToolDescriptor':McpToolDescriptor})
        self.assertEqual([d.name for d in descriptors],['pi_chat','authorize_pi_model','pi_status'])

    def test_real_http_response_can_be_read_as_sse_lines(self):
        body = b'data: {"choices":[]}\n\ndata: [DONE]\n\n'
        sock = Mock()
        sock.makefile.return_value = io.BytesIO(
            b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: '
            + str(len(body)).encode() + b'\r\n\r\n' + body)
        http_response = http.client.HTTPResponse(sock)
        http_response.begin()
        connection = Mock()
        with Response(connection, http_response) as response:
            received = b''.join(iter(lambda: response.readline(65537), b''))
        self.assertEqual(received, body)
        connection.close.assert_called_once()

    def test_relay_without_recovery_still_obeys_cancellation(self):
        for state in ({'managed':False,'cancel_requested':True},
                      {'managed':False,'state':'cancelled'},
                      {'managed':False,'remaining_seconds':0}):
            ctx = Mock()
            ctx.control.return_value = state
            with self.assertRaisesRegex(ValueError,'RUN_CANCELLED'):
                check_control(ctx)

    def test_ordinary_relay_and_local_context_can_proceed(self):
        for state in ({'managed':False}, {'managed':False,'state':'running','remaining_seconds':600}):
            ctx = Mock()
            ctx.control.return_value = state
            check_control(ctx)
            ctx.raise_if_cancelled.assert_called_once()

    def test_approval_reference_survives_provider_selection_but_not_other_caller(self):
        runtime = Runtime.__new__(Runtime)
        runtime.authorizations = {}
        ctx = Mock()
        ctx.run_id = '00000000-0000-0000-0000-000000000001'
        ctx.caller_identity.return_value = {'owner_scope':'alice','quota_scope':'alice'}
        credentials = Mock()
        credentials.authorize.return_value = {'status':'pending','authorization_id':'approval-a','provider':'nexus'}
        credentials.status.return_value = {'status':'pending','authorization_id':'approval-a','provider':'custom_openai'}
        with patch('runtime.ProviderCredentials',return_value=credentials):
            runtime.chat({'message':'hello'},ctx)
            selected = runtime.chat({'message':'continue'},ctx)
            self.assertEqual(selected['authorization']['provider'],'custom_openai')
            credentials.authorize.assert_called_once_with('nexus',0,reuse_existing=True)
            credentials.status.assert_called_once_with('approval-a')
            ctx.caller_identity.return_value = {'owner_scope':'bob','quota_scope':'bob'}
            runtime.chat({'message':'hello'},ctx)
            self.assertEqual(credentials.authorize.call_count,2)

    def test_new_conversation_requests_cloud_scoped_reuse_without_default_model(self):
        runtime=Runtime.__new__(Runtime)
        runtime.authorizations={}
        ctx=Mock()
        ctx.caller_identity.return_value={'owner_scope':'alice','quota_scope':'alice'}
        credentials=Mock()
        credentials.authorize.return_value={'status':'pending','authorization_id':'new-approval'}
        with patch('runtime.ProviderCredentials',return_value=credentials):
            for rid in ('00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002'):
                ctx.run_id=rid
                self.assertEqual(runtime.chat({'message':'hello'},ctx)['status'],'authorization_required')
        self.assertEqual(credentials.authorize.call_count,2)
        for args in credentials.authorize.call_args_list:
            self.assertEqual(args.kwargs,{'reuse_existing':True})
        credentials.status.assert_not_called()
        credentials.resolve_for_runtime.assert_not_called()


if __name__ == '__main__':
    unittest.main()
