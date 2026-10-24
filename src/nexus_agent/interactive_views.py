"""Private interactive pages with outbound SSE actions and caller-bound access.

Handlers run on a daemon thread after the originating MCP call returns. Keep the
Agent process alive. Never put secrets in page state or handler return values.
"""
import asyncio
import json
import inspect
import secrets
import threading
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InteractiveViewError('View callback redirect refused.')


class InteractiveViewError(RuntimeError):
    pass


class InteractiveView:
    """Runtime handle. Only ``public_info`` may be returned in an MCP result."""
    def __init__(self, context, registration, handlers):
        self._context = context
        self.public_info = {k: v for k, v in registration.items() if k != '_runtime'}
        runtime = registration['_runtime']
        self._url, self._token = runtime['url'], runtime['token']
        origin, target = urlsplit(context.interaction_url), urlsplit(self._url)
        if target.scheme != 'https' or (target.hostname, target.port) != (origin.hostname, origin.port) or target.username or target.query or target.fragment:
            raise InteractiveViewError('Invalid runtime endpoint.')
        self._handlers = dict(handlers)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread = None
        self._response = None
        self._pending_completions = {}
        self._last_error = None

    def __repr__(self):
        return f'InteractiveView(view_id={self.public_info["view_id"]!r})'

    @property
    def url(self): return self.public_info['url']

    @property
    def view_id(self): return self.public_info['view_id']

    @property
    def last_error(self):
        """A sanitized connection error, never response bodies or credentials."""
        return self._last_error

    def _open(self, suffix='', payload=None, stream=False):
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        request = Request(self._url + suffix, data=body, headers={
            'Content-Type': 'application/json', 'Accept': 'text/event-stream' if stream else 'application/json',
            'X-Nexus-View-Token': self._token}, method='GET' if body is None else 'POST')
        # Managed runtimes already pin origins and refuse redirects. The fallback
        # must also refuse redirects rather than forwarding a runtime capability.
        if self._context._cloud_opener is not None:
            return self._context._open_cloud(request, timeout=20)
        return build_opener(_NoRedirect()).open(request, timeout=20)

    def _request(self, payload=None):
        with self._open(payload=payload) as response:
            value = json.loads(response.read(262145))
            return value.get('data', value)

    def set_state(self, state):
        """Replace browser-visible JSON state. Serialized per handle; SSE pushes it."""
        with self._lock:
            for attempt in range(3):
                try:
                    current = self._request()
                    result = self._request({'operation': 'update', 'state': state, 'revision': current['revision']})
                    return result['revision']
                except HTTPError as exc:
                    if exc.code == 409 and attempt < 2: continue
                    raise InteractiveViewError(f'View update rejected (HTTP {exc.code}).') from None
                except (OSError, ValueError):
                    raise InteractiveViewError('View update could not be delivered.') from None

    def cancel_task(self):
        """Request cancellation of this view's originating durable task only.

        Call from an explicit user action handler. Cloud revokes task delegates;
        the runtime must still observe its normal cooperative cancellation checks.
        """
        return self._request({'operation': 'cancel_task'})

    def start(self):
        """Start one outbound action stream; no inbound server or public port."""
        with self._lock:
            if self._thread: return self
            self._request({'operation': 'heartbeat'})
            self._thread = threading.Thread(target=self._listen, name='nexus-view-' + self.view_id[:8], daemon=True)
            self._thread.start()
        return self

    def _complete(self, request_id):
        payload = self._pending_completions[request_id]
        try:
            self._request(payload)
            del self._pending_completions[request_id]
        except HTTPError as exc:
            if exc.code in (400, 403, 404):
                del self._pending_completions[request_id]
            else: raise

    def _dispatch(self, action):
        rid = action['request_id']
        lease = secrets.token_urlsafe(32)
        claim = self._request({'operation': 'claim', 'request_id': rid, 'lease': lease})
        if not claim.get('claimed'): return
        try:
            # Do not propagate the finished Run's context or its model credentials.
            handler = self._handlers[action['name']]
            result = handler(action['arguments'], self)
            if len(json.dumps(result, allow_nan=False).encode()) > 32768:
                raise ValueError('Result too large')
            failed = False
        except Exception:
            # Exception messages may include local paths or API credentials.
            result, failed = {'error': 'Agent action failed.'}, True
        self._pending_completions[rid] = {'operation': 'complete', 'request_id': rid, 'lease': lease,
                                          'result': result, 'failed': failed}
        self._complete(rid)

    def _listen(self):
        backoff = 1
        while not self._stop.is_set():
            try:
                for rid in list(self._pending_completions): self._complete(rid)
                with self._open('events/', stream=True) as response:
                    self._response = response
                    event, data = '', []
                    while not self._stop.is_set():
                        raw = response.readline(1048577)
                        if not raw: break
                        if len(raw) > 1048576: raise ValueError('Oversized stream event')
                        line = raw.decode().rstrip('\r\n')
                        if line.startswith('event:'): event = line[6:].strip()
                        elif line.startswith('data:'): data.append(line[5:].strip())
                        elif not line:
                            if event == 'closed': self._stop.set(); break
                            if event == 'actions' and data:
                                for action in json.loads('\n'.join(data)).get('actions', []):
                                    if self._stop.is_set(): break
                                    self._dispatch(action)
                            event, data = '', []
                        self._last_error = None
                        backoff = 1
                self._response = None
            except HTTPError as exc:
                self._last_error = f'View stream rejected (HTTP {exc.code}).'
                if exc.code in (401, 403, 404): self._stop.set()
            except Exception:
                self._last_error = 'View connection interrupted; reconnecting.'
            if self._stop.wait(backoff): break
            backoff = min(backoff * 2, 15)

    def close(self):
        """Revoke both browser access and runtime delivery, then stop listening."""
        self._stop.set()
        try: self._request({'operation': 'close'})
        except HTTPError as exc:
            if exc.code != 404: raise InteractiveViewError(f'View close rejected (HTTP {exc.code}).') from None
        except (OSError, ValueError):
            raise InteractiveViewError('View close was not confirmed; access expires at its TTL.') from None


