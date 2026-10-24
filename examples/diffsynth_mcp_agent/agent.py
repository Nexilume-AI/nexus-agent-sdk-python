import json
import signal
import threading
import queue
import os
import contextvars
from nexus_agent import NexusAgent, McpToolDescriptor, McpToolResult, SseEvent, current_run
from service import Service


def native(value):
    return McpToolResult(content=value.get('content',[]),structured_content=value.get('structuredContent'),is_error=value.get('isError',False))


def main():
    stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    service=Service()
    try:
        bridge=os.environ.get('DIFFSYNTH_BRIDGE_ADDRESS','10.153.89.1')
        agent=NexusAgent(runtime='openwrt',router='http://127.0.0.1:7446',tenant='eda',agent_id='diffsynth-11',listen_host=bridge,advertise_address=bridge,cloud_publish=True,cloud_name='DiffSynth LoRA Training MCP',lease_seconds=120)
        agent.server.max_response_bytes=1048576;agent.server.max_stream_event_bytes=1048576
        for item in service.catalog:
            descriptor=McpToolDescriptor(name=item['name'],title=item['name'].replace('_',' ').title(),description=item['description'],input_schema=item['inputSchema'])
            def invoke(args,name=item['name']):
                identity=None if name in ('get_diffsynth_status','diffsynth_tool_help','list_recipes') else current_run().caller_identity()
                return native(service.call(name,args,identity,context=None if identity is None else current_run()))
            agent.capability('diffsynth.'+item['name'],public_ipv6=True,tool=descriptor)(invoke)
            def stream(args,call=invoke):
                inbox=queue.Queue(maxsize=1)
                def run():
                    try:inbox.put(call(args))
                    except Exception as exc:
                        import traceback
                        # Stack locations only: never log callback tokens, URLs, or payloads.
                        locations=[(os.path.basename(f.filename),f.lineno,f.name) for f in traceback.extract_tb(exc.__traceback__)]
                        print('DiffSynth LoRA Training request failed',type(exc).__name__,locations,flush=True)
                        inbox.put(native({'content':[{'type':'text','text':'DiffSynth LoRA Training worker failed; create a new workspace if it expired.'}],'isError':True}))
                context=contextvars.copy_context()
                threading.Thread(target=lambda:context.run(run),daemon=True).start()
                yield SseEvent(event='progress',data=json.dumps({'progress':0,'message':'Processing isolated DiffSynth LoRA Training MCP request'}))
                while True:
                    try:response=inbox.get(timeout=5);break
                    except queue.Empty:yield SseEvent(event='heartbeat',data='{}')
                yield SseEvent(event='result',data=json.dumps({'result':response.to_wire()}))
            agent.stream_capability('diffsynth.'+item['name'],public_ipv6=True,tool=descriptor)(stream)
        with agent.start():
            print('DiffSynth LoRA Training MCP ready; tools='+str(len(service.catalog)),flush=True);stop.wait()
    finally:service.close()


if __name__=='__main__':main()
