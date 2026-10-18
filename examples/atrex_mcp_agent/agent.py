"""Publish the Atrex project/execution MCP catalog through OpenWrt and Cloud Relay."""
import json
import os
import signal
import threading
from nexus_agent import McpToolDescriptor, McpToolResult, NexusAgent, SseEvent, current_run
from nexus_agent.credentials import ProviderCredentials
from nexus_agent.background_jobs import BackgroundJobs
from service import Service, ToolError, catalog
from execution import Execution
import uuid


def main():
    service = Service('/data/projects', '/upstream')
    execution = Execution()
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
                    credentials = ProviderCredentials(current_run())
                    value = (credentials.status(args['authorization_id']) if args.get('authorization_id') else
                        credentials.authorize(args.get('provider','nexus'), args.get('wait_seconds', 15), base_url=args.get('base_url'), model=args.get('model'), force_new=args.get('force_new',False)))
                    value['execution_enabled'] = execution.status()['execution_enabled']
                    value['next_step'] = (
                        'Use authorization_id with provider, model and a fresh request_id in start_atrex_optimization.'
                        if value.get('status') == 'authorized' else
                        'Confirm the client authorization form, or open authorization_url. Nexus Cloud lets you choose an accessible Router/model without a key. Then check this authorization_id.'
                    )
                elif name == 'start_atrex_optimization':
                    validation = service.call('validate_atrex_project', args)
                    if not validation['static_valid']:
                        raise ToolError('INVALID_OPERATOR_INPUT', 'Upload a complete valid operator layout before starting.')
                    if validation['layout'] != 'atrex-bench':
                        raise ToolError('UNSUPPORTED_EXECUTION_LAYOUT', 'This Pascal GPU runner executes native Atrex-Bench inputs: reference.py, input.py and shapes.json. The current SOL evaluator requires newer hardware.')
                    if not execution.status()['execution_enabled']:
                        raise ToolError('EXECUTION_NOT_READY', 'The isolated GPU runner has not passed its image/GPU preflight. Check get_atrex_status before retrying.')
                    authorization_id = str(uuid.UUID(args.get('authorization_id', '')))
                    credentials = ProviderCredentials(current_run())
                    try:
                        grant = credentials.resolve_for_runtime(authorization_id)
                    except Exception:
                        raise ToolError('AUTHORIZATION_REQUIRED', 'No active caller-owned grant could be resolved. Call authorize_atrex_provider, approve the secure URL, then retry.') from None
                    grant['status_url'] = current_run().interaction_url.rstrip('/') + '/credentials/' + authorization_id + '/'
                    grant['interaction_token'] = current_run()._interaction_token
                    try:
                        delivery=BackgroundJobs(current_run()).register_for_runtime(authorization_id,args['request_id'])
                        value = execution.request('submit', {**args, 'authorization_id':authorization_id, 'credentials':grant,'event_delivery':delivery})
                    finally:
                        grant.clear()
                        if 'delivery' in locals():delivery.clear()
                elif name in ('get_atrex_job', 'cancel_atrex_job', 'read_atrex_artifact','watch_atrex_job'):
                    service.project(args)
                    record = execution.request('record', args)
                    # An approved project bearer alone cannot impersonate the caller who funded a job.
                    ProviderCredentials(current_run()).status(record['authorization_id'])
                    operation = {'get_atrex_job':'get','cancel_atrex_job':'cancel','read_atrex_artifact':'read','watch_atrex_job':'record'}[name]
                    value = record if operation == 'record' else execution.request(operation, args)
                else:
                    value = service.call(name, args)
                error = False
            except ToolError as exc:
                value, error = {'error': exc.code, 'message': str(exc)}, True
            except RuntimeError as exc:
                code = str(exc)
                if not code or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789' for c in code): code = 'AUTHORIZATION_OR_EXECUTION_FAILED'
                value, error = {'error':code,'message':'Check caller authorization and runner readiness; use get_atrex_status and authorize_atrex_provider.'}, True
            except Exception:
                value, error = {'error':'INTERNAL_ERROR','message':'Operation failed; contact the operator.'}, True
            return McpToolResult(content=[{'type':'text','text':json.dumps(value,ensure_ascii=False)}], structured_content=value, is_error=error)
        agent.capability('atrex.'+item['name'], public_ipv6=True, tool=descriptor)(invoke)
        def stream(args, call=invoke, name=item['name']):
            if name=='watch_atrex_job':
                checked=call(args)
                if checked.to_dict().get('isError'):
                    yield SseEvent(event='result',data=json.dumps({'result':checked.to_wire()}));return
                after=args.get('after',0)
                try:
                    for row in execution.events(args):
                        if row is None:
                            yield SseEvent(event='heartbeat',data='{}');continue
                        after=row['seq']
                        yield SseEvent(event='progress',data=json.dumps({'progress':after,'message':json.dumps(row,ensure_ascii=False)}))
                    value={'after':after,'job_id':args['job_id'],'view_url':checked.to_dict().get('structuredContent',{}).get('view_url',''),
                           'next_step':'Use view_url for continuous live updates, or resume watch_atrex_job with after. No job is restarted.'}
                    yield SseEvent(event='result',data=json.dumps({'result':McpToolResult(content=[{'type':'text','text':json.dumps(value)}],structured_content=value).to_wire()}))
                except Exception:
                    yield SseEvent(event='result',data=json.dumps({'result':McpToolResult(content=[{'type':'text','text':'Event connection interrupted. Resume with the last received sequence; the background task continues.'}],is_error=True).to_wire()}))
                return
            yield SseEvent(event='progress', data=json.dumps({'progress':0,'message':'Processing Atrex MCP request'}))
            yield SseEvent(event='result', data=json.dumps({'result':call(args).to_wire()}))
        agent.stream_capability('atrex.'+item['name'], public_ipv6=True, tool=descriptor)(stream)
    with agent.start():
        print('Atrex MCP ready; tools=%d; execution uses caller-owned authorization' % len(catalog()), flush=True)
        stop.wait()


if __name__ == '__main__':
    main()