class InteractiveViews:
    def __init__(self, context): self.context = context

    def create(self, *, title, state=None, actions=None, handlers=None, html=None, ttl_seconds=3600):
        """Create a builtin panel, or sandboxed self-contained HTML.

        actions maps names to {label, input_schema}. handlers maps the same names
        to synchronous callables (arguments, view). Handlers should finish within
        30 seconds; start background jobs for longer work. Return public_info to
        the MCP caller, and keep the Agent process running for action delivery.
        """
        if urlsplit(self.context.interaction_url).scheme != 'https':
            raise InteractiveViewError('Interactive Views require an HTTPS Cloud Run.')
        actions, handlers = dict(actions or {}), dict(handlers or {})
        if set(actions) != set(handlers) or any(not callable(h) or inspect.iscoroutinefunction(h) for h in handlers.values()):
            raise ValueError('Every declared action must have exactly one callable handler.')
        registration = self.context._interaction_request('views/', method='POST', payload={
            'definition': {'title': title, 'template': 'custom' if html is not None else 'panel',
                           'html': html or '', 'actions': actions}, 'state': state or {}, 'ttl_seconds': ttl_seconds})
        view = InteractiveView(self.context, registration, handlers)
        try: return view.start()
        except Exception:
            try: view.close()
            except Exception: pass
            raise InteractiveViewError('Interactive View could not connect to Cloud.') from None

    def link(self, view_id):
        """Issue a new single-use link, only for this verified caller and Agent."""
        return self.context._interaction_request(f'views/{uuid.UUID(str(view_id))}/', method='POST', payload={'action': 'ticket'})

    def close(self, view_id):
        return self.context._interaction_request(f'views/{uuid.UUID(str(view_id))}/', method='POST', payload={'action': 'close'})


class AsyncInteractiveView:
    def __init__(self, view): self._view = view
    @property
    def public_info(self): return dict(self._view.public_info)
    @property
    def view_id(self): return self._view.view_id
    @property
    def url(self): return self._view.url
    async def set_state(self, state): return await asyncio.to_thread(self._view.set_state, state)
    async def close(self): return await asyncio.to_thread(self._view.close)
    async def cancel_task(self): return await asyncio.to_thread(self._view.cancel_task)


class AsyncInteractiveViews:
    """Async creation facade; action handlers remain ordinary synchronous functions."""
    def __init__(self, views): self._views = views
    async def create(self, **kwargs): return AsyncInteractiveView(await asyncio.to_thread(self._views.create, **kwargs))
    async def link(self, view_id): return await asyncio.to_thread(self._views.link, view_id)
    async def close(self, view_id): return await asyncio.to_thread(self._views.close, view_id)
