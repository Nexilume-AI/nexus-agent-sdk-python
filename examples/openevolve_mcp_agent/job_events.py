"""Durable producer journal and outbound Cloud delivery. No model credentials."""
import json
import os
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

STATE_KEYS={'job_id','status','created_at','started_at','finished_at','model_requests','last_provider_http_status','exit_code','error','artifacts','artifact_collection_error'}
TERMINAL={'succeeded','failed','cancelled','timed_out','authorization_expired','interrupted'}


def save(path,value):
    temp=path.with_suffix('.next');temp.write_text(json.dumps(value),encoding='utf-8');temp.chmod(0o600);os.replace(temp,path)


def validate_config(value):
    if not isinstance(value,dict) or set(value)!={'job_ref','push_url','command_url','artifact_url','view_url','token'}:
        raise ValueError('INVALID_EVENT_DESTINATION')
    jid=str(uuid.UUID(value['job_ref']))
    prefix='/api/v1/agent-background-jobs/'+jid+'/'
    for key,suffix in [('push_url','ingest/'),('command_url','commands/'),('artifact_url','artifacts/'),('view_url','page/')]:
        parsed=urlsplit(value[key])
        if parsed.scheme!='https' or parsed.netloc!='cloud.nexilume.com' or parsed.path!=prefix+suffix or parsed.query or parsed.fragment:
            raise ValueError('INVALID_EVENT_DESTINATION')
    if not isinstance(value['token'],str) or not 32<=len(value['token'])<=128:raise ValueError('INVALID_EVENT_TOKEN')
    return dict(value)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class Journal:
    def __init__(self,directory,config=None):
        self.directory=Path(directory);self.path=self.directory/'events.jsonl'
        self.condition=threading.Condition(threading.RLock())
        self.rows=[];self.bytes=0;self.log_bytes=0;self.terminal=False
        if self.path.exists():
            # Ignore only an interrupted final append, never malformed committed rows.
            raw=self.path.read_bytes();parts=raw.splitlines(keepends=True);valid=0
            for index,line in enumerate(parts):
                if not line.endswith(b'\n') and index==len(parts)-1:break
                row=json.loads(line);self.rows.append(row);self.bytes+=len(line);valid+=len(line)
                if row['kind']=='log':self.log_bytes+=len(row['data']['text'].encode())
            if valid!=len(raw):
                with self.path.open('r+b') as f:f.truncate(valid)
        config_path=self.directory/'event-delivery.json'
        if config is not None:
            config=validate_config(config)
            if config_path.exists() and json.loads(config_path.read_text())!=config:raise ValueError('EVENT_CONFIG_CONFLICT')
            if not config_path.exists():save(config_path,config)
        self.config=validate_config(json.loads(config_path.read_text())) if config_path.exists() else None
        self.terminal=any(row['kind']=='state' and row['data'].get('status') in TERMINAL for row in self.rows)
        self.delivered=any(row['kind']=='delivery' for row in self.rows)

    def emit(self,kind,data):
        with self.condition:
            if kind=='state':data={key:value for key,value in data.items() if key in STATE_KEYS}
            if kind=='log':
                size=len(data['text'].encode())
                if self.log_bytes+size>12*1024*1024 or len(self.rows)>=11000:return
                self.log_bytes+=size
            row={'seq':len(self.rows)+1,'kind':kind,'data':data,'time':time.time()}
            encoded=(json.dumps(row,allow_nan=False)+'\n').encode()
            if kind=='log' and self.bytes+len(encoded)>20*1024*1024:return
            if self.bytes+len(encoded)>24*1024*1024 or len(self.rows)>=12000:raise RuntimeError('EVENT_QUOTA_EXCEEDED')
            with self.path.open('ab') as f:f.write(encoded);f.flush();os.fsync(f.fileno())
            self.path.chmod(0o600);self.bytes+=len(encoded);self.rows.append(row)
            if kind=='state' and data.get('status') in TERMINAL:self.terminal=True
            if kind=='delivery':self.delivered=True
            self.condition.notify_all()
            return row

    def follow(self,after=0,seconds=45):
        if type(after) is not int or not 0<=after<=len(self.rows):raise ValueError('INVALID_EVENT_CURSOR')
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            with self.condition:
                rows=self.rows[after:after+32]
                if not rows:
                    if self.terminal:return
                    self.condition.wait(min(10,max(0,deadline-time.monotonic())))
                    rows=self.rows[after:after+32]
            if not rows:yield None
            for row in rows:after=row['seq'];yield row


class Delivery:
    def __init__(self,journal,cancel):
        self.journal,self.cancel=journal,cancel
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        self.stop=threading.Event()

    def start(self):
        if not self.journal.config:return
        threading.Thread(target=self.send,daemon=True).start()
        if not self.journal.terminal:threading.Thread(target=self.commands,daemon=True).start()

    def request(self,url,body=None,content_type='application/json'):
        headers={'X-Nexus-Job-Token':self.journal.config['token'],'User-Agent':'Nexus-Atrex-Events/1','Content-Type':content_type}
        return self.opener.open(urllib.request.Request(url,data=body,headers=headers),timeout=70)

    def send(self):
        journal=self.journal;cursor_path=journal.directory/'event-ack.json'
        after=json.loads(cursor_path.read_text()) if cursor_path.exists() else 0
        backoff=1;deadline=time.monotonic()+7200
        while time.monotonic()<deadline:
            try:
                with journal.condition:
                    rows=journal.rows[after:after+16]
                    if not rows and not journal.terminal:
                        journal.condition.wait(30);continue
                if rows:
                    with self.request(journal.config['push_url'],json.dumps({'events':rows}).encode()) as response:
                        result=json.load(response);result=result.get('data',result)
                    ack=result['ack']
                    if type(ack) is not int or not after<=ack<=len(journal.rows):raise ValueError('INVALID_ACK')
                    after=ack;save(cursor_path,after)
                    if result.get('cancel_requested'):self.cancel.set()
                elif journal.terminal:
                    if not journal.delivered:
                        self.artifacts();journal.emit('delivery',{'complete':True})
                    else:self.stop.set();return
                backoff=1
            except Exception:
                # Never log response bodies, callback tokens or model credentials.
                if self.stop.wait(backoff):return
                backoff=min(30,backoff*2)

    def artifacts(self):
        journal=self.journal;record=json.loads((journal.directory/'job.json').read_text())
        for item in record.get('artifacts',[]):
            path=journal.directory/item['path']
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(journal.directory.resolve()):raise ValueError('INVALID_ARTIFACT')
            marker=journal.directory/('uploaded-'+item['sha256']+'-'+uuid.uuid5(uuid.NAMESPACE_URL,item['path']).hex)
            if marker.exists():continue
            boundary='nexus'+uuid.uuid4().hex
            body=(f'--{boundary}\r\nContent-Disposition: form-data; name="path"\r\n\r\n'+item['path']+f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="artifact.bin"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()+path.read_bytes()+f'\r\n--{boundary}--\r\n'.encode()
            with self.request(journal.config['artifact_url'],body,'multipart/form-data; boundary='+boundary) as response:response.read(65536)
            marker.touch(mode=0o600)

    def commands(self):
        deadline=time.monotonic()+7200;backoff=1
        while not self.stop.is_set() and not self.journal.terminal and time.monotonic()<deadline:
            try:
                with self.request(self.journal.config['command_url']) as response:
                    for line in response:
                        if line.strip()==b'event: cancel':self.cancel.set();return
                        if self.stop.is_set() or self.journal.terminal:return
                backoff=1
            except Exception:
                if self.stop.wait(backoff):return
                backoff=min(30,backoff*2)
