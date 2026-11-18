"""Outbound model transport. Install nexilume[relay] on Python 3.11+.

Only a browser-approved binding can select an upstream. POSTs are never replayed.
The same module runs the SDK worker and the single Cloud-side transport broker.
"""
import asyncio
import base64
import ipaddress
import json
import sys
import uuid
from urllib.parse import urlsplit
import aiohttp
from aiohttp import web

MAX_BODY = 16 * 1024 * 1024
HTTP_SESSION = web.AppKey('http', aiohttp.ClientSession)
ROUTES = {'openai-compatible': {('GET','models'),('GET','model/info'),('POST','chat/completions'),('POST','responses')},
          'system_one': {('GET','models'),('GET','model/info'),('POST','systemone')}}


def valid_request(binding, method, path, body):
    if (method,path) not in ROUTES.get(binding.get('protocol'),set()) or len(body)>MAX_BODY:return False
    if method=='GET':return not body
    try:
        data=json.loads(body)
        return isinstance(data,dict) and data.get('model') in binding['models']
    except (ValueError,TypeError):return False


def upstream_url(binding, path):
    base=binding['upstream_endpoint'].rstrip('/')
    for suffix in ('/chat/completions','/responses','/systemone','/models'):
        if base.endswith(suffix):base=base[:-len(suffix)];break
    parsed=urlsplit(base)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Invalid approved endpoint')
    return base+'/'+path


