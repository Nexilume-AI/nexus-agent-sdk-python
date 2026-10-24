import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from file_bridge import Bridges


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.w={'folder':self.root,'closed':False};self.bridge=Bridges(self.w)
        self.row={'path':'imports/a.safetensors','mode':'upload','size':12,'expires':10**12,'url':'https://cloud.nexilume.com/api/v1/agent-file-bridges/123/runtime/'}
        self.session=Mock();self.session.get.return_value.status_code=200
    def tearDown(self):self.tmp.cleanup()
    def write(self,offset,data):
        self.session.get.return_value.content=data
        return self.bridge.operation(self.row,{'id':'a'*36,'operation':'write','offset':offset,'length':len(data),'sha256':hashlib.sha256(data).hexdigest()},self.session)
    def test_resume_idempotence_and_conflicts(self):
        self.write(0,b'abcd');self.write(0,b'abcd')
        with self.assertRaises(ValueError):self.write(0,b'xxxx')
        with self.assertRaises(ValueError):self.write(8,b'ijkl')
        self.write(4,b'efgh');self.write(8,b'ijkl')
        self.assertEqual((self.root/self.row['path']).read_bytes(),b'abcdefghijkl')
    def test_boundaries_revocation_and_immutable_download(self):
        self.write(0,b'abcd')
        with self.assertRaises(ValueError):self.write(11,b'xx')
        self.row['path']='../escape'
        with self.assertRaises(ValueError):self.write(0,b'abcd')
        self.row['path']='imports/a.safetensors';self.row['mode']='download';self.row['snapshot']=list(self.bridge.snapshot(self.root/self.row['path']))
        cmd={'id':'a'*36,'operation':'read','offset':0,'length':4}
        self.assertEqual(self.bridge.operation(self.row,cmd,self.session),b'abcd')
        (self.root/self.row['path']).write_bytes(b'changed-size')
        with self.assertRaises(ValueError):self.bridge.operation(self.row,cmd,self.session)
        self.w['closed']=True
        with self.assertRaises(ValueError):self.bridge.operation(self.row,cmd,self.session)

    def test_prompt_punctuation_and_runtime_secret_stays_private(self):
        p=self.root/'A boat, steady camera.mp4';p.write_bytes(b'video')
        ctx=Mock();ctx.interaction_url='https://cloud.nexilume.com/api/v1/internal/agent-runs/test/interactions/'
        bid='12345678-1234-1234-1234-123456789012'
        ctx._interaction_request.return_value={'bridge_id':bid,'url':'https://cloud.nexilume.com/api/v1/agent-file-bridges/'+bid+'/#ticket=test',
            'session_expires_in':3600,'_runtime':{'url':'https://cloud.nexilume.com/api/v1/agent-file-bridges/'+bid+'/runtime/','token':'private-worker-secret'}}
        with patch.object(self.bridge,'start'):
            result=self.bridge.create(ctx,p,'download',hashlib.sha256(b'video').hexdigest(),5)
        self.assertEqual(ctx._interaction_request.call_args.kwargs['payload']['name'],'A boat_ steady camera.mp4')
        self.assertNotIn('_runtime',result);self.assertNotIn('private-worker-secret',str(result))
        self.assertTrue(p.exists())

if __name__=='__main__':unittest.main()
