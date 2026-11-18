import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from nexus_agent.model_relay import gateway,worker_connection,valid_request,check_binding,upstream_url,PublicResolver


class ModelRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls=[];self.cancelled=asyncio.Event();self.rotated=asyncio.Event();self.tokens={'device-A','device-B'};self.revoked=False
        self.binding={'binding_id':'binding-1','provider_id':'provider-1','upstream_endpoint':'https://api.example/v1',
            'endpoint':'https://cloud.example/api/model-relay/bindings/binding-1/v1','models':['model-1'],'protocol':'openai-compatible','status':'prepared'}
        async def control(req):
            auth=req.headers.get('Authorization','')
            if self.revoked or auth.removeprefix('Bearer ') not in self.tokens:return web.json_response({},status=401)
            if req.path.endswith('/session/'):
                if auth=='Bearer device-B':self.rotated.set()
                return web.json_response({'device_id':'device-1','bindings':[self.binding]})
            body=await req.json()
            if body.get('binding_id')!='binding-1' or body.get('endpoint_api_key')!='upstream-key':return web.json_response({},status=401)
            return web.json_response(self.binding)
        async def upstream(req):
            self.calls.append(req.path)
            if req.path.endswith('/models'):return web.json_response({'data':[{'id':'model-1'},{'id':'hidden'}]})
            if req.path.endswith('/model/info'):return web.json_response({'data':[{'model_name':'model-1','model_info':{'max_input_tokens':1000}},{'model_name':'hidden'}]})
            body=await req.json()
            if body.get('stream'):
                response=web.StreamResponse(headers={'Content-Type':'text/event-stream'});await response.prepare(req)
                try:
                    await response.write(b'data: {"choices":[]}\n\n')
                    if body.get('wait'):await asyncio.sleep(60)
                    else:await response.write(b'data: [DONE]\n\n');await response.write_eof()
                    return response
                except asyncio.CancelledError:self.cancelled.set();raise
            return web.json_response({'choices':[{'message':{'content':'OK'}}]})
        app=web.Application();app.router.add_route('*','/{path:.*}',control);self.control=TestServer(app);await self.control.start_server()
        app=web.Application();app.router.add_route('*','/{path:.*}',upstream);self.upstream=TestServer(app,handler_cancellation=True);await self.upstream.start_server()
        self.server=TestServer(gateway(str(self.control.make_url('')),recheck_interval=.1),handler_cancellation=True);await self.server.start_server()
        self.client=aiohttp.ClientSession();self.ws=await self.client.ws_connect(self.server.make_url('/worker'),headers={'Authorization':'Bearer device-A'})
        # Only the fixture transport bypasses public HTTPS, never production config.
        self.url_patch=patch('nexus_agent.model_relay.upstream_url',side_effect=lambda b,p:str(self.upstream.make_url('/v1/'+p)));self.url_patch.start()
        self.ip_patch=patch('nexus_agent.model_relay.ipaddress.ip_address',return_value=SimpleNamespace(is_global=True));self.ip_patch.start()
        self.worker=asyncio.create_task(worker_connection(self.client,self.ws,{'binding-1':self.binding.copy()}))
        self.headers={'Authorization':'Bearer upstream-key'}
    async def asyncTearDown(self):
        await self.ws.close();await self.worker;self.url_patch.stop();self.ip_patch.stop()
        await self.client.close();await self.server.close();await self.control.close();await self.upstream.close()
    def url(self,path='chat/completions'):return self.server.make_url('/bindings/binding-1/v1/'+path)
    async def test_catalog_scope_json_stream_and_cutover(self):
        async with self.client.get(self.url('models'),headers=self.headers) as r:self.assertEqual((await r.json())['data'],[{'id':'model-1'}])
        async with self.client.get(self.url('model/info'),headers=self.headers) as r:self.assertEqual((await r.json())['data'],[{'model_name':'model-1','model_info':{'max_input_tokens':1000}}])
        self.binding['status']='active'
        async with self.client.post(self.url(),headers=self.headers,json={'model':'model-1'}) as r:self.assertEqual(r.status,200)
        async with self.client.post(self.url(),headers=self.headers,json={'model':'model-1','stream':True}) as r:self.assertIn('[DONE]',await r.text())
    async def test_auth_scope_and_duplicate_worker(self):
        for headers,model,status in [({},'model-1',401),({'Authorization':'Bearer wrong'},'model-1',401),(self.headers,'other',400)]:
            async with self.client.post(self.url(),headers=headers,json={'model':model}) as r:self.assertEqual(r.status,status)
        with self.assertRaises(aiohttp.WSServerHandshakeError) as error:
            await self.client.ws_connect(self.server.make_url('/worker'),headers={'Authorization':'Bearer device-A'})
        self.assertEqual(error.exception.status,409);self.assertEqual(self.calls,[])
    async def test_disconnect_no_replay(self):
        r=await self.client.post(self.url(),headers=self.headers,json={'model':'model-1','stream':True,'wait':True})
        await r.content.readuntil(b'\n\n');await self.ws.close()
        with self.assertRaises(aiohttp.ClientPayloadError):await asyncio.wait_for(r.read(),5)
        await asyncio.wait_for(self.cancelled.wait(),2);self.assertEqual(len(self.calls),1)
    async def test_cancellation(self):
        r=await self.client.post(self.url(),headers=self.headers,json={'model':'model-1','stream':True,'wait':True})
        await r.content.readuntil(b'\n\n');r.close();await asyncio.wait_for(self.cancelled.wait(),2)
        self.assertEqual(len(self.calls),1)
    async def test_rotation_and_revocation(self):
        await self.ws.send_json({'type':'auth','token':'device-B'});await asyncio.wait_for(self.rotated.wait(),2);self.tokens.remove('device-A')
        async with self.client.post(self.url(),headers=self.headers,json={'model':'model-1'}) as r:self.assertEqual(r.status,200)
        self.revoked=True
        async with self.client.post(self.url(),headers=self.headers,json={'model':'model-1'}) as r:self.assertEqual(r.status,401)
        self.assertEqual(len(self.calls),1)
        await asyncio.wait_for(self.worker,4)


class ScopeTests(unittest.TestCase):
    def test_binding_and_routes(self):
        spec={'model':'m','endpoint':'https://api.example/v1','protocol':'openai-compatible','transport':'outbound','provider_id':'a'}
        b={'models':['m'],'upstream_endpoint':spec['endpoint'],'protocol':spec['protocol'],'provider_id':'a'}
        check_binding(b,[spec]);self.assertEqual(upstream_url(b,'models'),'https://api.example/v1/models')
        for changed in [{**b,'models':['other']},{**b,'provider_id':'b'},{**b,'upstream_endpoint':'https://evil.example'}]:
            with self.assertRaises(ValueError):check_binding(changed,[spec])
        self.assertFalse(valid_request(b,'POST','../admin',b'{}'))
        self.assertFalse(valid_request(b,'POST','chat/completions',b'{"model":"other"}'))
        self.assertTrue(valid_request(b,'POST','chat/completions',b'{"model":"m"}'))
        with self.assertRaises(ValueError):upstream_url({**b,'upstream_endpoint':'http://localhost/v1'},'models')
