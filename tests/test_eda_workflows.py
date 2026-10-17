import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

spec=importlib.util.spec_from_file_location('eda_workflows',Path(__file__).resolve().parents[1]/'examples/openroad_mcp_agent/eda_workflows.py')
eda=importlib.util.module_from_spec(spec);spec.loader.exec_module(eda)

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.flow=self.root/'flow'
        (self.flow/'platforms/nangate45').mkdir(parents=True)
        (self.flow/'platforms/nangate45/config.mk').touch()
        self.service=eda.EdaWorkflows(self.root/'eda',self.flow)
        self.project=self.service.create_eda_project('nangate45',top='counter')['project_id']
        disk=patch.object(eda.shutil,'disk_usage',return_value=Mock(free=100*1024**3))
        disk.start();self.addCleanup(disk.stop)

    def upload(self,path,raw,offset=0,**kwargs):
        return self.service.upload_eda_file(self.project,path,base64.b64encode(raw).decode(),offset,**kwargs)

    def test_chunk_retries_and_hash_and_path_boundaries(self):
        self.upload('rtl/counter.v',b'module ')
        self.upload('rtl/counter.v',b'module ')
        result=self.upload('rtl/counter.v',b'counter; endmodule',7,sha256=hashlib.sha256(b'module counter; endmodule').hexdigest())
        self.assertEqual(result['next_offset'],25)
        for path in ('../escape.v','/absolute.v','rtl/../../escape.v','config.mk','x\\evil.v'):
            with self.subTest(path=path),self.assertRaises(ValueError):self.upload(path,b'bad')
        with self.assertRaises(ValueError):self.upload('rtl/counter.v',b'changed')
        with self.assertRaises(ValueError):self.upload('rtl/counter.v',b'gap',100)
        with self.assertRaises(ValueError):self.upload('rtl/counter.v',b'',25,sha256='0'*64)

    def test_submission_requires_inputs_and_snapshots_and_deduplicates(self):
        with self.assertRaises(ValueError):self.service.run_eda_flow(self.project,'retry-key')
        self.upload('a.v',b'module counter; endmodule')
        self.upload('a.sdc',b'create_clock -period 10 [get_ports clk]')
        with patch.object(eda.threading.Thread,'start'):
            job=self.service.run_eda_flow(self.project,'retry-key')
        repeated=self.service.run_eda_flow(self.project,'retry-key')
        self.assertEqual(job['job_id'],repeated['job_id']);self.assertTrue(repeated['reused'])
        with self.assertRaises(ValueError):self.service.run_eda_flow(self.project,'retry-key',through='synth')
        with self.assertRaises(ValueError):self.service.run_eda_flow(self.project,'different-key')
        path=self.service.directory('jobs',job['job_id'])
        self.assertEqual((path/'inputs/a.v').read_bytes(),b'module counter; endmodule')
        # Restart must not replay a submitted job.
        recovered=eda.EdaWorkflows(self.root/'eda',self.flow)
        state=recovered.get_eda_job(job['job_id'])
        self.assertEqual(state['status'],'interrupted')
        self.assertTrue(state['artifacts_ready'])

    def test_binary_artifact_download_and_zip_are_lossless(self):
        jid='a'*32;directory=self.root/'eda/jobs'/jid;directory.mkdir()
        eda.dump(directory/'state.json',{'status':'succeeded','artifacts_ready':False})
        (directory/'work/results').mkdir(parents=True)
        data=bytes(range(256))*1000
        (directory/'work/results/6_final.gds').write_bytes(data)
        self.service._artifacts(directory)
        listing=self.service.list_eda_artifacts(jid,limit=1)
        self.assertFalse(listing['eof'])
        artifact=listing['artifacts'][0];chunks=[];offset=0
        while True:
            page=self.service.read_eda_artifact(jid,artifact['artifact_id'],offset,40000)
            chunks.append(base64.b64decode(page['data_base64']));offset=page['next_offset']
            if page['eof']:break
        self.assertEqual(b''.join(chunks),data)
        self.assertEqual(hashlib.sha256(b''.join(chunks)).hexdigest(),artifact['sha256'])
        self.assertEqual(self.service.list_eda_artifacts(jid,1)['artifacts'][0]['path'],'artifacts.zip')
        with self.assertRaises(ValueError):self.service.read_eda_artifact(jid,'../state.json')

    def test_tool_schemas_fit_sdk_registration(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
        from nexus_agent import McpToolDescriptor
        for tool in eda.TOOLS:
            McpToolDescriptor(name=tool['name'],description=tool['description'],input_schema=tool['inputSchema'])
        with self.assertRaises(ValueError):self.service.call('run_eda_flow',{'project_id':self.project,'request_id':'x','shell':'rm'})

if __name__=='__main__':unittest.main()
