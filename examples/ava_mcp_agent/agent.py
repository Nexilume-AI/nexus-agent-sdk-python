import json
import signal
import threading
import queue
import os
import contextvars
import time
from nexus_agent import NexusAgent, McpToolDescriptor, McpToolResult, SseEvent, current_run
from nexus_agent.reporting import NexusRunContextUnavailable
from service import Service, CATALOG


def native(value):
    return McpToolResult(content=value.get('content',[]),structured_content=value.get('structuredContent'),is_error=value.get('isError',False))


def caller():
    # Only retry this read-only identity lookup. Never infer an owner on failure,
    # and never retry a model invocation or job submission automatically here.
    for attempt in range(3):
        try:return current_run().caller_identity()
        except NexusRunContextUnavailable:
            if attempt==2:raise
            time.sleep(.5*(attempt+1))


def main():
    stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    service=Service()
    try:
        bridge=os.environ.get('AVA_BRIDGE_ADDRESS','10.153.89.1')
        agent=NexusAgent(runtime='openwrt',router='http://127.0.0.1:7446',tenant='eda',agent_id='ava-11',listen_host=bridge,advertise_address=bridge,cloud_publish=True,cloud_name='AVA RISC-V Verification MCP',lease_seconds=120)
        agent.server.max_response_bytes=1048576;agent.server.max_stream_event_bytes=1048576
        for item in CATALOG:
            descriptor=McpToolDescriptor(name=item['name'],title=item['name'].replace('_',' ').title(),description=item['description'],input_schema=item['inputSchema'])
            def invoke(args,name=item['name']):
                identity=None if name in ('get_ava_status','ava_tool_help') else caller()
                if name == 'authorize_ava_provider':
                    from nexus_agent.credentials import ProviderCredentials
                    c=ProviderCredentials(current_run())
                    v=c.status(args['authorization_id']) if args.get('authorization_id') else c.authorize(args.get('provider','nexus'),0,model=args.get('model'),base_url=args.get('base_url'),force_new=args.get('force_new',False))
                    return native({'content':[{'type':'text','text':json.dumps(v)}],'structuredContent':v})
                return native(service.call(name,args,identity,ctx=None if identity is None else current_run()))
            agent.capability('ava.'+item['name'],public_ipv6=False,tool=descriptor)(invoke)
            def stream(args,call=invoke):
                inbox=queue.Queue(maxsize=1)
                def run():
                    try:inbox.put(call(args))
                    except Exception as exc:
                        import traceback
                        # Stack locations only: never log callback tokens, URLs, or payloads.
                        locations=[(os.path.basename(f.filename),f.lineno,f.name) for f in traceback.extract_tb(exc.__traceback__)]
                        print('AVA RISC-V request failed',type(exc).__name__,locations,flush=True)
                        code='CALLER_CONTEXT_UNAVAILABLE' if isinstance(exc,NexusRunContextUnavailable) else 'REQUEST_FAILED'
                        value={'error':code,'message':'Retry using the same request_id for a job submission. Caller identity is always required.'}
                        inbox.put(native({'content':[{'type':'text','text':json.dumps(value)}],'structuredContent':value,'isError':True}))
                context=contextvars.copy_context()
                threading.Thread(target=lambda:context.run(run),daemon=True).start()
                yield SseEvent(event='progress',data=json.dumps({'progress':0,'message':'Processing isolated AVA RISC-V MCP request'}))
                while True:
                    try:response=inbox.get(timeout=5);break
                    except queue.Empty:yield SseEvent(event='heartbeat',data='{}')
                yield SseEvent(event='result',data=json.dumps({'result':response.to_wire()}))
            agent.stream_capability('ava.'+item['name'],public_ipv6=False,tool=descriptor)(stream)
        with agent.start():
            print('AVA RISC-V MCP ready; tools='+str(len(CATALOG)),flush=True);stop.wait()
    finally:service.close()


if __name__=='__main__':main()
