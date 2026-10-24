import http.client,socket,json
class UnixHTTP(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX);self.sock.settimeout(650);self.sock.connect('/broker/run.sock')
def request(path,value):
 c=UnixHTTP('localhost',timeout=650)
 try:
  c.request('POST',path,json.dumps(value).encode(),{'Content-Type':'application/json'})
  r=c.getresponse();raw=r.read(32*1024*1024+1)
  if len(raw)>32*1024*1024:raise RuntimeError('BROKER_RESPONSE_LIMIT')
  out=json.loads(raw)
  if r.status!=200:raise RuntimeError(out.get('error','BROKER_ERROR'))
  return out
 finally:c.close()
