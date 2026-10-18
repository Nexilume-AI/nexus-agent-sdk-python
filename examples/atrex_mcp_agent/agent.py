"""Publish the Atrex preparation MCP catalog through OpenWrt and Cloud Relay."""
import json
import os
import signal
import threading
from nexus_agent import McpToolDescriptor, McpToolResult, NexusAgent, SseEvent, current_run
from nexus_agent.credentials import ProviderCredentials
from service import Service, ToolError, catalog


def main():
    service = Service('/data/projects', '/upstream')
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    agent = NexusAgent(runtime='openwrt', router=os.environ.get('NEXUS_ROUTER_URL', 'http://router:7446'),
                       tenant=os.environ.get('NEXUS_TENANT', 'default'), agent_id=os.environ.get('NEXUS_AGENT_ID', 'atrex-mcp'), advertise_address='auto', cloud_publish=True,
                       cloud_name='Atrex Kernel Agent MCP', lease_seconds=120)
    agent.server.max_response_bytes = 1048576
    agent.server.max_stream_event_bytes = 1048576
    for item in catalog():
        descriptor = McpToolDescriptor(name=item['name'], title=item['name'].replace('_', ' ').title(),
                                       description=item['description'], input_schema=item['inputSchema'])
        def invoke(args, name=item['name']):
            try:
                if name == 'authorize_atrex_provider':
                    value = ProviderCredentials(current_run()).authorize(args.get('provider'), args.get('wait_seconds', 15))
                    value['execution_enabled'] = False
                    value['next_step'] = (
                        'Authorization is available for this caller until expiry or revocation. GPU execution remains unconfigured.'
                        if value.get('status') == 'authorized' else
                        'Open authorization_url, sign in as the invoking user and confirm authorization, then retry this tool. GPU execution remains unconfigured.'
                    )
                else:
                    value = service.call(name, args)
                error = False
            except ToolError as exc:
                value, error = {'error': exc.code, 'message': str(exc)}, True
            except Exception:
                value, error = {'error':'INTERNAL_ERROR','message':'Operation failed; contact the operator.'}, True
            return McpToolResult(content=[{'type':'text','text':json.dumps(value,ensure_ascii=False)}], structured_content=value, is_error=error)
        agent.capability('atrex.'+item['name'], public_ipv6=True, tool=descriptor)(invoke)
        def stream(args, call=invoke):
            yield SseEvent(event='progress', data=json.dumps({'progress':0,'message':'Processing Atrex MCP request'}))
            yield SseEvent(event='result', data=json.dumps({'result':call(args).to_wire()}))
        agent.stream_capability('atrex.'+item['name'], public_ipv6=True, tool=descriptor)(stream)
    with agent.start():
        print('Atrex preparation MCP ready; tools=%d; optimization disabled' % len(catalog()), flush=True)
        stop.wait()


if __name__ == '__main__':
    main()
