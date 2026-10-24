"""Trusted controller IPC. Candidate/evaluator containers never mount this socket."""
import asyncio,http.client,json,socket
class Connection(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(240);self.sock.connect('/broker/run.sock')
def request(path,payload):
 c=Connection('localhost',timeout=240)
 try:
  c.request('POST',path,json.dumps(payload).encode(),{'Content-Type':'application/json'})
  r=c.getresponse();data=json.loads(r.read(524288))
  if r.status!=200:raise RuntimeError(data.get('error','BROKER_FAILED'))
  return data
 finally:c.close()
class BrokerLLM:
 def __init__(self,cfg):self.model=cfg.name
 async def generate(self,prompt,**kwargs):return await self.generate_with_context('',[{'role':'user','content':prompt}],**kwargs)
 async def generate_with_context(self,system_message,messages,**kwargs):
  return (await asyncio.to_thread(request,'/model',{'messages':[{'role':'system','content':system_message},*messages]}))['text']
def init_client(cfg):return BrokerLLM(cfg)
