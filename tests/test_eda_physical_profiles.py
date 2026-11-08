import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

spec=importlib.util.spec_from_file_location('eda_profiles',Path(__file__).resolve().parents[1]/'examples/openroad_mcp_agent/eda_workflows.py')
eda=importlib.util.module_from_spec(spec);spec.loader.exec_module(eda)

class PhysicalProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();flow=self.root/'flow'
        (flow/'platforms/sky130hd').mkdir(parents=True);(flow/'platforms/sky130hd/config.mk').touch()
        self.service=eda.EdaWorkflows(self.root/'eda',flow)
        self.profile=self.root/'eda/physical_profiles/memory8k';self.profile.mkdir(parents=True)
        (self.profile/'config.mk').write_text('export ADDITIONAL_LEFS = $(NEXUS_PROFILE_DIR)/macro.lef\n')
        (self.profile/'macro.lef').write_text('VERSION 5.4 ;\n')
        self.manifest={'platform':'sky130hd','top':'digital_mcu','files':{p.name:eda.digest(p) for p in self.profile.iterdir()}}
        eda.dump(self.profile/'manifest.json',self.manifest)
        p=patch.object(eda.shutil,'disk_usage',return_value=Mock(free=100*1024**3));p.start();self.addCleanup(p.stop)

    def create(self,**kw):
        return self.service.call('create_eda_project',{'platform':'sky130hd','top':'digital_mcu','physical_profile':'memory8k',**kw})

    def ready(self,project):
        for n,b in [('top.v',b'module digital_mcu; endmodule'),('clock.sdc',b'create_clock -period 14 [get_ports clk]')]:
            self.service.upload_eda_file(project['project_id'],n,base64.b64encode(b).decode())

    def test_public_dispatch_and_immutable_job_snapshot(self):
        p=self.create();self.ready(p)
        with patch.object(eda.threading.Thread,'start'):
            job=self.service.run_eda_flow(p['project_id'],'test')
        out=self.service.directory('jobs',job['job_id'])
        self.assertEqual((out/'profile/macro.lef').read_bytes(),(self.profile/'macro.lef').read_bytes())
        (self.profile/'macro.lef').write_text('later revision')
        self.assertEqual((out/'profile/macro.lef').read_text(),'VERSION 5.4 ;\n')
        self.assertIn('/profile/config.mk',(out/'config.mk').read_text())
        self.service._artifacts(out)
        paths=[a['path'] for a in self.service.list_eda_artifacts(job['job_id'],limit=100)['artifacts']]
        self.assertIn('profile/manifest.json',paths);self.assertIn('profile/macro.lef',paths)

    def test_changed_content_and_revision_refused(self):
        p=self.create();self.ready(p)
        (self.profile/'macro.lef').write_text('changed')
        with self.assertRaisesRegex(ValueError,'content changed'):
            self.service.run_eda_flow(p['project_id'],'test')
        self.manifest['files']['macro.lef']=eda.digest(self.profile/'macro.lef')
        eda.dump(self.profile/'manifest.json',self.manifest)
        with self.assertRaisesRegex(ValueError,'revision changed'):
            self.service.run_eda_flow(p['project_id'],'test')
        self.assertEqual(list((self.root/'eda/jobs').iterdir()),[])

    def test_no_path_injection_or_wrong_platform_top(self):
        for name in ['../memory8k','memory8k;evil','memory8k\ninclude x',False]:
            with self.subTest(name=name),self.assertRaises(ValueError):self.create(physical_profile=name)
        with self.assertRaisesRegex(ValueError,'mismatch'):self.create(top='different_top')
        self.manifest['files']['../secret']='0'*64;eda.dump(self.profile/'manifest.json',self.manifest)
        with self.assertRaisesRegex(ValueError,'filename'):self.create()

if __name__=='__main__':unittest.main()
