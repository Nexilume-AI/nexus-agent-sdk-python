"""Native Mobilerun workflow with caller-owned model and phone, bounded FIFO."""
import asyncio
import contextlib
import importlib.metadata
import logging
import threading
import time
import uuid
from nexus_agent.credentials import ProviderCredentials
from driver import NexusMobileDriver, check_control
from model_broker import ModelBroker, validate_effort
from console import MobileConsole

VERSION='0.7.1'
COMMIT='3b62bd767131db32bce2632d55f3372b62fff015'


class Runtime:
    def __init__(self):
        self.cv=threading.Condition(); self.queue=[]; self.owners={}; self.active=0
        self.authorizations={}
        self.consoles={}

    def new_console(self, ctx, reasoning_effort="default"):
        owner=ctx.caller_identity()['owner_scope']
        previous=self.consoles.get(owner)
        if previous:
            previous.close()
            previous.view.close()
        console=MobileConsole(ctx, reasoning_effort=reasoning_effort)
        self.consoles={key:value for key,value in self.consoles.items() if time.monotonic()-value.created<3600}
        self.consoles[owner]=console
        return console

    def open_console(self, args, ctx):
        owner=ctx.caller_identity()['owner_scope']
        view_id=args.get('view_id')
        if not view_id:
            console=self.consoles.get(owner)
            if not console:return {'status':'not_ready','next_step':'Start mobilerun_run or mobilerun_observe, then call mobilerun_console.'}
            view_id=console.view.view_id
        # Cloud independently verifies the caller, Agent, organization and project.
        return ctx.views.link(view_id)

    @contextlib.contextmanager
    def slot(self, ctx):
        owner=ctx.caller_identity()['owner_scope']
        if not owner: raise ValueError('VERIFIED_CALLER_REQUIRED')
        ticket=uuid.uuid4().hex; deadline=time.monotonic()+180
        with self.cv:
            if owner in self.owners: raise ValueError('CALLER_ALREADY_HAS_MOBILE_TASK')
            if len(self.queue)>=8: raise ValueError('MOBILE_QUEUE_FULL')
            self.owners[owner]=str(getattr(ctx,'run_id','')); self.queue.append(ticket)
        entered=False
        try:
            while not entered:
                check_control(ctx)
                with self.cv:
                    if self.queue[0]==ticket and self.active<2:
                        self.queue.pop(0);self.active+=1;entered=True;break
                    if time.monotonic()>deadline: raise ValueError('QUEUE_TIMEOUT')
                    self.cv.wait(1)
            yield
        finally:
            with self.cv:
                if ticket in self.queue:self.queue.remove(ticket)
                if entered:self.active-=1
                self.owners.pop(owner,None);self.cv.notify_all()

    def authorize(self, args, ctx):
        ctx.caller_identity()
        credential=ProviderCredentials(ctx)
        if args.get('authorization_id'):return credential.status(args['authorization_id'])
        return credential.authorize(args.get('provider','nexus'),0,model=args.get('model'),base_url=args.get('base_url'),
            force_new=args.get('force_new',False),reuse_existing=not args.get('force_new',False))

    def observe(self, ctx):
        with self.slot(ctx):
            console=self.new_console(ctx)
            async def run():
                driver=NexusMobileDriver(ctx,console)
                try:
                    await driver.connect();await driver.screenshot()
                    value=await driver.call(ctx.mobile.observe,timeout=60)
                    result={'status':'completed','platform':'android','observation':value.data,'screenshot':'Available through mobilerun_console','width':driver.size[0],'height':driver.size[1]}
                    console.finish({'status':'completed','screenshots':driver.frames,'device_actions':driver.actions})
                    return {**result,'console':console.view.public_info}
                finally:await driver.disconnect()
            try:return asyncio.run(run())
            except BaseException:
                console.stopped(ctx);raise

    def run(self, args, ctx):
        message=args.get('message','')
        if not isinstance(message,str) or not message.strip() or len(message)>8000:raise ValueError('MESSAGE_REQUIRED')
        effort=validate_effort(args.get('reasoning_effort','default'))
        started=time.monotonic()
        steps=args.get('max_steps',10)
        if type(steps) is not int or not 1<=steps<=20:raise ValueError('MAX_STEPS_1_TO_20')
        owner=ctx.caller_identity()['owner_scope']
        key=(owner,str(ctx.run_id))
        authargs={k:args[k] for k in ('authorization_id',) if args.get(k)}
        if not authargs and key in self.authorizations:authargs['authorization_id']=self.authorizations[key]
        auth=self.authorize(authargs,ctx)
        if auth.get('authorization_id'):
            if len(self.authorizations)>2048:self.authorizations.clear()
            self.authorizations[key]=auth['authorization_id']
        if auth.get('status')!='authorized':
            url=auth.get('authorization_url') or auth.get('url')
            ctx.chat.say('Authorize your own vision-capable model, then call mobilerun_run again. Bind your Android phone and approve Mobile permissions before calling.'+(f' [Authorize model]({url})' if url else ''))
            return {'status':'authorization_required','authorization':auth}
        with self.slot(ctx):
            console=self.new_console(ctx,effort)
            async def execute():
                driver=NexusMobileDriver(ctx,console);broker=None;handler=None;execution=None
                try:
                    await driver.connect()
                    broker=await asyncio.to_thread(ModelBroker,ctx,auth['authorization_id'],steps+4,effort,console.get_reasoning_effort)
                    from mobilerun import MobileAgent
                    from mobilerun.config_manager.config_manager import MobileConfig
                    from llama_index.llms.openai_like import OpenAILike
                    # Framework logs can contain phone text and prompts; keep them out of shared service logs.
                    logging.getLogger('mobilerun').disabled=True
                    config=MobileConfig()
                    config.telemetry.enabled=False;config.tracing.enabled=False
                    config.credentials.enabled=False;config.mcp.enabled=False
                    config.logging.save_trajectory='none';config.agent.app_cards.enabled=False
                    config.device.auto_setup=False;config.device.portal_mode='disabled';config.device.control_backend='visual-remote'
                    config.agent.max_steps=steps;config.agent.vision_only=True
                    config.agent.fast_agent.vision=True;config.agent.fast_agent.parallel_tools=False
                    config.agent.streaming=False;config.agent.reasoning=False
                    config.agent.model_screenshot_max_side=1280;config.tools.disabled_tools=[]
                    llm=OpenAILike(model=broker.grant['model'],api_base=broker.url,api_key=broker.nonce,
                        is_chat_model=True,is_function_calling_model=True,context_window=32768,max_tokens=2048,max_retries=0,timeout=100)
                    from screenshot_state import DecisionStateProvider
                    workflow=MobileAgent(goal=message,config=config,llms=llm,driver=driver,
                        state_provider=DecisionStateProvider(driver,llm),timeout=600)
                    ctx.display.title('Mobilerun · caller phone')
                    handler=workflow.run()
                    async def await_result():
                        return await handler
                    execution=asyncio.create_task(await_result())
                    async def monitor():
                        while not execution.done():
                            await asyncio.to_thread(check_control,ctx);await asyncio.sleep(1)
                    watch=asyncio.create_task(monitor())
                    try:
                        done,_=await asyncio.wait([execution,watch],return_when=asyncio.FIRST_COMPLETED)
                        if watch in done:await watch
                        result=await execution
                    finally:
                        watch.cancel()
                        with contextlib.suppress(asyncio.CancelledError):await watch
                    reason=str(result.reason)[:16000]
                    ctx.chat.say(reason)
                    return {'status':'completed' if result.success else 'not_completed','success':bool(result.success),'reason':reason,
                        'steps':int(result.steps),'device_actions':driver.actions,'screenshots':driver.frames,'model_requests':broker.calls,
                        'reasoning_effort':console.get_reasoning_effort(),
                        'timing':{'total_seconds':round(time.monotonic()-started,3),
                            'capture_seconds':round(driver.capture_seconds,3),
                            'capture_attempts':driver.capture_attempts,'failed_captures':driver.failed_captures,
                            'model_seconds':round(broker.model_seconds,3),'failed_model_requests':broker.failed_requests,
                            'efforts_used':broker.efforts_used,'requests':broker.request_diagnostics},
                        'framework':'mobilerun','version':VERSION}
                finally:
                    driver.stopped.set()
                    if execution is not None and not execution.done():
                        with contextlib.suppress(Exception):await asyncio.wait_for(handler.cancel_run(),timeout=5)
                        execution.cancel()
                        with contextlib.suppress(asyncio.CancelledError,Exception):await asyncio.wait_for(execution,timeout=5)
                    if broker is not None:await asyncio.to_thread(broker.close)
            try:
                async def bounded():
                    return await asyncio.wait_for(execute(),timeout=600)
                result=asyncio.run(bounded())
                console.finish(result)
                return {**result,'console':console.view.public_info}
            except BaseException:
                console.stopped(ctx)
                raise

    def status(self,ctx=None):
        with self.cv:
            active_run=self.owners.get(ctx.caller_identity()['owner_scope']) if ctx else None
        return {'framework':'mobilerun','version':importlib.metadata.version('mobilerun'),'commit':COMMIT,
            'caller_task':{'active':active_run is not None,'run_id':active_run},
            'device':'caller-owned Nexus Android Mobile; never a shared server phone','model':'caller authorization required; OpenAI-compatible vision model',
            'limits':{'active_tasks':2,'queued_tasks':8,'per_caller':1,'max_steps':20,'task_timeout_seconds':600},
            'features':['native MobileAgent workflow','phone screenshots','tap','swipe','text entry with Cloud confirmation','home/back','launch Android package'],
            'unsupported':['iOS through Nexus Mobile','ADB endpoints','shell commands','app installation','clear existing text'],
            'telemetry':False}
