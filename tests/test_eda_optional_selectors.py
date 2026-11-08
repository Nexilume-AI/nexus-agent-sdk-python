"""Exercise the actual public dispatchers with blank optional MCP selectors."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

ROOT=Path(__file__).resolve().parents[1]/'examples'
def load(name,file):
 spec=importlib.util.spec_from_file_location(name,file);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
eda=load('eda_optional_workflows',ROOT/'openroad_mcp_agent/eda_workflows.py')
sys.path.insert(0,str(ROOT/'verisure_mcp_agent'))
verisure=load('eda_optional_verisure',ROOT/'verisure_mcp_agent/service.py')

class OptionalSelectorTests(unittest.TestCase):
 def test_custom_and_bundled_keep_mutual_exclusion(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);flow=root/'flow'
   for p in [flow/'platforms/sky130hd/config.mk',flow/'designs/sky130hd/gcd/config.mk']:
    p.parent.mkdir(parents=True,exist_ok=True);p.write_text('')
   service=eda.EdaWorkflows(root/'work',flow)
   for blank in ['', '  ', None]:
    custom=service.call('create_eda_project',{'platform':'sky130hd','top':'timer_apb','design':blank})
    self.assertEqual(custom['top'],'timer_apb');self.assertIsNone(custom['design'])
    bundled=service.call('create_eda_project',{'platform':'sky130hd','top':blank,'design':'gcd'})
    self.assertEqual(bundled['design'],'gcd');self.assertIsNone(bundled['top'])
   for extra in [{'top':'timer_apb','design':'gcd'},{'top':'','design':''},{'top':'../timer','design':''},{'top':'timer_apb','design':False}]:
    with self.assertRaises(ValueError):service.call('create_eda_project',{'platform':'sky130hd',**extra})
 def test_verisure_empty_selectors_default_without_relaxing_paths(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);control=root/'control';control.mkdir();(root/'runtime-ready.json').write_text('{}')
   for blank in ['', ' ', None]:
    folder=root/uuid.uuid4().hex;(folder/'inputs').mkdir(parents=True)
    for name in ['rtl.sv','tb.sv']:(folder/'inputs'/name).write_text('module tb; endmodule')
    service=object.__new__(verisure.Service);service.pending=[];service.active=None;service.live={};service.deliveries={};service.wake=threading.Event();service.save_job=lambda j:None
    w={'folder':folder,'jobs':{},'quota':uuid.uuid4().hex}
    args={'mode':'simulate','request_id':str(uuid.uuid4()),'top':blank,'rtl_path':blank}
    with patch.object(verisure,'ROOT',root),patch.object(verisure,'CONTROL',control):
     result=service.start(w,args,None)
     request=json.loads((folder/'jobs'/result['job_id']/'work/request.json').read_text())
     self.assertEqual(request['top'],'tb');self.assertEqual(request['rtl_path'],'rtl.sv')
     for bad in ['../rtl.sv','/etc/passwd',False]:
      with self.assertRaises(ValueError):service.start(w,{**args,'request_id':str(uuid.uuid4()),'rtl_path':bad},None)

if __name__=='__main__':unittest.main()
