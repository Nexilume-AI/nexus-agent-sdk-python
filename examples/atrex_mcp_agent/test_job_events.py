import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch
from job_events import Journal, validate_config


class EventTests(unittest.TestCase):
    def test_mcp_watch_emits_replay_progress_and_resume_cursor(self):
        import agent as app
        streams={};runtime=MagicMock();execution=Mock()
        runtime.stream_capability.side_effect=lambda name,**kw:lambda fn:streams.setdefault(name,fn)
        runtime.capability.side_effect=lambda *a,**kw:lambda fn:fn
        execution.request.return_value={'authorization_id':'test','view_url':'https://cloud.nexilume.com/test'}
        execution.events.return_value=iter([{'seq':8,'kind':'log','data':{'text':'actual output'},'time':0},None])
        with patch.object(app,'NexusAgent',return_value=runtime),patch.object(app,'Execution',return_value=execution),patch.object(app,'Service'),patch.object(app,'ProviderCredentials'),patch.object(app,'current_run'),patch.object(app.threading,'Event'),patch.object(app.signal,'signal'):
            app.main()
            events=list(streams['atrex.watch_atrex_job']({'project_id':'test','access_key':'private','job_id':'test','after':7}))
        self.assertEqual(events[0].event,'progress')
        result=json.loads(events[-1].data)['result']['result']
        self.assertFalse(result['isError']);self.assertEqual(result['structuredContent']['after'],8)
        self.assertNotIn('private',json.dumps([event.data for event in events]))

    def test_reconnect_replays_only_missing_events_and_restart_recovers(self):
        with tempfile.TemporaryDirectory() as root:
            journal=Journal(root);journal.emit('state',{'status':'running','secret':'must not escape'})
            journal.emit('log',{'text':'hello'});journal.emit('state',{'status':'succeeded'})
            self.assertNotIn('must not escape',Path(root,'events.jsonl').read_text())
            recovered=Journal(root)
            self.assertEqual([row['seq'] for row in recovered.follow(1)],[2,3])
            self.assertEqual(list(recovered.follow(3)),[])
            with self.assertRaises(ValueError):list(recovered.follow(9))

    def test_live_subscriber_wakes_on_emit_without_job_queries(self):
        with tempfile.TemporaryDirectory() as root:
            journal=Journal(root);received=[]
            t=threading.Thread(target=lambda:received.extend(journal.follow()));t.start()
            journal.emit('log',{'text':'streamed now'});journal.emit('state',{'status':'cancelled'})
            t.join(2);self.assertFalse(t.is_alive());self.assertEqual(received[0]['data']['text'],'streamed now')

    def test_partial_append_recovery_and_private_callback_destination(self):
        with tempfile.TemporaryDirectory() as root:
            journal=Journal(root);journal.emit('log',{'text':'saved'})
            with Path(root,'events.jsonl').open('ab') as file:file.write(b'{"seq":')
            journal=Journal(root);journal.emit('state',{'status':'failed'})
            self.assertEqual(len(Journal(root).rows),2)
        with self.assertRaises(ValueError):validate_config({'push_url':'http://localhost/private'})

    def test_stream_redacts_credentials_across_pipe_chunks(self):
        from broker import Broker
        with tempfile.TemporaryDirectory() as root:
            journal=Journal(root);broker=object.__new__(Broker);broker.journals={'test':journal}
            broker.live={'test':{'capability':'private-capability','credentials':{'secret':'private-secret','interaction_token':'callback-token'}}}
            process=Mock();process.stdout.read1.side_effect=[b'hello\nprivate-sec',b'ret\nprivate-cap',b'ability\ncallback-token\n',b'']
            broker.follow_logs('test',process)
            text=''.join(row['data']['text'] for row in journal.rows)
            self.assertIn('hello',text);self.assertEqual(text.count('[redacted]'),3)
            self.assertNotIn('private-secret',text);self.assertNotIn('callback-token',text)
