"""No-credential integration test: real worker/optimizer, local model rejection only."""
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import tempfile
import threading
import time
import uuid
from broker import Broker,Server,Handler,TERMINAL,atomic
from service import Service


def main():
    here=Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix='runner-smoke-',dir=here) as temporary:
        root=Path(temporary)
        service=Service(root/'projects',here/'upstream')
        project=service.call('create_atrex_project',{'name':'offline runner smoke'})
        ref={k:project[k] for k in ('project_id','access_key')}
        for source in (here/'sample_vector_add').iterdir():
            text=source.read_text();offset=0
            for i in range(0,len(text),1000):
                uploaded=service.call('upload_atrex_file',{**ref,'filename':source.name,'content':text[i:i+1000],'offset':offset})
                offset=uploaded['next_offset']
        broker=Broker(root/'broker','nexus-atrex-worker:20261018',root/'projects')
        preflight=json.loads((here/'broker-state/gpu-preflight.json').read_text())
        atomic(broker.root/'gpu-health.json',preflight)
        authorization=str(uuid.uuid4())
        broker.grant_status=lambda _: {'status':'authorized','provider':'nexus','authorization_id':authorization,'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()}
        called=threading.Event()
        def reject_model(handler,payload):
            assert payload['model']=='gpt-6-sol'
            print('PASS real Codex reached job model proxy '+handler.path,flush=True)
            called.set()
            handler.reply(401,{'error':{'message':'Intentional offline smoke-test rejection','type':'authentication_error'}})
        broker.model=reject_model
        socket_path=broker.root/'proxy/model.sock';socket_path.parent.mkdir()
        server=Server(str(socket_path),Handler);server.is_model=True;server.broker=broker
        socket_path.chmod(0o600)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        jid=None
        try:
            record=broker.submit({**ref,'request_id':str(uuid.uuid4()),'authorization_id':authorization,
                'provider':'nexus','model':'gpt-6-sol','max_iters':1,'timeout_seconds':240,
                'credentials':{'provider':'nexus','base_url':'https://cloud.nexilume.com/api/v1/openai/v1','secret':'unused-test-placeholder'}})
            jid=record['job_id'];query={**ref,'job_id':jid};deadline=time.time()+250
            cancelled=False
            while time.time()<deadline:
                record=broker.control('get',query)
                if record['status'] in TERMINAL:break
                if called.is_set() and not cancelled:
                    broker.control('cancel',query);cancelled=True
                time.sleep(2)
            print(json.dumps({k:record.get(k) for k in ('status','error','artifacts')}),flush=True)
            log=broker.root/jid/'optimizer.log'
            if log.exists():
                log_text=log.read_text(errors='replace')
                print(log_text[-14000:],flush=True)
                assert 'python: command not found' not in log_text
                assert 'runtime_arch=sm_60' in log_text
            assert called.is_set(),'Optimizer did not reach the test model endpoint'
            assert record['status'] in TERMINAL and record['artifacts'],'No terminal artifacts'
            import base64,hashlib
            for item in record['artifacts']:
                data=bytearray();offset=0
                while True:
                    page=broker.control('read',{**query,'path':item['path'],'offset':offset})
                    data.extend(base64.b64decode(page['data']));offset=page['next_offset']
                    if page['eof']:break
                assert hashlib.sha256(data).hexdigest()==item['sha256']
            print('PASS real offline worker startup, model routing, cancellation/failure collection and artifact SHA256; no real provider called',flush=True)
            atomic(here/'broker-state/gpu-health.json',{**preflight,'worker_startup_test_passed':True})
        finally:
            if jid:
                with broker.lock:
                    if jid in broker.live:broker.live[jid]['cancel'].set()
                for _ in range(25):
                    if jid not in broker.live:break
                    time.sleep(2)
                broker.docker('rm','-f',broker.container(jid),check=False)
            server.shutdown()


if __name__=='__main__':main()
