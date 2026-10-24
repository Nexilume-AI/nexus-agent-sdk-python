import sys
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from nexus_agent import NexusAgentClient,NexusAgentServer
from nexus_agent.direct_tasks import DirectTaskStore,DirectTaskCapacityError

class TaskCapacityTest(unittest.TestCase):
    def test_reads_at_capacity_preserve_all_tasks_and_wrong_tokens(self):
        store=DirectTaskStore(max_tasks=2)
        a=store.create();b=store.create();a.start();b.start()
        for _ in range(3):
            self.assertEqual(store.snapshot()['tasks'],2)
            self.assertIsNone(store.get(a.task_id,'wrong'))
            self.assertIs(store.get(a.task_id,a.token),a)
            self.assertIs(store.get(b.task_id,b.token),b)
        self.assertFalse(a.cancelled)
        self.assertFalse(b.cancelled)

    def test_new_admission_retains_completed_result_until_expiry(self):
        with patch('nexus_agent.direct_tasks.time.time',return_value=100):
            store=DirectTaskStore(max_tasks=1,ttl_seconds=30)
            task=store.create();task.start();task.complete({'answer':42})
        with patch('nexus_agent.direct_tasks.time.time',return_value=129):
            with self.assertRaises(DirectTaskCapacityError):store.create()
            self.assertEqual(store.get(task.task_id,task.token).result,{'answer':42})
        with patch('nexus_agent.direct_tasks.time.time',return_value=131):
            self.assertIsNone(store.get(task.task_id,task.token))
            replacement=store.create()
            self.assertNotEqual(replacement.task_id,task.task_id)

    def test_concurrent_admission_never_evicts_successful_admissions(self):
        store=DirectTaskStore(max_tasks=2);barrier=threading.Barrier(8);admitted=[];rejected=[]
        def create():
            barrier.wait()
            try:admitted.append(store.create())
            except DirectTaskCapacityError:rejected.append(True)
        threads=[threading.Thread(target=create) for _ in range(8)]
        for t in threads:t.start()
        for t in threads:t.join(timeout=3)
        self.assertEqual((len(admitted),len(rejected)),(2,6))
        for task in admitted:self.assertIs(store.get(task.task_id,task.token),task)

    def test_http_capacity_rejects_before_handler_and_first_task_remains_usable(self):
        entered=threading.Event();release=threading.Event();calls=[]
        server=NexusAgentServer('127.0.0.1',0,path='/agent/v1/invoke',direct_task_max_tasks=1)
        @server.handler('demo.capacity')
        def worker(request):
            calls.append(request.task_id);entered.set();release.wait(5);return {'ok':True}
        thread=server.serve_in_thread();client=NexusAgentClient(f'http://127.0.0.1:{server.port}',auth='none',timeout=3,use_environment_proxy=False)
        def env(task):return dict(version='1.0',intent='demo.capacity',intent_version=1,task_id=task,source_agent='agent://demo/a',tenant='demo',hop_limit=8,payload={})
        try:
            first=client.invoke_async(env('first'));self.assertTrue(entered.wait(1))
            self.assertEqual(first.status()['status'],'running')
            with self.assertRaises(Exception) as failure:client.invoke_async(env('second'))
            self.assertEqual(getattr(failure.exception,'status',None),503)
            self.assertEqual(getattr(failure.exception,'code',None),'DIRECT_TASK_CAPACITY_EXCEEDED')
            self.assertEqual(calls,['first']);release.set()
            self.assertEqual(first.result(timeout=3),{'ok':True})
        finally:
            release.set();server.shutdown();server.server_close();thread.join(timeout=2)

if __name__=='__main__':unittest.main()
