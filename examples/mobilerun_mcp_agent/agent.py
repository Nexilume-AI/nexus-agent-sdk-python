import json,signal,threading
from nexus_agent import NexusAgent,McpToolDescriptor,McpToolResult,current_run,MOBILE_SCOPES
from runtime import Runtime
from driver import InvocationContext
from streaming import task_stream


def main():
    stop=threading.Event()
    for signum in (signal.SIGTERM,signal.SIGINT):signal.signal(signum,lambda *_:stop.set())
    runtime=Runtime()
    agent=NexusAgent(runtime='openwrt',router='http://127.0.0.1:7446',tenant='eda',agent_id='mobilerun-11',
        listen_host='10.153.89.1',advertise_address='10.153.89.1',cloud_publish=True,cloud_name='Mobilerun Mobile Agent',lease_seconds=120,
        mobile_requirement='optional',mobile_capabilities=MOBILE_SCOPES)
    agent.server.max_response_bytes=1048576;agent.server.max_stream_event_bytes=1048576
    tools=[
        McpToolDescriptor(name='mobilerun_console',title='Open task console',description='Open the latest task Background console for this caller, or renew a view_id returned by this Agent. Returns a short-lived single-use Interactive View link with pushed progress, screenshots, result and cancellation. No Chat UI or publisher credentials. Call after starting mobilerun_run or mobilerun_observe.',input_schema={'type':'object','properties':{'view_id':{'type':'string'}},'additionalProperties':False}),
        McpToolDescriptor(name='mobilerun_status',title='Mobilerun status',description='Inspect the native Mobilerun version, supported Nexus Mobile actions and limits. No model call or phone operation.',input_schema={'type':'object','properties':{},'additionalProperties':False}),
        McpToolDescriptor(name='authorize_mobilerun_model',title='Authorize Mobilerun model',description='Request or inspect your own vision model authorization. Choose a Nexus Router or OpenAI-compatible endpoint through secure confirmation. Do not pass API keys in tool arguments.',input_schema={'type':'object','properties':{'authorization_id':{'type':'string'},'provider':{'type':'string','enum':['nexus','openai','custom_openai']},'model':{'type':'string'},'base_url':{'type':'string'},'force_new':{'type':'boolean'}},'additionalProperties':False}),
        McpToolDescriptor(name='mobilerun_observe',title='Observe attached phone',description='Read the caller-attached Android phone UI and publish a protected screenshot in this Run. Requires your online Nexus Mobile binding and Android screen permissions. MCP invocation authorizes the declared phone scopes; no model call.',input_schema={'type':'object','properties':{},'additionalProperties':False},task=True,interactive=True,mobile_scopes=('mobile.observe','mobile.screen.capture')),
        McpToolDescriptor(name='mobilerun_run',title='Run mobile task',description='Use native Mobilerun to perform a natural-language task on your currently attached Nexus Android phone, using your explicitly authorized vision model. Bind your own phone before calling. Follow progress and protected screenshots in the Background page from mobilerun_console. MCP invocation authorizes the declared Mobile scopes without extra Cloud approvals. No shared server device, ADB address, shell or publisher key. Up to 20 steps and 10 minutes; cancel through MCP tasks/cancel or the Background page.',input_schema={'type':'object','properties':{'message':{'type':'string','maxLength':8000},'max_steps':{'type':'integer','minimum':1,'maximum':20,'default':10},'authorization_id':{'type':'string'},'reasoning_effort':{'type':'string','enum':['default','low','medium','high'],'default':'default','description':'Model reasoning budget: default preserves provider settings; low favors latency; medium/high allow more reasoning. Provider must support reasoning_effort. Can also change in the Background console for subsequent requests.'}},'required':['message'],'additionalProperties':False},task=True,interactive=True,continuable=True,mobile_scopes=MOBILE_SCOPES),
    ]
    def invoke(args,name,stopped=None):
        ctx=InvocationContext(current_run(), stopped)
        if name=='mobilerun_console':value=runtime.open_console(args,ctx)
        elif name=='mobilerun_status':value=runtime.status(ctx)
        elif name=='authorize_mobilerun_model':value=runtime.authorize(args,ctx)
        elif name=='mobilerun_observe':value=runtime.observe(ctx)
        else:value=runtime.run(args,ctx)
        return McpToolResult(content=[{'type':'text','text':json.dumps(value,ensure_ascii=False)}],structured_content=value)
    for descriptor in tools:
        def call(args,name=descriptor.name):return invoke(args,name)
        agent.capability('mobilerun.'+descriptor.name,public_ipv6=True,tool=descriptor)(call)
        def stream(args,name=descriptor.name):
            yield from task_stream(lambda stopped:invoke(args,name,stopped))
        agent.stream_capability('mobilerun.'+descriptor.name,public_ipv6=True,tool=descriptor)(stream)
    with agent.start():
        print('Mobilerun ready: 5 MCP tools',flush=True);stop.wait()


if __name__=='__main__':main()
