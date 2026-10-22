"""Real native-engine acceptance and workspace boundary checks; run on deployment host."""
import base64
import hashlib
import json
import time
from service import Service

def main():
    svc=Service();owner={'version':1,'principal_type':'api_key','owner_scope':'a'*64,'quota_scope':'b'*64}
    stranger={**owner,'owner_scope':'c'*64,'quota_scope':'d'*64}
    def call(name,args=None,who=owner,error=False):
        result=svc.call(name,args or {},who)
        assert bool(result.get('isError'))==error,(name,result)
        if error:return result
        return result.get('structuredContent') or json.loads(result['content'][0]['text'])
    try:
        w=call('create_ramulator_workspace');auth={k:w[k] for k in ('workspace_id','workspace_key')}
        call('list_artifacts',auth,stranger,error=True)
        call('list_artifacts',{**auth,'workspace_key':'wrong'},error=True)
        call('read_artifact',{**auth,'path':'/etc/passwd'},error=True)
        call('upload_trace',{**auth,'filename':'../bad','data_base64':'YQ==','offset':0},error=True)
        payload=b'LD 0x0\nST 0x40\nLD 0x80\nLD 0x0\n'
        params={**auth,'filename':'uploaded.trace','data_base64':base64.b64encode(payload).decode(),'offset':0}
        call('upload_trace',params);call('upload_trace',params)
        read=call('read_artifact',{**auth,'path':'traces/uploaded.trace'})
        assert base64.b64decode(read['data_base64'])==payload and read['sha256']==hashlib.sha256(payload).hexdigest()
        presets=call('list_dram_presets',auth)
        evidence={}
        for standard,p in presets.items():
            args={**auth,'standard':standard,'organization':p['organizations'][0],'timing':p['timings'][0],'trace':'uploaded.trace'}
            job=call('start_simulation',args)
            for _ in range(120):
                status=call('job_status',{**auth,'job_id':job['job_id']})
                if status['state']!='running':break
                time.sleep(.25)
            assert status['state']=='succeeded',(standard,status)
            stats=status['stats']['memory_system']
            assert stats['total_num_read_requests']==3 and stats['total_num_write_requests']==1,(standard,stats)
            assert status['stats']['frontend']['completed_requests']==4,status['stats']
            assert stats['controller']['avg_read_latency']>0 and stats['controller']['cycles']>4,stats
            data=call('read_artifact',{**auth,'path':'jobs/'+job['job_id']+'/stats.json'})
            assert json.loads(base64.b64decode(data['data_base64']))==status['stats']
            evidence[standard]={'state':status['state'],'reads':3,'writes':1,'completed':4,'avg_read_latency':stats['controller']['avg_read_latency'],'stats_sha256':data['sha256']}
        job=call('start_simulation',auth)
        call('cancel_simulation',{**auth,'job_id':job['job_id']})
        for _ in range(20):
            cancelled=call('job_status',{**auth,'job_id':job['job_id']})
            if cancelled['state']!='running':break
            time.sleep(.05)
        assert cancelled['state']=='cancelled',cancelled
        call('start_simulation',{**auth,'timing':'invalid'},error=True)
        call('close_ramulator_workspace',auth)
        call('list_artifacts',auth,error=True)
        print(json.dumps({'passed':True,'tools':len(svc.catalog),'boundaries':True,'simulations':evidence},indent=2),flush=True)
    finally:svc.close()

if __name__=='__main__':main()
