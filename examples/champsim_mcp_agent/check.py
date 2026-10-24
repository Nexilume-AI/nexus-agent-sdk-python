"""Run native preset simulations and caller/file isolation checks on the host."""
import base64,hashlib,json,time
from service import Service

def main():
    svc=Service();owner={'version':1,'principal_type':'api_key','owner_scope':'a'*64,'quota_scope':'b'*64}
    def call(name,args=None,identity=owner,error=False):
        r=svc.call(name,args or {},identity);assert bool(r.get('isError'))==error,(name,r)
        if error:return r
        return r.get('structuredContent') or json.loads(r['content'][0]['text'])
    try:
        w=call('create_champsim_workspace');auth={k:w[k] for k in ('workspace_id','workspace_key')}
        call('list_artifacts',auth,{**owner,'owner_scope':'c'*64},True)
        call('list_artifacts',{**auth,'workspace_key':'wrong'},error=True)
        call('read_artifact',{**auth,'path':'/etc/passwd'},error=True)
        call('upload_trace',{**auth,'filename':'../bad','offset':0,'data_base64':'YQ=='},error=True)
        call('upload_trace',{**auth,'filename':'invalid.trace','offset':0,'data_base64':'YQ=='})
        call('start_simulation',{**auth,'trace':'invalid.trace'},error=True)
        presets=call('list_simulation_presets',auth);evidence={}
        for p in presets['presets']:
            preset=p['executable_name'];job=call('start_simulation',{**auth,'preset':preset})
            for _ in range(180):
                status=call('job_status',{**auth,'job_id':job['job_id']})
                if status['state']!='running':break
                time.sleep(.25)
            assert status['state']=='succeeded',status
            core=status['stats'][0]['roi']['cores'][0]
            assert core['instructions']>=10000 and core['cycles']>0,core
            data=call('read_artifact',{**auth,'path':'jobs/'+job['job_id']+'/stats.json'})
            chunks=[base64.b64decode(data['data_base64'])]
            while not data['eof']:
                data=call('read_artifact',{**auth,'path':'jobs/'+job['job_id']+'/stats.json','offset':data['next_offset']});chunks.append(base64.b64decode(data['data_base64']))
            blob=b''.join(chunks);assert hashlib.sha256(blob).hexdigest()==data['sha256']
            assert json.loads(blob)==status['stats']
            evidence[preset]={'state':status['state'],'stats':status['stats'],'log_tail':status['log_tail'][-700:]}
        job=call('start_simulation',{**auth,'simulation_instructions':100000000})
        call('cancel_simulation',{**auth,'job_id':job['job_id']})
        for _ in range(20):
            status=call('job_status',{**auth,'job_id':job['job_id']})
            if status['state']!='running':break
            time.sleep(.1)
        assert status['state']=='cancelled',status
        call('close_champsim_workspace',auth);call('list_artifacts',auth,error=True)
        from pathlib import Path
        Path('native-evidence.json').write_text(json.dumps({'passed':True,'tools':len(svc.catalog),'presets':evidence},indent=2))
        print('PASS native presets, artifact checksum, caller/path isolation, malformed trace, cancellation and closed workspace')
        print('STATS_SAMPLE',json.dumps(evidence['baseline']['stats'])[:2500])
    finally:svc.close()

if __name__=='__main__':main()
