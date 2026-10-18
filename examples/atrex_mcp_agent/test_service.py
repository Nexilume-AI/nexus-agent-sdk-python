import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from service import Service, ToolError, catalog


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.upstream = root / 'upstream'
        (self.upstream / 'docs').mkdir(parents=True)
        (self.upstream / 'README.md').write_text('one\ntwo\nthree\n')
        self.service = Service(root / 'data', self.upstream)
        created = self.service.call('create_atrex_project', {'name':'test'})
        self.ref = {k:created[k] for k in ('project_id','access_key')}

    def test_metadata(self):
        from nexus_agent import McpToolDescriptor
        tools = catalog()
        self.assertEqual(len({t['name'] for t in tools}),15)
        for t in tools:
            McpToolDescriptor(name=t['name'],description=t['description'],input_schema=t['inputSchema'])

    def test_access_key_and_path(self):
        for args in ({**self.ref,'access_key':'wrong'}, {**self.ref,'project_id':'../escape'}):
            with self.assertRaises(ToolError): self.service.call('get_atrex_project',args)
        for name in ('../escape.py','/tmp/a.py','a/b.py','a\\b.py','.env','project.json/../a.py'):
            with self.assertRaises(ToolError): self.service.call('upload_atrex_file',{**self.ref,'filename':name,'content':'x'})
        self.assertNotIn('access_hash',self.service.call('get_atrex_project',self.ref))

    def test_download_pages_and_restart(self):
        content = ('中文 kernel\n'*8000).encode()
        text, offset = content.decode(), 0
        for i in range(0,len(text),1024):
            args={**self.ref,'filename':'reference.py','content':text[i:i+1024],'offset':offset}
            result=self.service.call('upload_atrex_file',args)
            self.assertEqual(self.service.call('upload_atrex_file',args),result)
            offset=result['next_offset']
        with self.assertRaises(ToolError):
            self.service.call('upload_atrex_file',{**self.ref,'filename':'reference.py','content':'different','offset':1})
        restarted = Service(self.service.root,self.upstream)
        offset, result = 0,b''
        while True:
            page = restarted.call('read_atrex_file',{**self.ref,'filename':'reference.py','offset':offset,'limit':999})
            result += base64.b64decode(page['data'])
            offset=page['next_offset']
            if page['eof']: break
        self.assertEqual(result,content)
        self.assertEqual(page['sha256'],hashlib.sha256(content).hexdigest())

    def test_explicit_overwrite_and_limits(self):
        args={**self.ref,'filename':'reference.py','content':'old'}
        self.service.call('upload_atrex_file',args)
        with self.assertRaises(ToolError): self.service.call('upload_atrex_file',{**args,'content':'new'})
        self.service.call('upload_atrex_file',{**args,'content':'new','overwrite':True})
        self.service.call('upload_atrex_file',{**args,'content':'n','overwrite':True,'offset':0})
        self.assertEqual(self.service.call('get_atrex_project',self.ref)['files'][0]['bytes'],1)
        with self.assertRaises(ToolError): self.service.call('upload_atrex_file',{**args,'content':'a'*262145,'overwrite':True})
        for offset in (-1,True,999):
            with self.assertRaises(ToolError): self.service.call('read_atrex_file',{**self.ref,'filename':'reference.py','offset':offset})

    def test_validation_does_not_execute_uploaded_python(self):
        inputs={'reference.py':'raise RuntimeError("never execute")','definition.json':'{}','workload.jsonl':'{}\n'}
        for name,content in inputs.items():
            self.service.call('upload_atrex_file',{**self.ref,'filename':name,'content':content})
        result=self.service.call('validate_atrex_project',self.ref)
        self.assertTrue(result['static_valid'])
        self.assertFalse(result['code_executed'])
        self.assertFalse(result['optimization_ready'])
        self.service.call('upload_atrex_file',{**self.ref,'filename':'definition.json','content':'{','overwrite':True})
        self.assertFalse(self.service.call('validate_atrex_project',self.ref)['static_valid'])

    def test_execution_is_disabled(self):
        with self.assertRaisesRegex(ToolError,'Optimization is disabled'):
            self.service.call('start_atrex_optimization',self.ref)
        plan=self.service.call('prepare_atrex_optimization',self.ref)
        self.assertIn('GPU_RUNNER_NOT_CONFIGURED',plan['blockers'])
        self.assertFalse(self.service.call('get_atrex_status',{})['execution_enabled'])

    def test_document_allowlist_and_paging(self):
        page=self.service.call('read_atrex_doc',{'path':'README.md','limit':2})
        self.assertEqual(page['text'],'one\ntwo')
        self.assertFalse(page['eof'])
        with self.assertRaises(ToolError): self.service.call('read_atrex_doc',{'path':'../data/project.json'})


if __name__ == '__main__': unittest.main()
