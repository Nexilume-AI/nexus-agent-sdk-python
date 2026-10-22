"""Deterministic isolation/scheduling tests; no Docker or Cloud credentials."""
import hashlib
import threading
import time
import unittest
from unittest.mock import patch
from fair_queue import FairQueue
from service import Service


def identity(value='a', quota=None):
    return dict(version=1,principal_type='api_key',owner_scope=value*64,quota_scope=(quota or value)*64)


class FakeWorker:
    count=0
    def __init__(self, sample=False):
        FakeWorker.count+=1;self.id=str(FakeWorker.count)
        self.key='workspace-secret';self.key_hash=hashlib.sha256(self.key.encode()).digest()
        self.lock=threading.Lock();self.last=time.monotonic();self.results={}
    def close(self):pass
    def rpc(self,method,params=None):
        return {'tools':[]} if method=='tools/list' else {}


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.mock=patch('service.Worker',FakeWorker);self.mock.start()
        self.service=Service();self.addCleanup(self.mock.stop);self.addCleanup(self.service.close)
    def create(self,who):
        response=self.service.call('create_wave_workspace',{},who)
        self.assertFalse(response['isError'],response)
        return {k:response['structuredContent'][k] for k in ('workspace_id','workspace_key')}
    def test_correct_workspace_secret_cannot_cross_caller(self):
        auth=self.create(identity())
        denied=self.service.call('close_wave_workspace',auth,identity('b'))
        self.assertTrue(denied['isError']);self.assertIn(auth['workspace_id'],self.service.workers)
        # New MCP sessions with the same Key use the exact same verified identity.
        self.assertFalse(self.service.call('close_wave_workspace',auth,dict(identity()))['isError'])
    def test_tool_arguments_cannot_supply_identity(self):
        response=self.service.call('create_wave_workspace',{'caller_identity':identity()})
        self.assertTrue(response['isError']);self.assertFalse(self.service.workers)
    def test_per_key_limit_is_shared_across_project_scopes_and_sessions(self):
        self.create(identity());self.create(identity('b','a'))
        self.assertTrue(self.service.call('create_wave_workspace',{},identity('c','a'))['isError'])
        self.create(identity('c'));self.create(identity('d'))
        self.assertTrue(self.service.call('create_wave_workspace',{},identity('e'))['isError'])
    def test_revoked_workspace_cannot_be_used_after_queue_wait(self):
        auth=self.create(identity());self.service.call('close_wave_workspace',auth,identity())
        self.assertTrue(self.service.call('read_wave_result',{**auth,'result_id':'id'},identity())['isError'])


class QueueTests(unittest.TestCase):
    def wait_pending(self,q,count):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            with q.condition:
                if sum(map(len,q.queues.values()))==count:return
            time.sleep(.005)
        self.fail('Queue did not reach expected length')
    def test_fifo_round_robin_and_no_simultaneous_same_key(self):
        q=FairQueue(concurrency=1);order=[]
        def work(key,label):
            with q.slot(key):order.append(label)
        threads=[]
        with q.slot('a'):
            for i,(key,label) in enumerate([('a','a1'),('a','a2'),('b','b1'),('b','b2')],1):
                t=threading.Thread(target=work,args=(key,label));t.start();threads.append(t);self.wait_pending(q,i)
        for t in threads:t.join(2);self.assertFalse(t.is_alive())
        self.assertEqual(order,['a1','b1','a2','b2'])
        self.assertFalse(q.queues);self.assertFalse(q.active)
    def test_another_caller_can_use_second_slot_and_timeout_cleans_up(self):
        q=FairQueue(wait_seconds=.05);seen=[]
        def run():
            with q.slot('b'):seen.append('b')
        with q.slot('a'):
            t=threading.Thread(target=run);t.start();t.join(1);self.assertEqual(seen,['b'])
            with self.assertRaisesRegex(RuntimeError,'TIMEOUT'):
                with q.slot('a'):self.fail('Same Key acquired concurrent slot')
        self.assertFalse(q.queues);self.assertFalse(q.active)
    def test_exception_releases_slot_and_shutdown_wakes_waiters(self):
        q=FairQueue();errors=[]
        with self.assertRaises(ValueError):
            with q.slot('a'):raise ValueError()
        def run():
            try:
                with q.slot('a'):pass
            except RuntimeError as e:errors.append(str(e))
        with q.slot('a'):
            t=threading.Thread(target=run);t.start();self.wait_pending(q,1);q.close();t.join(1)
        self.assertEqual(errors,['SERVICE_STOPPING']);self.assertFalse(q.queues);self.assertFalse(q.active)
    def test_queue_capacity_rejects_excess(self):
        q=FairQueue(per_caller_pending=0)
        with self.assertRaisesRegex(RuntimeError,'FULL'):
            with q.slot('a'):pass


if __name__=='__main__':unittest.main()
