import asyncio,io,json,threading,time,unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from test_driver import context
from driver import NexusMobileDriver
from console import MobileConsole
from model_broker import ModelBroker

class PerformanceTests(unittest.TestCase):
    def test_failed_capture_is_included_in_latency(self):
        ctx=context();driver=NexusMobileDriver(ctx)
        def fail(**kwargs):time.sleep(.01);raise TimeoutError('test')
        ctx.mobile.capture_screen.side_effect=fail
        with self.assertRaises(TimeoutError):asyncio.run(driver.screenshot())
        self.assertEqual(driver.capture_attempts,1)
        self.assertEqual(driver.failed_captures,1)
        self.assertGreaterEqual(driver.capture_seconds,.01)
        self.assertIsNone(driver._state_image)

    def test_state_reuses_once_and_action_invalidates(self):
        ctx=context();d=NexusMobileDriver(ctx)
        async def run():
            first=await d.screenshot()
            self.assertEqual(first,await d.consume_state_screenshot())
            self.assertEqual(ctx.mobile.capture_screen.call_count,1)
            await d.consume_state_screenshot()
            self.assertEqual(ctx.mobile.capture_screen.call_count,2)
            await d.screenshot();await d.tap(1,1);await d.consume_state_screenshot()
            self.assertEqual(ctx.mobile.capture_screen.call_count,4)
        asyncio.run(run())

    def test_display_network_cannot_block_capture_and_queue_is_coalesced(self):
        ctx=context();entered=threading.Event();release=threading.Event()
        def slow(*args,**kwargs):entered.set();release.wait(3)
        ctx.views.create.return_value.set_state.side_effect=slow
        console=MobileConsole(ctx);driver=NexusMobileDriver(ctx,console)
        try:
            console.note('start');self.assertTrue(entered.wait(1))
            started=time.monotonic();asyncio.run(driver.screenshot())
            self.assertLess(time.monotonic()-started,0.5)
            for i in range(50):console.frame(b'latest',screenshots=i,actions=0)
            self.assertEqual(console.pending_frame[0],b'latest')
            console.set_reasoning_effort({'effort':'low'},None)
            self.assertEqual(console.get_reasoning_effort(),'low')
            with self.assertRaises(ValueError):console.set_reasoning_effort({'effort':'unknown'},None)
        finally:console.close();release.set();console.worker.join(2)
        self.assertFalse(console.worker.is_alive())

    def test_terminal_console_state_is_flushed_and_worker_exits(self):
        ctx=context();console=MobileConsole(ctx)
        console.note('progress');console.finish({'status':'completed'})
        console.worker.join(2)
        self.assertFalse(console.worker.is_alive())
        state=ctx.views.create.return_value.set_state.call_args.args[0]
        self.assertEqual(state['status'],'completed')
        with self.assertRaisesRegex(ValueError,'TASK_FINISHED'):
            console.set_reasoning_effort({'effort':'low'},None)

    def test_model_effort_forwarded_and_default_omitted(self):
        ctx=context();broker=object.__new__(ModelBroker)
        broker.ctx=ctx;broker.authorization='test';broker.maximum=5;broker.calls=0
        broker.lock=threading.Lock();broker.stopped=threading.Event()
        broker.grant={'model':'test','provider':'nexus','base_url':'https://example.com/v1'}
        broker.model_seconds=0;broker.failed_requests=0;broker.efforts_used=[];broker.effort_getter=None
        broker.request_diagnostics=[];broker.previous_payload_digest=None
        credential=Mock();credential.resolve_for_runtime.side_effect=lambda _:dict(broker.grant,secret='not-a-real-key')
        requests=[]
        class Response(io.BytesIO):
            def read1(self,n):return self.read(n)
        def send(request,**kwargs):requests.append(json.loads(request.data));return Response(b'{"choices":[]}')
        with patch('model_broker.ProviderCredentials',return_value=credential),patch('model_broker.open_model_request',side_effect=send):
            for effort in ('default','low','medium','high'):
                broker.reasoning_effort=effort;broker.request({'messages':[]})
        self.assertNotIn('reasoning_effort',requests[0])
        self.assertEqual([x['reasoning_effort'] for x in requests[1:]],['low','medium','high'])
        before=broker.model_seconds
        def fail(*args,**kwargs):time.sleep(.01);raise TimeoutError('test timeout')
        with patch('model_broker.ProviderCredentials',return_value=credential),patch('model_broker.open_model_request',side_effect=fail):
            with self.assertRaises(TimeoutError):broker.request({'messages':[]})
        self.assertEqual(broker.failed_requests,1)
        self.assertGreater(broker.model_seconds,before)

    def test_large_opaque_model_response_preserved_without_regeneration(self):
        broker=object.__new__(ModelBroker);broker.ctx=context();broker.authorization='test';broker.maximum=2;broker.calls=0
        broker.lock=threading.Lock();broker.stopped=threading.Event();broker.grant={'model':'test','provider':'nexus','base_url':'https://example.com/v1'}
        broker.model_seconds=0;broker.failed_requests=0;broker.efforts_used=[];broker.effort_getter=None;broker.reasoning_effort='default'
        broker.request_diagnostics=[];broker.previous_payload_digest=None
        credential=Mock();credential.resolve_for_runtime.side_effect=lambda _:dict(broker.grant,secret='not-a-real-key')
        raw=json.dumps({'choices':[{'message':{'content':'<function_calls></function_calls>',
            'reasoning_details':[{'type':'reasoning.encrypted','data':'x'*(4*1024*1024)}]},'finish_reason':'stop'}]}).encode()
        class Response(io.BytesIO):
            def read1(self,n):return self.read(n)
        with patch('model_broker.ProviderCredentials',return_value=credential),patch('model_broker.open_model_request',return_value=Response(raw)) as send:
            self.assertEqual(broker.request({'messages':[]}),raw)
            send.assert_called_once()
        self.assertTrue(broker.request_diagnostics[0]['spooled_to_disk'])
        self.assertEqual(broker.request_diagnostics[0]['response_bytes'],len(raw))
        with patch('model_broker.ProviderCredentials',return_value=credential),patch('model_broker.open_model_request',return_value=Response(raw)),patch('model_broker.MAX_MODEL_RESPONSE_BYTES',1024):
            with self.assertRaisesRegex(ValueError,'MODEL_RESPONSE_LIMIT'):broker.request({'messages':[]})