class PublicResolver(aiohttp.resolver.DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        rows=await super().resolve(host,port,family)
        if not rows or any(not ipaddress.ip_address(r['host']).is_global for r in rows):
            raise OSError('Model upstream must resolve to public addresses')
        return rows


def check_binding(binding, approved):
    matches=[m for m in approved if m.get('transport')=='outbound' and m['endpoint']==binding['upstream_endpoint'] and m['protocol']==binding['protocol']]
    if not set(binding['models']).issubset({m['model'] for m in matches}):raise ValueError('Unapproved binding')
    if any(m.get('provider_id') and m['provider_id']!=binding['provider_id'] for m in matches if m['model'] in binding['models']):
        raise ValueError('Provider binding differs from approval')
    upstream_url(binding,'models')


async def worker_connection(session, ws, bindings):
    tasks,acks={},{}
    async def invoke(data):
        ident=data['id']
        try:
            binding=bindings[data['binding_id']]
            body=base64.b64decode(data.get('body',''),validate=True)
            if not valid_request(binding,data.get('method'),data.get('path'),body):raise ValueError()
            url=upstream_url(binding,data['path'])
            # Also reject IP literals, which aiohttp can connect without resolver.
            try:literal=ipaddress.ip_address(urlsplit(url).hostname)
            except ValueError:literal=None
            if literal is not None and not literal.is_global:raise ValueError()
            async with session.request(data['method'],url,data=body or None,
                    headers={'Authorization':data['authorization'],'Content-Type':'application/json',
                             'Accept':'application/json, text/event-stream','User-Agent':'Nexus-Model-Relay/1'},
                    allow_redirects=False) as response:
                await ws.send_json({'type':'headers','id':ident,'status':response.status,'content_type':response.headers.get('Content-Type','application/json')})
                async def send(chunk):
                    acks[ident].clear()
                    await ws.send_json({'type':'chunk','id':ident,'data':base64.b64encode(chunk).decode()})
                    await asyncio.wait_for(acks[ident].wait(),180)
                if data['path'] in {'models', 'model/info'} and response.status==200:
                    raw=bytearray()
                    async for chunk in response.content.iter_chunked(32768):
                        raw.extend(chunk)
                        if len(raw)>MAX_BODY:raise ValueError()
                    catalog=json.loads(raw)
                    catalog['data']=[r for r in catalog.get('data',[]) if
                        isinstance(r,dict) and (r.get('id') if data['path']=='models' else r.get('model_name') or r.get('id')) in binding['models']]
                    await send(json.dumps(catalog).encode())
                else:
                    async for chunk in response.content.iter_chunked(32768):await send(chunk)
                await ws.send_json({'type':'end','id':ident})
        except asyncio.CancelledError:raise
        except Exception:
            if not ws.closed:await ws.send_json({'type':'failed','id':ident})
        finally:tasks.pop(ident,None);acks.pop(ident,None)
    try:
        async for message in ws:
            if message.type!=aiohttp.WSMsgType.TEXT:continue
            data=json.loads(message.data);ident=data.get('id')
            if data.get('type')=='ack' and ident in acks:acks[ident].set()
            elif data.get('type')=='cancel' and ident in tasks:tasks[ident].cancel()
            elif data.get('type')=='request' and ident not in tasks:
                if not isinstance(ident,str) or len(ident)>64 or len(tasks)>=8:raise ValueError()
                acks[ident]=asyncio.Event();tasks[ident]=asyncio.create_task(invoke(data))
    finally:
        running=list(tasks.values())
        for task in running:task.cancel()
        await asyncio.gather(*running,return_exceptions=True)


async def serve(client):
    from .relay_models import RelayModelError
    delay=1
    while True:
        try:
            auth=await asyncio.to_thread(client._transport_credentials)
            info=await asyncio.to_thread(client.transport_status)
            parsed=urlsplit(info['websocket_url']);origin=urlsplit(client.cloud_url)
            if parsed.scheme!='wss' or parsed.netloc!=origin.netloc or parsed.path!='/api/model-relay/worker' or parsed.query or parsed.fragment:
                raise ValueError('Unexpected transport origin')
            bindings={b['binding_id']:b for b in info['bindings']}
            for binding in bindings.values():check_binding(binding,auth['models'])
            timeout=aiohttp.ClientTimeout(total=None,sock_connect=15,sock_read=180)
            async with aiohttp.ClientSession(timeout=timeout,trust_env=False) as control, aiohttp.ClientSession(
                    timeout=timeout,trust_env=False,connector=aiohttp.TCPConnector(resolver=PublicResolver())) as upstream:
                async with control.ws_connect(info['websocket_url'],headers={'Authorization':'Bearer '+auth['token']},heartbeat=20,max_msg_size=MAX_BODY*2) as ws:
                    print('MODEL_RELAY_CONNECTED',flush=True);delay=1
                    async def renew():
                        while True:
                            await asyncio.sleep(30)
                            fresh=await asyncio.to_thread(client._transport_credentials)
                            current=await asyncio.to_thread(client.transport_status)
                            updated={b['binding_id']:b for b in current['bindings']}
                            for binding in updated.values():check_binding(binding,fresh['models'])
                            bindings.clear();bindings.update(updated)
                            await ws.send_json({'type':'auth','token':fresh['token']})
                    renewal=asyncio.create_task(renew());reader=asyncio.create_task(worker_connection(upstream,ws,bindings))
                    try:
                        done,_=await asyncio.wait([renewal,reader],return_when=asyncio.FIRST_COMPLETED)
                        for task in done:task.result()
                    finally:
                        renewal.cancel();reader.cancel();await asyncio.gather(renewal,reader,return_exceptions=True)
        except RelayModelError as exc:
            if exc.status in (401,403) or (not exc.status and not exc.code.startswith('Cloud connection failed')):
                print('MODEL_RELAY_AUTH_REQUIRED',flush=True);raise
            print('MODEL_RELAY_RECONNECT',flush=True)
        except (OSError,aiohttp.ClientError,asyncio.TimeoutError):print('MODEL_RELAY_RECONNECT',flush=True)
        await asyncio.sleep(delay);delay=min(30,delay*2)


def gateway(control_url, *, host_header=None, recheck_interval=20):
    workers={};by_binding={}
    async def control(token,path,data=None):
        headers={'Authorization':'Bearer '+token}
        if host_header:headers.update({'Host':host_header,'X-Forwarded-Proto':'https'})
        async with app[HTTP_SESSION].request('GET' if data is None else 'POST',control_url.rstrip('/')+'/'+path,
                headers=headers,json=data,allow_redirects=False) as response:
            if response.status!=200:raise PermissionError()
            raw=await response.content.read(1024*1024+1)
            if len(raw)>1024*1024:raise ValueError()
            result=json.loads(raw)
            return result['data'] if result.get('ok') is True else result
    def fail(code,status=503):return web.json_response({'error':{'code':code,'replayed':False}},status=status)
    def token(request):
        value=request.headers.get('Authorization','')
        return value[7:] if value.startswith('Bearer ') and len(value)<=8300 else ''
    async def refresh(state,new_token=None):
        async with state['lock']:
            candidate=new_token or state['token']
            info=await control(candidate,'transport/session/')
            if info['device_id']!=state['device']:raise PermissionError()
            state['token']=candidate;state['bindings']={b['binding_id']:b for b in info['bindings']}
            for ident,owner in list(by_binding.items()):
                if owner is state and ident not in state['bindings']:by_binding.pop(ident,None)
            for ident in state['bindings']:
                if ident in by_binding and by_binding[ident] is not state:raise PermissionError()
                by_binding[ident]=state
    async def websocket(request):
        secret=token(request)
        if not secret:return fail('unauthorized',401)
        try:info=await control(secret,'transport/session/')
        except Exception:return fail('unauthorized',401)
        ident=info['device_id']
        if ident in workers:return fail('worker_already_connected',409)
        ws=web.WebSocketResponse(heartbeat=20,max_msg_size=MAX_BODY*2)
        state={'device':ident,'token':secret,'ws':ws,'pending':{},'bindings':{},'lock':asyncio.Lock()}
        workers[ident]=state
        watcher=None
        try:
            await refresh(state);await ws.prepare(request)
            async def watch():
                while True:
                    await asyncio.sleep(recheck_interval)
                    try:await refresh(state)
                    except Exception:
                        # Token rotation can race the worker's auth frame.
                        await asyncio.sleep(2)
                        try:await refresh(state)
                        except Exception:await ws.close(code=1008);return
            watcher=asyncio.create_task(watch())
            async for message in ws:
                if message.type!=aiohttp.WSMsgType.TEXT:continue
                data=json.loads(message.data)
                if data.get('type')=='auth':await refresh(state,data['token']);continue
                queue=state['pending'].get(data.get('id'))
                if queue is not None:queue.put_nowait(data)
        except (ValueError,KeyError,PermissionError,asyncio.QueueFull,aiohttp.ClientError,asyncio.TimeoutError):
            await ws.close(code=1008)
        finally:
            workers.pop(ident,None)
            for key,owner in list(by_binding.items()):
                if owner is state:by_binding.pop(key,None)
            for queue in state['pending'].values():
                while not queue.empty():queue.get_nowait()
                queue.put_nowait({'type':'disconnected'})
            # Notify requests before any cancellable await: peer disconnect can
            # cancel this handler while the authorization watcher is stopping.
            if watcher:watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)
        return ws
    async def proxy(request):
        key=token(request)
        if not key:return fail('unauthorized',401)
        binding_id=request.match_info['binding'];path=request.match_info['path']
        state=by_binding.get(binding_id)
        if not state or state['ws'].closed:return fail('relay_offline')
        body=await request.read()
        binding=state['bindings'].get(binding_id,{})
        if request.query_string or not valid_request(binding,request.method,path,body):return fail('unsupported_relay_request',400)
        try:
            async with state['lock']:
                checked=await control(state['token'],'transport/authorize/',{'binding_id':binding_id,'endpoint_api_key':key})
            if any(checked.get(k)!=binding.get(k) for k in ('binding_id','provider_id','upstream_endpoint','protocol','models')):
                return fail('relay_binding_changed')
        except Exception:return fail('unauthorized',401)
        if len(state['pending'])>=8:return fail('relay_busy',429)
        ident=uuid.uuid4().hex;queue=asyncio.Queue(maxsize=4);state['pending'][ident]=queue;ws=state['ws'];response=None
        try:
            await ws.send_json({'type':'request','id':ident,'binding_id':binding_id,'method':request.method,'path':path,
                'authorization':'Bearer '+key,'body':base64.b64encode(body).decode()})
            head=await asyncio.wait_for(queue.get(),180)
            if head.get('type')!='headers':return fail('relay_disconnected')
            status=int(head['status'])
            if not 100<=status<=599:raise ValueError()
            response=web.StreamResponse(status=status,headers={'Content-Type':head.get('content_type','application/json'),
                'Cache-Control':'no-store','X-Accel-Buffering':'no'})
            await response.prepare(request)
            while True:
                item=await asyncio.wait_for(queue.get(),180)
                if item.get('type')=='end':await response.write_eof();return response
                if item.get('type')!='chunk':raise ConnectionError()
                chunk=base64.b64decode(item['data'],validate=True)
                if len(chunk)>MAX_BODY:raise ValueError()
                await response.write(chunk);await ws.send_json({'type':'ack','id':ident})
        except (ConnectionError,asyncio.TimeoutError,RuntimeError,ValueError,KeyError):
            if response is not None and response.prepared:
                if request.transport:request.transport.close()
                return response
            return fail('relay_interrupted_not_replayed',504)
        finally:
            state['pending'].pop(ident,None)
            if not ws.closed:
                try:await ws.send_json({'type':'cancel','id':ident})
                except (ConnectionError,RuntimeError):pass
    app=web.Application(client_max_size=MAX_BODY)
    async def context(app):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10),trust_env=False) as session:
            app[HTTP_SESSION]=session;yield
        for state in list(workers.values()):await state['ws'].close(code=1001)
    app.cleanup_ctx.append(context)
    app.router.add_get('/worker',websocket)
    app.router.add_route('*','/bindings/{binding}/v1/{path:.*}',proxy)
    return app


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Cloud model Relay broker (operator only)')
    parser.add_argument('--control',default='http://127.0.0.1:8002/api/v1/auth/relay/')
    parser.add_argument('--host-header',required=True)
    parser.add_argument('--port',type=int,default=18764)
    args=parser.parse_args()
    web.run_app(gateway(args.control,host_header=args.host_header),host='127.0.0.1',port=args.port,access_log=None,handler_cancellation=True)
