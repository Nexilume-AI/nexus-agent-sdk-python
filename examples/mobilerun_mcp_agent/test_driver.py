import asyncio
import io
from PIL import Image
import unittest
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from driver import NexusMobileDriver, InvocationContext, check_control
from streaming import task_stream
from nexus_agent import McpToolResult
from runtime import Runtime


def context(owner='caller'):
    mobile=Mock()
    mobile.status.return_value=SimpleNamespace(enabled=True,available=True,platform='android')
    image=io.BytesIO();Image.new('RGB',(400,800),'white').save(image,format='WEBP')
    mobile.capture_screen.return_value=SimpleNamespace(width=400,height=800,content=image.getvalue(),content_type='image/webp')
    return SimpleNamespace(mobile=mobile,control=lambda:{},raise_if_cancelled=lambda:None,browser=Mock(),views=Mock(),caller_identity=lambda:{'owner_scope':owner})


class DriverTests(unittest.TestCase):
    def test_terminal_cloud_run_stops_unmanaged_sync_worker(self):
        for state in ('failed','completed','cancelled','expired'):
            ctx=context();ctx.control=lambda:{'managed':False,'state':state}
            with self.assertRaisesRegex(ValueError,'RUN_CANCELLED'):check_control(ctx)
            ctx.mobile.tap.assert_not_called()

    def test_stream_disconnect_releases_slot_after_worker_cleanup(self):
        runtime=Runtime();entered=threading.Event();cleaned=threading.Event()
        def worker(stop):
            ctx=InvocationContext(context(),stop)
            try:
                with runtime.slot(ctx):
                    entered.set()
                    while True:
                        check_control(ctx);time.sleep(.01)
            finally:cleaned.set()
        stream=task_stream(worker,.01);next(stream)
        self.assertTrue(entered.wait(1));self.assertTrue(runtime.owners)
        stream.close()
        self.assertTrue(cleaned.wait(2))
        self.assertFalse(runtime.owners);self.assertEqual(runtime.active,0)
        with runtime.slot(context()):self.assertEqual(runtime.active,1)

    def test_local_deadline_stops_without_remote_cancellation(self):
        ctx=InvocationContext(context(),timeout=-1)
        with self.assertRaisesRegex(ValueError,'TASK_DEADLINE_EXCEEDED'):check_control(ctx)

    def test_scope_and_coordinates(self):
        ctx=context(); driver=NexusMobileDriver(ctx)
        async def test():
            await driver.connect()
            with self.assertRaises(ValueError):await driver.tap(20,30)
            png=await driver.screenshot();self.assertTrue(png.startswith(b'\x89PNG'));await driver.tap(20,30)
            ctx.mobile.tap.assert_called_once_with(x=20/399,y=30/799,coordinate_space='normalized',timeout=120)
            with self.assertRaises(ValueError):await driver.tap(400,30)
            with self.assertRaises(ValueError):await driver.input_text('hello',clear=True)
            with self.assertRaises(ValueError):await driver.press_button('shell')
            await driver.input_text('hello')
            ctx.mobile.type_text.assert_called_once_with('hello',timeout=180)
            await driver.disconnect()
            with self.assertRaises(ValueError):await driver.screenshot()
        asyncio.run(test())

    def test_offline_does_not_fall_back_to_server_phone(self):
        ctx=context();ctx.mobile.status.return_value.available=False
        with self.assertRaisesRegex(ValueError,'ATTACH_ONLINE'):asyncio.run(NexusMobileDriver(ctx).connect())
        ctx.mobile.capture_screen.assert_not_called()

    def test_one_active_or_waiting_task_per_caller(self):
        runtime=Runtime();ctx=context()
        with runtime.slot(ctx):
            with self.assertRaisesRegex(ValueError,'CALLER_ALREADY'):
                with runtime.slot(ctx):pass
            with runtime.slot(context('other')):self.assertEqual(runtime.active,2)
        self.assertEqual(runtime.active,0);self.assertFalse(runtime.owners)


if __name__=='__main__':unittest.main()
