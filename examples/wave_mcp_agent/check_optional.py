"""Verify local optional binaries and viewer MCP lifecycle in an isolated worker.

This checks worker-local HTTP delivery, not a public browser route or rendering.
Run with WAVE_IMAGE pointing to the locally built optional image.
"""
import base64
import json
import subprocess
from service import Worker


def main():
    worker = Worker(sample=True)
    try:
        def call(name, **args):
            response = worker.rpc('tools/call', {'name': name, 'arguments': args})
            assert not response.get('isError'), response
            value = response.get('structuredContent')
            if value is None:
                value = json.loads(response['content'][0]['text'])
            assert value.get('status') != 'error', value
            return value

        subprocess.run(['docker', 'exec', worker.name, 'python', '-c', '''
import pathlib, subprocess
subprocess.run(['verible-verilog-syntax', '/work/sample/counter.sv'], check=True)
for tool in ['gcc', 'g++', 'make', 'patch']:
    subprocess.run([tool, '--version'], check=True, stdout=subprocess.DEVNULL)
assert pathlib.Path('/usr/include/zlib.h').is_file()
plugin = '/opt/wave-optionals/fstdumper.so'
symbols = subprocess.check_output(['nm', '-D', plugin], text=True)
assert 'vlog_startup_routines_bootstrap' in symbols
assert 'not found' not in subprocess.check_output(['ldd', plugin], text=True)
subprocess.run(['python', '-m', 'pip', 'check'], check=True)
print('PASS Verible syntax, build tools, fstdumper symbols/dependencies and pip check')
'''], check=True)
        call('upload_wave_file', filename='missing-runtime.fsdb',
             data_base64=base64.b64encode(b'FSDB runtime availability probe').decode())
        unavailable = worker.rpc('tools/call', {'name':'convert_fsdb_to_fst',
            'arguments':{'fsdb_path':'/work/missing-runtime.fsdb'}})
        assert 'FsdbReader' in json.dumps(unavailable), unavailable
        assert 'no Verdi' in json.dumps(unavailable), unavailable
        print('PASS FSDB reports the missing proprietary runtime')
        view = call('open_wave_view', fst_paths=['/work/sample/dump.vcd'],
                    signals=[{'path': 'top_tb.u_counter.count'}])
        assert view.get('available') is True, view
        view_id = view['view_id']
        # Never publish this worker-local, token-bearing URL in test output.
        subprocess.run(['docker', 'exec', '-i', worker.name, 'python', '-c', '''
import json, sys, urllib.request, urllib.parse
url=json.load(sys.stdin)['url']
parts=urllib.parse.urlsplit(url)
root=parts.scheme+'://'+parts.netloc
for path in [url, root+'/index.html', root+'/api/view-state?'+parts.query]:
    with urllib.request.urlopen(path, timeout=10) as response:
        assert response.status == 200
        assert response.read()
print('PASS worker-local viewer shell, WASM entry and state HTTP endpoints')
'''], input=json.dumps({'url': view['url']}), text=True, check=True)
        call('update_wave_view', view_id=view_id, cursor={'time':'40', 'unit':'ns'})
        export = call('_nexus_export_wave_view', view_id=view_id, export=True)
        assert 'cursor_set 40' in export['snapshot']['panels'][0]['commands'], export['snapshot']
        assert 'surver_select_file' not in export['snapshot']['panels'][0]['commands']
        payload = subprocess.check_output(['docker','exec',worker.name,'cat',export['files'][0]])
        assert len(payload)>100
        print('PASS private tmpfs export and native-timescale browser cursor commands')
        state = call('get_view_state', view_id=view_id)
        assert '40' in json.dumps(state), state
        assert view_id in json.dumps(call('list_wave_views'))
        call('close_wave_view', view_id=view_id)
        assert view_id not in json.dumps(call('list_wave_views'))
        print('PASS viewer MCP open/update/state/list/close; no public browser route tested')
    finally:
        worker.close()


if __name__ == '__main__':
    main()
