"""Authorization and path boundaries without launching or charging a job."""
import tempfile
from pathlib import Path
import service
def main():
 with tempfile.TemporaryDirectory() as folder:
  service.DATA=Path(folder)/'data';service.CONTROL=Path(folder)/'control'
  s=service.Service();i={'version':1,'principal_type':'api_key','owner_scope':'a'*64,'quota_scope':'b'*64}
  try:
   assert s.call('create_kernelfoundry_workspace',{})['isError']
   w=s.call('create_kernelfoundry_workspace',{},i)['structuredContent']
   assert not s.call('load_demo_project',w,i)['isError']
   assert s.call('validate_project',w,{**i,'owner_scope':'c'*64})['isError']
   assert s.call('validate_project',{**w,'workspace_key':'wrong'},i)['isError']
   assert s.call('put_project_file',{**w,'name':'../escape','content':'x'},i)['isError']
   assert s.call('put_project_file',{**w,'name':'config.yaml','content':'malicious_target'},i)['isError']
   assert not s.call('close_kernelfoundry_workspace',w,i)['isError']
   assert s.call('validate_project',w,i)['isError']
   print('CALLER_CAPABILITY_PATH_AND_CONFIG_BOUNDARIES_PASSED')
  finally:s.close()
if __name__=='__main__':main()
