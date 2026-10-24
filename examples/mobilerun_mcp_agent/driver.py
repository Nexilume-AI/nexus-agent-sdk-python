"""Mobilerun driver for the caller's Run-bound Nexus Mobile delegate."""
import asyncio
import datetime
import io
import math
import re
import threading
import time
from PIL import Image


class InvocationContext:
    """Local stream cancellation complements the authenticated Cloud Run state."""
    def __init__(self, ctx, stopped=None, timeout=600):
        self.ctx = ctx
        self.stopped = stopped if stopped is not None else threading.Event()
        self.deadline = time.monotonic() + timeout

    def __getattr__(self, name):
        return getattr(self.ctx, name)

    def check_local_control(self):
        if self.stopped.is_set():
            raise ValueError('RUN_CANCELLED')
        if time.monotonic() >= self.deadline:
            raise ValueError('TASK_DEADLINE_EXCEEDED')


def check_control(ctx):
    local = getattr(ctx, 'check_local_control', None)
    if local is not None: local()
    state = ctx.control()
    if state.get('cancel_requested') or state.get('state') in ('cancel_requested', 'cancelled', 'failed', 'completed', 'expired') or state.get('remaining_seconds') == 0:
        raise ValueError('RUN_CANCELLED')
    ctx.raise_if_cancelled()


class NexusMobileDriver:
    platform = 'android'
    supported = {'tap', 'swipe', 'input_text', 'press_button', 'start_app', 'screenshot'}
    supported_buttons = {'back', 'home'}

    def __init__(self, ctx, console=None):
        self.ctx = ctx
        self.console = console
        self.stopped = threading.Event()
        self.size = None
        self.actions = 0
        self.frames = 0
        self._state_image = None
        self.capture_seconds = 0.0
        self.capture_attempts = 0
        self.failed_captures = 0

    async def call(self, operation, **kwargs):
        if self.stopped.is_set():
            raise ValueError('RUN_CANCELLED')
        await asyncio.to_thread(check_control, self.ctx)
        result = await asyncio.to_thread(operation, **kwargs)
        if self.stopped.is_set():
            raise ValueError('RUN_CANCELLED')
        return result

    async def connect(self):
        if self.console is not None:await asyncio.to_thread(self.console.note,'Checking the attached phone connection.')
        status = await self.call(self.ctx.mobile.status)
        if not status.enabled or not status.available:
            raise ValueError('ATTACH_ONLINE_CALLER_MOBILE')
        if status.platform.lower() != 'android':
            raise ValueError('NEXUS_ANDROID_MOBILE_REQUIRED')

    async def disconnect(self):
        self.invalidate_screenshot()
        self.stopped.set()

    async def screenshot(self):
        self.invalidate_screenshot()
        started = time.monotonic()
        self.capture_attempts += 1
        try:
            return await self._capture_screenshot()
        except BaseException:
            self.failed_captures += 1
            raise
        finally:
            self.capture_seconds += time.monotonic() - started

    async def _capture_screenshot(self):
        if self.console is not None:self.console.note('Waiting for a phone screenshot.')
        shot = await self.call(self.ctx.mobile.capture_screen, timeout=60)
        if not shot.width or not shot.height or not shot.content:
            raise ValueError('MOBILE_SCREEN_UNAVAILABLE')
        self.size = (shot.width, shot.height)
        self.frames += 1
        if self.console is not None:
            self.console.frame(shot.content, screenshots=self.frames, actions=self.actions,
                content_type=shot.content_type, width=shot.width, height=shot.height)
        # Nexus sends WebP; native Mobilerun accepts PNG/JPEG only. Keep the
        # original screenshot in the Background page and use lossless PNG for vision.
        with Image.open(io.BytesIO(shot.content)) as image:
            if image.size != self.size or image.width * image.height > 20000000:
                raise ValueError('INVALID_MOBILE_SCREEN_DIMENSIONS')
            output = io.BytesIO()
            image.convert('RGB').save(output, format='PNG')
            self._state_image = output.getvalue()
            return self._state_image

    def invalidate_screenshot(self):
        self._state_image = None

    async def consume_state_screenshot(self):
        if self.stopped.is_set(): raise ValueError('RUN_CANCELLED')
        image = self._state_image
        if image is None: image = await self.screenshot()
        self._state_image = None
        return image

    def point(self, x, y):
        if not self.size or any(type(v) not in (int, float) or not math.isfinite(v) for v in (x, y)):
            raise ValueError('OBSERVE_PHONE_BEFORE_ACTION')
        if not 0 <= x < self.size[0] or not 0 <= y < self.size[1]:
            raise ValueError('MOBILE_COORDINATES_OUT_OF_RANGE')
        # Nexus screenshots may be downscaled; Android maps normalized values
        # against the actual screen dimensions. Never send screenshot pixels as
        # physical pixels. Mobilerun already maps its model grid to this image.
        return float(x) / max(1, self.size[0] - 1), float(y) / max(1, self.size[1] - 1)

    async def tap(self, x, y):
        x, y = self.point(x, y)
        self.invalidate_screenshot()
        await self.call(self.ctx.mobile.tap, x=x, y=y, coordinate_space='normalized', timeout=120)
        self.actions += 1

    async def swipe(self, start_x, start_y, end_x, end_y, duration_ms=300):
        x, y = self.point(start_x, start_y); ex, ey = self.point(end_x, end_y)
        if type(duration_ms) not in (int, float) or not 50 <= duration_ms <= 5000:
            raise ValueError('INVALID_SWIPE_DURATION')
        self.invalidate_screenshot()
        if (x, y) == (ex, ey) and duration_ms >= 500:
            await self.call(self.ctx.mobile.long_press, x=x, y=y, duration_ms=int(duration_ms), coordinate_space='normalized', timeout=120)
        else:
            await self.call(lambda **kw: self.ctx.mobile.swipe(x, y, ex, ey, **kw), duration_ms=int(duration_ms), coordinate_space='normalized', timeout=120)
        self.actions += 1

    async def input_text(self, text, clear=False):
        if clear:
            raise ValueError('CLEAR_TEXT_NOT_SUPPORTED_BY_NEXUS_MOBILE')
        if not isinstance(text, str) or len(text) > 4000:
            raise ValueError('INVALID_MOBILE_TEXT')
        # Cloud retains its own high-risk-action confirmation policy.
        self.invalidate_screenshot()
        await self.call(lambda **kw: self.ctx.mobile.type_text(text, **kw), timeout=180)
        self.actions += 1
        return True

    async def press_button(self, button):
        operation = {'back': self.ctx.mobile.press_back, 'home': self.ctx.mobile.press_home}.get(button)
        if operation is None:
            raise ValueError('UNSUPPORTED_SYSTEM_BUTTON')
        self.invalidate_screenshot()
        await self.call(operation, timeout=120); self.actions += 1

    async def start_app(self, package):
        if not isinstance(package, str) or len(package) > 255 or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+', package):
            raise ValueError('ANDROID_PACKAGE_REQUIRED')
        self.invalidate_screenshot()
        await self.call(lambda **kw: self.ctx.mobile.open_app(package, **kw), timeout=120)
        self.actions += 1
        return 'App opened'

    async def get_date(self):
        return datetime.datetime.now(datetime.timezone.utc).isoformat()
