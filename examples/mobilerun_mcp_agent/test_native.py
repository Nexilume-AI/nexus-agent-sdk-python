"""Deterministic native-engine contract test; not a real-phone acceptance."""
import asyncio,http.server,io,json,threading,unittest,uuid
from types import SimpleNamespace
from unittest.mock import Mock,patch
from PIL import Image
import runtime
from test_driver import context


class FakeModel:
    def __init__(self,ctx,authorization,maximum,*args):
        self.model_seconds=0;self.failed_requests=0;self.efforts_used=[];self.request_diagnostics=[];self.calls=0;self.grant={'model':'test-vision'};self.nonce='test-nonce'
        parent=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert data['model']=='test-vision'
                parent.calls+=1
                tool=('<invoke name="click_at"><parameter name="x">40</parameter><parameter name="y">80</parameter></invoke>' if parent.calls==1 else '<invoke name="complete"><parameter name="success">true</parameter><parameter name="message">NATIVE_TEST_PASSED</parameter></invoke>')
                body=json.dumps({'id':'test','object':'chat.completion','created':0,'model':'test-vision','choices':[{'index':0,'message':{'role':'assistant','content':'I will execute the test action.\n<function_calls>'+tool+'</function_calls>'},'finish_reason':'stop'}],'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.url=f'http://127.0.0.1:{self.server.server_port}/v1'
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
    def close(self):self.server.shutdown();self.server.server_close()


class NativeEngineTest(unittest.TestCase):
    def test_permanent_bridge_error_does_not_regenerate_model_request(self):
        from model_broker import Handler
        from llama_index.llms.openai_like import OpenAILike
        from llama_index.core.base.llms.types import ChatMessage
        from mobilerun.agent.utils.inference import acall_with_retries
        from openai import UnprocessableEntityError
        broker=SimpleNamespace(nonce='test-nonce',request=Mock(side_effect=ValueError('MODEL_RESPONSE_LIMIT')))
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);server.broker=broker
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            llm=OpenAILike(model='test',api_base=f'http://127.0.0.1:{server.server_port}/v1',api_key=broker.nonce,
                is_chat_model=True,max_retries=0,timeout=5)
            with self.assertRaises(UnprocessableEntityError):
                asyncio.run(acall_with_retries(llm,[ChatMessage(role='user',content='test')],retries=3,delay=0))
            broker.request.assert_called_once()
        finally:
            server.shutdown();server.server_close();worker.join(2)

    def test_native_model_loop_and_nexus_driver(self):
        ctx=context();ctx.run_id=str(uuid.uuid4());ctx.chat=Mock();ctx.display=Mock()
        output=io.BytesIO();Image.new('RGB',(400,800),'white').save(output,format='PNG')
        ctx.mobile.capture_screen.return_value=SimpleNamespace(content=output.getvalue(),content_type='image/png',width=400,height=800)
        service=runtime.Runtime();service.authorize=lambda *_:{'status':'authorized','authorization_id':str(uuid.uuid4())}
        with patch.object(runtime,'ModelBroker',FakeModel):
            result=service.run({'message':'Run the deterministic test','max_steps':3},ctx)
        self.assertTrue(result['success'],result)
        self.assertEqual(result['model_requests'],2)
        self.assertEqual(result['device_actions'],1)
        self.assertEqual(result['screenshots'],3)  # Two decision frames plus one final frame.
        ctx.mobile.tap.assert_called_once()
        print('NATIVE_ENGINE_CONTRACT_PASSED (simulated phone/model only)')


if __name__=='__main__':unittest.main()
