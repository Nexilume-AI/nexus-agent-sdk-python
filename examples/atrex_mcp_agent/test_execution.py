import io
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch, Mock
import uuid
from broker import Broker, Failure
from service import Service


class ExecutionTests(unittest.TestCase):
    def test_custom_endpoint_and_exact_model_cannot_be_overridden(self):
        self.args.update(provider='custom_openai',base_url='https://api.example/v1')
        self.args['credentials'].update(provider='custom_openai',base_url='https://api.example/v1',model='gpt-6-sol')
        with self.assertRaisesRegex(Failure,'ENDPOINT_NOT_AUTHORIZED'):self.broker.submit({**self.args,'base_url':'https://other.example/v1'})
        with self.assertRaisesRegex(Failure,'MODEL_NOT_AUTHORIZED'):self.broker.submit({**self.args,'model':'other-model'})
        job=self.broker.submit(self.args)
        self.assertEqual(job['spec']['base_url'],'https://api.example/v1')
        self.assertEqual(job['spec']['protocol'],'openai')

    def test_model_transport_blocks_private_and_mixed_dns_before_connection(self):
        import socket
        from model_transport import PinnedHTTPS
        for addresses in (['127.0.0.1'],['93.184.215.14','10.0.0.1']):
            rows=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443)) for ip in addresses]
            with patch('model_transport.socket.getaddrinfo',return_value=rows),patch('model_transport.socket.socket') as connect:
                with self.assertRaisesRegex(ValueError,'NOT_PUBLIC'):PinnedHTTPS('api.example',timeout=1).connect()
                connect.assert_not_called()

    def test_model_transport_pins_ip_and_keeps_tls_hostname(self):
        import socket
        from model_transport import PinnedHTTPS
        rows=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.215.14',443))]
        context=Mock();connection=PinnedHTTPS('api.example',timeout=1,context=context)
        with patch('model_transport.socket.getaddrinfo',return_value=rows) as dns,patch('model_transport.socket.socket') as factory:
            connection.connect()
            dns.assert_called_once()
            factory.return_value.connect.assert_called_once_with(('93.184.215.14',443))
            context.wrap_socket.assert_called_once_with(factory.return_value,server_hostname='api.example')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.service = Service(root/'projects', root/'upstream')
        self.project = self.service.call('create_atrex_project', {'name':'isolation-test'})
        self.ref = {k:self.project[k] for k in ('project_id','access_key')}
        self.service.call('upload_atrex_file',{**self.ref,'filename':'reference.py','content':'# immutable input'})
        for name in ('input.py','shapes.json'):
            self.service.call('upload_atrex_file',{**self.ref,'filename':name,'content':'{}'})
        self.broker = Broker(root/'broker','test-image',root/'projects')
        self.authorization = str(uuid.uuid4())
        self.broker.grant_status = Mock(return_value={'status':'authorized','authorization_id':self.authorization,
            'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
        self.broker.health = Mock(return_value={'execution_enabled':True})
        self.args = {**self.ref,'request_id':str(uuid.uuid4()),'authorization_id':self.authorization,
            'provider':'nexus','model':'gpt-6-sol','credentials':{'provider':'nexus','base_url':'https://cloud.nexilume.com/api/v1/openai/v1',
            'secret':'private-provider-secret','interaction_token':'private-cloud-token'}}
        self.thread = patch('broker.threading.Thread')
        self.thread.start();self.addCleanup(self.thread.stop)
        gpu_probe=patch('broker.subprocess.run',return_value=Mock(returncode=0,stdout='0,0'))
        gpu_probe.start();self.addCleanup(gpu_probe.stop)

    def test_idempotency_snapshot_and_no_persisted_secret(self):
        first = self.broker.submit(self.args)
        self.assertEqual(first, self.broker.submit(self.args))
        self.assertEqual(len(self.broker.live),1)
        for path in self.broker.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'private-provider-secret',path.read_bytes())
                self.assertNotIn(b'private-cloud-token',path.read_bytes())
        self.service.call('upload_atrex_file',{**self.ref,'filename':'reference.py','content':'changed','overwrite':True})
        self.assertEqual((self.broker.root/first['job_id']/'input/files/reference.py').read_text(),'# immutable input')
        with self.assertRaisesRegex(Failure,'IDEMPOTENCY_CONFLICT'):
            self.broker.submit({**self.args,'model':'different'})

    def test_quota_framework_and_authorization(self):
        with self.assertRaisesRegex(Failure,'FRAMEWORK_UNSUPPORTED'):
            self.broker.submit({**self.args,'framework':'Triton'})
        with self.assertRaisesRegex(Failure,'INVALID_PROVIDER'):
            self.broker.submit({**self.args,'provider':'other'})
        with self.assertRaisesRegex(Failure,'INVALID_ARGUMENT'):
            self.broker.submit({**self.args,'timeout_seconds':True})
        self.broker.grant_status.side_effect = Failure('AUTHORIZATION_NOT_ACTIVE')
        with self.assertRaisesRegex(Failure,'AUTHORIZATION_NOT_ACTIVE'): self.broker.submit(self.args)
        self.assertFalse(self.broker.live)

    def test_project_isolation_and_cancel(self):
        job = self.broker.submit(self.args)
        args = {**self.ref,'job_id':job['job_id']}
        with self.assertRaisesRegex(Failure,'PROJECT_NOT_FOUND'):
            self.broker.record({**args,'access_key':'wrong'})
        second = self.service.call('create_atrex_project',{'name':'other'})
        with self.assertRaisesRegex(Failure,'JOB_NOT_FOUND'):
            self.broker.record({**args,'project_id':second['project_id'],'access_key':second['access_key']})
        self.assertTrue(self.broker.control('cancel',args)['cancel_requested'])
        self.assertTrue(self.broker.live[job['job_id']]['cancel'].is_set())

    def test_model_scope_and_revocation_rechecked(self):
        job = self.broker.submit(self.args)
        state = self.broker.live[job['job_id']]
        handler = Mock(headers={'Authorization':'Bearer '+state['capability']},path='/v1/responses')
        with self.assertRaisesRegex(Failure,'MODEL_NOT_AUTHORIZED'):
            self.broker.model(handler,{'model':'wrong'})
        self.broker.grant_status.side_effect=Failure('AUTHORIZATION_NOT_ACTIVE')
        with self.assertRaisesRegex(Failure,'AUTHORIZATION_NOT_ACTIVE'):
            self.broker.model(handler,{'model':'gpt-6-sol'})

    def test_model_streaming_and_secret_confinement(self):
        job=self.broker.submit(self.args)
        state=self.broker.live[job['job_id']]
        handler=Mock(headers={'Authorization':'Bearer '+state['capability']},path='/v1/responses',wfile=io.BytesIO())
        response=Mock(status=200,headers={'Content-Type':'text/event-stream'})
        response.read1.side_effect=[b'data: {"type":"response.created"}\n\n',b'data: {"type":"response.completed"}\n\n',b'']
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        self.broker.model_transport=Mock(return_value=response)
        self.broker.model(handler,{'model':'gpt-6-sol','stream':True,'input':'test'})
        request=self.broker.model_transport.call_args.args[0]
        self.assertEqual(request.full_url,'https://cloud.nexilume.com/api/v1/openai/v1/responses')
        self.assertEqual(request.get_header('Authorization'),'Bearer private-provider-secret')
        self.assertEqual(json.loads(request.data)['max_output_tokens'],8192)
        self.assertNotIn(b'private-provider-secret',handler.wfile.getvalue())
        self.assertEqual(handler.wfile.getvalue().count(b'data:'),2)
        self.assertEqual(self.broker.record({**self.ref,'job_id':job['job_id']})['model_requests'],1)

    def test_nested_lite_tools_cannot_enable_hosted_execution(self):
        job=self.broker.submit(self.args)
        state=self.broker.live[job['job_id']]
        handler=Mock(headers={'Authorization':'Bearer '+state['capability']},path='/v1/responses')
        with self.assertRaisesRegex(Failure,'REMOTE_HOSTED_TOOL_NOT_ALLOWED'):
            self.broker.model(handler,{'model':'gpt-6-sol','input':[{'type':'additional_tools','tools':[
                {'type':'namespace','name':'external','tools':[{'type':'mcp','server_url':'https://invalid'}]}]}]})

    def test_artifact_traversal_links_and_capability_redaction(self):
        job = self.broker.submit(self.args)
        data=io.BytesIO()
        with tarfile.open(fileobj=data,mode='w') as archive:
            for name,content in [('result/kernel.cu',b'kernel'),('../escape',b'bad'),('.codex/auth.json',b'bad'),('result/log.txt',b'job-cap')]:
                member=tarfile.TarInfo(name);member.size=len(content);archive.addfile(member,io.BytesIO(content))
            member=tarfile.TarInfo('link');member.type=tarfile.SYMTYPE;member.linkname='/etc/passwd';archive.addfile(member)
        process=Mock(stdout=io.BytesIO(data.getvalue()))
        with patch('broker.subprocess.Popen',return_value=process):
            artifacts=self.broker.collect(job['job_id'],'job-cap')
        self.assertEqual({x['path'] for x in artifacts},{'artifacts/result/kernel.cu','artifacts/result/log.txt'})
        self.assertEqual((self.broker.root/job['job_id']/'artifacts/result/log.txt').read_bytes(),b'[redacted]')
        self.broker.update(job['job_id'],artifacts=artifacts,status='failed')
        with self.assertRaisesRegex(Failure,'ARTIFACT_NOT_FOUND'):
            self.broker.control('read',{**self.ref,'job_id':job['job_id'],'path':'../job.json'})


if __name__=='__main__':unittest.main()
