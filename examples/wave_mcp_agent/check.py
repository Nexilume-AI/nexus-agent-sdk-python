"""Real isolated-worker smoke test; no Cloud credentials or user data."""
import base64,hashlib,json
from service import Service


def main():
    service=Service()
    try:
        def call(name,args=None,error=False):
            res=service.call(name,args or {},{'version':1,'principal_type':'api_key','owner_scope':'a'*64,'quota_scope':'b'*64})
            assert bool(res.get('isError'))==error,(name,res)
            return res.get('structuredContent') or res
        status=call('get_wave_status');assert status['tools']==46
        w=call('create_wave_workspace',{'sample':True});auth={k:w[k] for k in ('workspace_id','workspace_key')}
        print('PASS isolated workspace, 46 tools',flush=True)
        prep=call('prepare_session',{**auth,**w['sample']})
        print('PREP',json.dumps(prep)[:1800],flush=True)
        sid=prep.get('session_id')
        assert sid,prep
        session={**auth,'session_id':sid}
        info=call('session_info',session);print('SESSION',json.dumps(info)[:700],flush=True)
        values=call('signal_values',{**session,'paths':['top_tb.u_counter.count'],'start':'0ns','end':'50ns'})
        print('VALUES',json.dumps(values)[:1600],flush=True)
        assert '11111111' in json.dumps(values) or '255' in json.dumps(values),values
        modules=call('list_modules',session);assert 'counter' in json.dumps(modules)
        drivers=call('signal_drivers',{**session,'path':'top_tb.u_counter.count'});assert 'counter.sv' in json.dumps(drivers)
        print('PASS VCD conversion, FST values and RTL driver analysis',flush=True)
        content=b'module remote(input a,output y); assign y=~a; endmodule\n'
        upload={**auth,'filename':'remote.sv','data_base64':base64.b64encode(content).decode()}
        call('upload_wave_file',upload);call('upload_wave_file',upload)
        read=call('read_wave_file',{**auth,'filename':'remote.sv'});assert base64.b64decode(read['data_base64'])==content
        assert read['sha256']==hashlib.sha256(content).hexdigest()
        static=call('open_static_session',{**auth,'top':'remote','filelist':['/work/remote.sv']});assert static.get('session_id'),static
        call('upload_wave_file',{**upload,'filename':'../../escape.sv'},True)
        call('list_wave_files',{**auth,'workspace_key':'wrong'},True)
        other=call('create_wave_workspace');otherauth={k:other[k] for k in ('workspace_id','workspace_key')}
        call('read_wave_file',{**otherauth,'filename':'remote.sv'},True)
        wrong=call('session_info',{**otherauth,'session_id':sid})
        assert wrong.get('error') or wrong.get('status')=='error',wrong
        call('close_wave_workspace',otherauth)
        worker=service.workers[auth['workspace_id']]
        large={'content':[{'type':'text','text':'x'*210000}]}
        page=worker.bounded(large)['structuredContent'];output=bytearray();offset=0
        while True:
            part=call('read_wave_result',{**auth,'result_id':page['result_id'],'offset':offset})
            output.extend(base64.b64decode(part['data_base64']));offset=part['next_offset']
            if part['eof']:break
        assert json.loads(output)==large
        call('close_wave_workspace',auth);call('list_wave_files',auth,True)
        print('PASS uploads/retry/SHA256, static-only analysis, workspace isolation, traversal denial, large results and cleanup',flush=True)
    finally:service.close()


if __name__=='__main__':main()
