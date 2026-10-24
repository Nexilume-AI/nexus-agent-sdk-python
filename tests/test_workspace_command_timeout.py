"""Real HTTP command completion must outlive the event-delivery deadline."""
import json,pathlib,sys,threading,time,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from nexus_agent import NexusRunContext,NexusReportingConfig

class WorkspaceTimeoutTests(unittest.TestCase):
    def test_delayed_read_and_write_complete_once(self):
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def handle_request(self):
                calls.append(self.command)
                if self.command=='POST':self.rfile.read(int(self.headers['Content-Length']))
                time.sleep(3.5)  # Longer than the default 3-second event deadline.
                raw=json.dumps({'ok':True,'data':{'content':'delayed result','written':True},'error':None}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            do_GET=handle_request
            do_POST=handle_request
        httpd=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=httpd.serve_forever,daemon=True).start()
        try:
            context=NexusRunContext(computer_enabled=True,workspace_url=f'http://127.0.0.1:{httpd.server_port}/workspace',
                workspace_token='local-test-token',workspace_capabilities=('files.read','files.write'),config=NexusReportingConfig())
            self.assertEqual(context.config.request_timeout,3.0)
            self.assertEqual(context.workspace.read_text('input.txt'),'delayed result')
            self.assertTrue(context.workspace.write_text('output.txt','value')['written'])
            self.assertEqual(calls,['GET','POST'])
        finally:httpd.shutdown();httpd.server_close()
if __name__=='__main__':unittest.main()
