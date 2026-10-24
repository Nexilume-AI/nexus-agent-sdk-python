import contextvars,json,os,queue,signal,threading
from nexus_agent import NexusAgent,McpToolDescriptor,McpToolResult,SseEvent,current_run
from nexus_agent.credentials import ProviderCredentials
from runtime import Runtime
from dataclasses import replace

def main():
 stop=threading.Event()
 for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
 runtime=Runtime()
 agent=NexusAgent(runtime='openwrt',router='http://127.0.0.1:7446',tenant='eda',agent_id='pi-chat-11',listen_host='10.153.89.1',advertise_address='10.153.89.1',cloud_publish=True,cloud_name='Pi Coding Agent',lease_seconds=120,computer_requirement='optional',workspace_capabilities=('files.list','files.read','files.write','command.execute','browser.control'))
 agent.server.max_response_bytes=1048576;agent.server.max_stream_event_bytes=1048576
 tools=[McpToolDescriptor(name='pi_chat',title='Chat with Pi',description='Chat with native Pi in a private offline workspace or operate your attached Computer with scoped file, command and browser tools, including web search. Uses your approved model API; valid caller-owned grants are reused. Supports streaming replies, protected downloads and continuation in Private Display. Limits: 12 model requests, 30 tools and 10 minutes per turn; sandbox expires after one idle hour.',input_schema={'type':'object','properties':{'message':{'type':'string','maxLength':16000},'authorization_id':{'type':'string','description':'Optional previously approved per-caller model authorization ID.'}},'required':['message'],'additionalProperties':False},chat=True,task=True,continuable=True,interactive=True),
 McpToolDescriptor(name='authorize_pi_model',title='Authorize Pi model',description='Request or inspect caller-owned model authorization for Pi. Choose your own Nexus Router or OpenAI-compatible provider on the secure approval page. Never pass API keys in tool arguments.',input_schema={'type':'object','properties':{'authorization_id':{'type':'string'},'provider':{'type':'string','enum':['nexus','openai','custom_openai']},'model':{'type':'string'},'base_url':{'type':'string'},'force_new':{'type':'boolean'}},'additionalProperties':False}),
 McpToolDescriptor(name='pi_status',title='Pi runtime status',description='Show installed Pi version, private workspace limits and available tools.',input_schema={'type':'object','properties':{},'additionalProperties':False})]
 def invoke(a,name):
  ctx=current_run()
  ctx.config=replace(ctx.config,control_poll_interval=1.0)
  if name=='pi_chat':value=runtime.chat(a,ctx)
  elif name=='authorize_pi_model':
   ctx.caller_identity();c=ProviderCredentials(ctx)
   value=c.status(a['authorization_id']) if a.get('authorization_id') else c.authorize(a.get('provider','nexus'),0,model=a.get('model'),base_url=a.get('base_url'),force_new=a.get('force_new',True))
  else:value={'version':'1.4.0','upstream':'earendil-works/pi','chat':True,'native_tools':['read','bash','edit','write'],'computer':'optional; current Run attached Computer only','computer_tools':['computer_list','computer_read','computer_write','computer_exec','computer_browser_search','computer_browser_open','computer_browser_observe','computer_browser_click','computer_browser_fill','computer_browser_scroll'],'workspace':'private offline Linux sandbox, 128 MiB, one hour idle expiry','limits':{'sessions_per_caller':2,'sessions_total':8,'concurrent_turns':2,'model_requests_per_turn':12,'tool_calls_per_turn':30},'model_authorization':'caller-owned; required'}
  return McpToolResult(content=[{'type':'text','text':json.dumps(value,ensure_ascii=False)}],structured_content=value)
 for descriptor in tools:
  def call(a,name=descriptor.name):return invoke(a,name)
  agent.capability('pi.'+descriptor.name,public_ipv6=True,tool=descriptor)(call)
  def stream(a,call=call):
   inbox=queue.Queue(maxsize=1)
   def run():
    try:inbox.put(call(a))
    except Exception as e:
     code=str(e) if isinstance(e,ValueError) else type(e).__name__
     print('Pi request failed',type(e).__name__,flush=True)
     inbox.put(McpToolResult(content=[{'type':'text','text':'Pi could not complete this turn: '+code[:100]}],is_error=True))
   context=contextvars.copy_context();threading.Thread(target=lambda:context.run(run),daemon=True).start()
   yield SseEvent(event='progress',data=json.dumps({'progress':0,'message':'Pi is processing your request'}))
   while True:
    try:value=inbox.get(timeout=5);break
    except queue.Empty:yield SseEvent(event='heartbeat',data='{}')
   yield SseEvent(event='result',data=json.dumps({'result':value.to_wire()}))
  agent.stream_capability('pi.'+descriptor.name,public_ipv6=True,tool=descriptor)(stream)
 try:
  with agent.start():print('Pi Chat ready: 3 tools',flush=True);stop.wait()
 finally:runtime.close()

if __name__=='__main__':main()
