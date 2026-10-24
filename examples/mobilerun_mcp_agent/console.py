"""Bounded, coalescing UI publisher: network work never blocks phone decisions."""
import base64
import contextvars
import io
import threading
import time
from pathlib import Path
from PIL import Image
from model_broker import validate_effort

class MobileConsole:
    def __init__(self, ctx, reasoning_effort="default"):
        self.ctx=ctx;self.lock=threading.RLock();self.cv=threading.Condition(self.lock)
        self.created=time.monotonic();self.dirty=False;self.pending_frame=None
        self.closing=False;self.closed=False;self.delivery_failures=0
        self.state={'status':'running','screenshots':0,'actions':0,'started_at':time.time(),
            'reasoning_effort':validate_effort(reasoning_effort),'logs':['Task started.'],'result':None}
        self.view=ctx.views.create(title='Mobilerun · Background',state=dict(self.state),
            html=Path(__file__).with_name('console.html').read_text(encoding='utf-8'),
            actions={'cancel':{'label':'Cancel task','input_schema':{'type':'object','additionalProperties':False}},
                'set_reasoning_effort':{'label':'Reasoning effort','input_schema':{'type':'object','properties':{
                    'effort':{'type':'string','enum':['default','low','medium','high']}},'required':['effort'],'additionalProperties':False}}},
            handlers={'cancel':self.cancel,'set_reasoning_effort':self.set_reasoning_effort},ttl_seconds=3600)
        context=contextvars.copy_context()
        self.worker=threading.Thread(target=lambda:context.run(self._publish),name='mobilerun-display',daemon=True)
        self.worker.start()

    def get_reasoning_effort(self):
        with self.lock:return self.state['reasoning_effort']

    def set_reasoning_effort(self, args, view):
        effort=validate_effort(args.get('effort'))
        with self.lock:
            if self.closing or self.closed:raise ValueError('TASK_FINISHED')
            self.update(reasoning_effort=effort,logs=(self.state['logs']+['Reasoning: '+effort+' (next model request).'])[-40:])
        return {'reasoning_effort':effort,'applies_to':'next_model_request'}

    def cancel(self, args, view):
        with self.lock:
            if self.state['status'] not in ('running','cancel_requested'):return {'status':self.state['status']}
        result=view.cancel_task()
        self.update(status=result.get('status','cancel_requested'))
        self.note('Cancellation requested. Waiting for the task to stop.')
        return result

    def update(self, **values):
        with self.cv:
            if self.closed:return
            self.state.update(values);self.state['elapsed_seconds']=round(time.monotonic()-self.created)
            self.dirty=True;self.cv.notify()

    def note(self, text):
        with self.lock:self.update(logs=(self.state['logs']+[text])[-40:])

    def frame(self, content, *, screenshots, actions, content_type='image/webp', width=None, height=None):
        with self.cv:
            if self.closed:return
            # One latest raw frame plus one in flight; never an unbounded queue.
            self.pending_frame=(content,content_type,width,height)
            self.update(screenshots=screenshots,actions=actions)

    @staticmethod
    def preview(content):
        with Image.open(io.BytesIO(content)) as image:
            image=image.convert('RGB');image.thumbnail((640,960))
            for quality in (65,45,25):
                out=io.BytesIO();image.save(out,format='JPEG',quality=quality)
                if len(out.getvalue())<=60000:break
            if len(out.getvalue())>60000:
                image.thumbnail((320,480));out=io.BytesIO();image.save(out,format='JPEG',quality=25)
        return 'data:image/jpeg;base64,'+base64.b64encode(out.getvalue()).decode()

    def _publish(self):
        while True:
            with self.cv:
                self.cv.wait_for(lambda:self.dirty or self.closing or self.closed)
                if self.closed or (self.closing and not self.dirty):return
                frame=self.pending_frame;self.pending_frame=None;self.dirty=False
                state=dict(self.state)
            if frame:
                try:
                    state['image']=self.preview(frame[0])
                    with self.lock:self.state['image']=state['image']
                except Exception:self.delivery_failures+=1
            try:self.view.set_state(state)
            except Exception:self.delivery_failures+=1
            if frame:
                try:self.ctx.browser.frame(frame[0],content_type=frame[1],width=frame[2],height=frame[3],
                    title='Mobilerun · attached phone',text='Caller-authorized phone screenshot')
                except Exception:self.delivery_failures+=1

    def finish(self, result):
        with self.cv:
            self.update(status=result.get('status','failed'),result=result)
            self.closing=True;self.cv.notify()

    def close(self):
        with self.cv:
            self.closed=True;self.pending_frame=None;self.cv.notify()

    def stopped(self, ctx):
        try:cancelled=bool(ctx.control(refresh=True).get('cancel_requested'))
        except Exception:cancelled=False
        self.finish({'status':'cancelled' if cancelled or self.state['status']=='cancel_requested' else 'failed',
            'error':'Task stopped; inspect the MCP task result.'})
