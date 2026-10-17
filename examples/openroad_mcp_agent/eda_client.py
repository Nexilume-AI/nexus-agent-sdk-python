"""Upload RTL, run ORFS, follow logs and download verified artifacts over Cloud MCP."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

class Client:
    def __init__(self,url,token,tenant):
        if not url.startswith('https://'):raise ValueError('Use an HTTPS MCP endpoint')
        self.url=url
        self.headers={'Authorization':'Bearer '+token,'X-Nexus-Tenant':tenant,'Content-Type':'application/json',
                      'Accept':'application/json, text/event-stream','User-Agent':'Nexus-EDA-Client/1.0'}
        self.rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'nexus-eda-client','version':'1.0'}})
        self.rpc('notifications/initialized',{},notification=True)

    def rpc(self,method,params,notification=False):
        body={'jsonrpc':'2.0','method':method,'params':params}
        if not notification:body['id']=uuid.uuid4().hex
        headers=dict(self.headers)
        # File chunks are bounded, immediate responses; JSON avoids wrapping
        # large base64 blocks inside additional SSE envelopes.
        if method=='tools/call' and params.get('name')=='read_eda_artifact':headers['Accept']='application/json'
        req=urllib.request.Request(self.url,data=json.dumps(body).encode(),headers=headers)
        try:
            response=urllib.request.urlopen(req,timeout=45)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'MCP HTTP {exc.code}: '+exc.read(1500).decode(errors='replace')) from None
        with response:
            if response.headers.get('Mcp-Session-Id'):self.headers['Mcp-Session-Id']=response.headers['Mcp-Session-Id']
            if notification:return None
            if 'text/event-stream' in response.headers.get('Content-Type',''):
                data=[];result=None
                for line in response:
                    line=line.decode().rstrip('\r\n')
                    if line.startswith('data:'):data.append(line[5:].lstrip())
                    elif not line and data:
                        message=json.loads('\n'.join(data));data=[]
                        if message.get('id')==body['id']:result=message;break
                if result is None:raise RuntimeError('MCP stream ended before a result')
            else:result=json.load(response)
        if 'error' in result:raise RuntimeError(str(result['error']))
        return result['result']

    def call(self,name,arguments):
        result=self.rpc('tools/call',{'name':name,'arguments':arguments})
        if result.get('isError'):raise RuntimeError(str(result.get('content')))
        return result.get('structuredContent') or json.loads(result['content'][0]['text'])

    def upload(self,project,source,relative):
        if source.stat().st_size>32*1024*1024:raise ValueError('Input file exceeds the 32 MiB limit')
        content=source.read_bytes();offset=0
        while offset<len(content) or not content and offset==0:
            chunk=content[offset:offset+49152]
            args={'project_id':project,'path':relative,'offset':offset,'data_base64':base64.b64encode(chunk).decode()}
            if offset+len(chunk)==len(content):args['sha256']=hashlib.sha256(content).hexdigest()
            result=self.call('upload_eda_file',args)
            offset=result['next_offset']
            if offset==len(content):break
        return result

    def watch(self,job,interval=3):
        offset=0;last=None
        while True:
            state=self.call('get_eda_job',{'job_id':job,'log_offset':offset,'limit':16384})
            offset=state['next_offset']
            label=(state['status'],state['current_stage'],len(state['completed_stages']))
            if label!=last:
                print(f"[{state['status']}] stage={state['current_stage']} completed={len(state['completed_stages'])}/{len(state['stages'])}",flush=True)
                last=label
            print(state['log'],end='',flush=True)
            if state['status'] not in ('queued','running','cancelling','finalizing') and offset>=state['log_bytes']:return state
            if offset>=state['log_bytes']:time.sleep(interval)

    def artifacts(self,job):
        entries=[];offset=0
        while True:
            page=self.call('list_eda_artifacts',{'job_id':job,'offset':offset,'limit':100})
            if not page['ready']:raise RuntimeError('Artifacts are still being finalized')
            entries.extend(page['artifacts'])
            if page['eof']:return entries
            offset=page['next_offset']

    def download(self,job,entry,destination):
        destination=Path(destination)
        if destination.exists():raise ValueError('Destination already exists; choose a new output path')
        destination.parent.mkdir(parents=True,exist_ok=True)
        temporary=destination.with_name(destination.name+'.part')
        if temporary.is_symlink():raise ValueError('Download staging path must not be a symlink')
        offset=0;h=hashlib.sha256()
        with temporary.open('wb') as output:
            while True:
                page=self.call('read_eda_artifact',{'job_id':job,'artifact_id':entry['artifact_id'],'offset':offset,'limit':262144})
                raw=base64.b64decode(page['data_base64'],validate=True)
                if page['offset']!=offset or page['next_offset']!=offset+len(raw) or page['sha256']!=entry['sha256']:
                    raise RuntimeError('Artifact transfer metadata mismatch')
                output.write(raw);h.update(raw);offset=page['next_offset']
                if page['eof']:break
                if not raw:raise RuntimeError('Empty non-final chunk')
        if offset!=entry['size'] or h.hexdigest()!=entry['sha256']:raise RuntimeError('Artifact integrity check failed')
        os.link(temporary,destination);temporary.unlink()
        return {'path':str(destination),'bytes':offset,'sha256':h.hexdigest()}

    def cloud_download(self,job,entry,destination):
        destination=Path(destination)
        if destination.exists():raise ValueError('Destination already exists; choose a new output path')
        reference=self.call('export_eda_artifact',{'job_id':job,'artifact_id':entry['artifact_id']})
        run_id=str(uuid.UUID(reference['run_id']));file_id=str(uuid.UUID(reference['file']['file_id']))
        origin=urlsplit(self.url);base=origin.scheme+'://'+origin.netloc
        headers={k:v for k,v in self.headers.items() if k in ('Authorization','X-Nexus-Tenant','User-Agent')}
        request=urllib.request.Request(base+f'/api/v1/agent-runs/{run_id}/display-token/',data=b'{}',headers={**headers,'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=45) as response:
            access=json.load(response);access=access.get('data',access)
        request=urllib.request.Request(base+f'/api/v1/agent-runs/{run_id}/files/{file_id}/download/',headers={**headers,'X-Nexus-Agent-Display-Token':access['display_token']})
        destination.parent.mkdir(parents=True,exist_ok=True)
        temporary=destination.with_name(destination.name+'.part')
        if temporary.is_symlink():raise ValueError('Download staging path must not be a symlink')
        h=hashlib.sha256();size=0
        with urllib.request.urlopen(request,timeout=60) as response,temporary.open('wb') as output:
            for chunk in iter(lambda:response.read(256*1024),b''):
                size+=len(chunk)
                if size>entry['size']:raise RuntimeError('Cloud file exceeds the artifact size')
                output.write(chunk);h.update(chunk)
        if size!=entry['size'] or h.hexdigest()!=entry['sha256']:raise RuntimeError('Cloud artifact integrity check failed')
        os.link(temporary,destination);temporary.unlink()
        return {'path':str(destination),'bytes':size,'sha256':h.hexdigest(),'run_id':run_id,'file_id':file_id,'transport':'cloud-file'}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default=os.environ.get('NEXUS_MCP_URL'))
    parser.add_argument('--tenant',default=os.environ.get('NEXUS_TENANT_ID'))
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('platforms')
    submit=sub.add_parser('submit');submit.add_argument('--platform',required=True)
    design=submit.add_mutually_exclusive_group(required=True);design.add_argument('--design');design.add_argument('--top')
    submit.add_argument('--inputs',type=Path);submit.add_argument('--through',default='finish');submit.add_argument('--request-id',default=None)
    submit.add_argument('--cores',type=int,default=2);submit.add_argument('--timeout',type=int,default=3600)
    for name in ('status','watch','artifacts'):
        p=sub.add_parser(name);p.add_argument('job_id')
    p=sub.add_parser('download');p.add_argument('job_id');p.add_argument('--artifact',default='artifacts.zip');p.add_argument('--output',required=True,type=Path);p.add_argument('--mcp',action='store_true',help='Use MCP chunks instead of Cloud private file export')
    args=parser.parse_args();token=os.environ.get('NEXUS_MCP_TOKEN')
    if not args.url or not args.tenant or not token:parser.error('Set NEXUS_MCP_URL, NEXUS_TENANT_ID and NEXUS_MCP_TOKEN')
    client=Client(args.url,token,args.tenant)
    if args.command=='platforms':result=client.call('list_eda_platforms',{})
    elif args.command=='submit':
        if args.top and (not args.inputs or not args.inputs.is_dir()):parser.error('--top requires --inputs directory')
        spec={'platform':args.platform,**({'design':args.design} if args.design else {'top':args.top})}
        project=client.call('create_eda_project',spec)['project_id']
        print('project_id='+project,flush=True)
        if args.inputs:
            for path in sorted(args.inputs.rglob('*')):
                if path.is_file() and not path.is_symlink():client.upload(project,path,path.relative_to(args.inputs).as_posix())
        request=args.request_id or uuid.uuid4().hex
        print('request_id='+request,flush=True)
        result=client.call('run_eda_flow',{'project_id':project,'request_id':request,'through':args.through,'cores':args.cores,'timeout_seconds':args.timeout})
    elif args.command=='status':result=client.call('get_eda_job',{'job_id':args.job_id})
    elif args.command=='watch':result=client.watch(args.job_id)
    elif args.command=='artifacts':result=client.artifacts(args.job_id)
    else:
        entries=client.artifacts(args.job_id)
        entry=next((e for e in entries if e['path']==args.artifact or e['artifact_id']==args.artifact),None)
        if not entry:raise ValueError('Artifact not found; use artifacts to list paths and IDs')
        result=(client.download if args.mcp else client.cloud_download)(args.job_id,entry,args.output)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if args.command=='watch' and result['status']!='succeeded':raise SystemExit(1)

if __name__=='__main__':main()
