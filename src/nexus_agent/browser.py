"""Run-isolated Chrome automation for Nexus Agent Serving.

Playwright is imported lazily so the core SDK keeps zero mandatory
dependencies.  A single worker thread owns Playwright and one Chrome process;
each Nexus Run receives a separate BrowserContext and page.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
from dataclasses import dataclass, field
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit


class NexusBrowserError(RuntimeError):
    """A browser operation could not be completed."""


class NexusBrowserUnavailable(NexusBrowserError):
    """Playwright or a usable Chrome installation is unavailable."""


class NexusBrowserComputerRequired(NexusBrowserUnavailable):
    """The caller must attach a Computer before browser automation can start."""


class NexusBrowserPermissionRequired(NexusBrowserUnavailable):
    """The caller has not granted browser.control for this Run."""


class NexusBrowserTunnelUnavailable(NexusBrowserUnavailable):
    """Cloud could not establish the protected CDP tunnel."""


class NexusBrowserActionFailed(NexusBrowserError):
    """Chrome rejected or failed an action."""


class NexusBrowserStaleObservation(NexusBrowserActionFailed):
    """An element reference belongs to an older page observation."""


class NexusBrowserSessionLost(NexusBrowserError):
    """Chrome exited and the Run's page state could not be recovered."""


@dataclass(frozen=True)
class NexusBrowserNode:
    ref: str
    tag: str
    role: str = ""
    name: str = field(default="", repr=False)
    text: str = field(default="", repr=False)
    selector: str = field(default="", repr=False)
    bounds: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    disabled: bool = False
    checked: Optional[bool] = None
    selected: Optional[bool] = None
    expanded: Optional[bool] = None


@dataclass(frozen=True)
class NexusBrowserDOMSnapshot:
    revision: int
    nodes: Tuple[NexusBrowserNode, ...] = ()
    truncated: bool = False

    def by_ref(self, ref: str) -> Optional[NexusBrowserNode]:
        return next((node for node in self.nodes if node.ref == ref), None)


@dataclass(frozen=True)
class NexusBrowserObservation:
    observation_id: str
    revision: int
    url: str
    title: str
    viewport: Tuple[int, int]
    image: bytes = field(repr=False)
    content_type: str = "image/jpeg"
    dom: NexusBrowserDOMSnapshot = field(default_factory=lambda: NexusBrowserDOMSnapshot(0), repr=False)
    _html: str = field(default="", repr=False, compare=False)

    def html(self) -> str:
        """Return the sanitized, bounded HTML captured with this observation."""

        return self._html

    def __repr__(self) -> str:
        return (
            "NexusBrowserObservation("
            f"observation_id={self.observation_id!r}, revision={self.revision}, "
            f"viewport={self.viewport!r}, dom_nodes={len(self.dom.nodes)})"
        )


@dataclass(frozen=True)
class NexusBrowserAction:
    kind: str
    parameters: Mapping[str, Any] = field(default_factory=dict, repr=False)
    expected_revision: Optional[int] = None

    def __repr__(self) -> str:
        return (
            f"NexusBrowserAction(kind={self.kind!r}, "
            f"expected_revision={self.expected_revision!r})"
        )


@dataclass
class _RunBrowserState:
    context: Any
    page: Any
    viewport: Tuple[int, int]
    revision: int = 0
    last_used: float = field(default_factory=time.monotonic)
    active: int = 0


@dataclass
class _WorkItem:
    operation: Callable[["_BrowserWorker"], Any]
    result: "queue.Queue[Tuple[bool, Any]]"


_SEMANTIC_DOM_SCRIPT = r"""
() => {
  const MAX = 2000;
  window.__nexusBrowserCounter = window.__nexusBrowserCounter || 0;
  const candidates = Array.from(document.querySelectorAll(
    'a,button,input,textarea,select,option,[role],[contenteditable="true"],summary,label,h1,h2,h3,h4,h5,h6,p,li,td,th'
  ));
  const nodes = [];
  for (const element of candidates) {
    if (nodes.length >= MAX) break;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    if (rect.width <= 0 || rect.height <= 0 || style.visibility === 'hidden' || style.display === 'none') continue;
    let ref = element.getAttribute('data-nexus-browser-ref');
    if (!ref) {
      ref = 'e' + (++window.__nexusBrowserCounter);
      element.setAttribute('data-nexus-browser-ref', ref);
    }
    const tag = element.tagName.toLowerCase();
    const role = element.getAttribute('role') || ({a:'link',button:'button',input:'textbox',textarea:'textbox',select:'combobox',option:'option'}[tag] || '');
    const sensitive = tag === 'input' && ['password','hidden'].includes((element.getAttribute('type') || 'text').toLowerCase());
    const rawText = (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim();
    const value = !sensitive && 'value' in element ? String(element.value || '') : '';
    const name = element.getAttribute('aria-label') || element.getAttribute('alt') || element.getAttribute('placeholder') || rawText || value;
    const nullable = (name) => element.hasAttribute(name) ? element.getAttribute(name) === 'true' : null;
    nodes.push({
      ref, tag, role,
      name: String(name || '').slice(0, 500),
      text: String(rawText || value || '').slice(0, 1000),
      selector: '[data-nexus-browser-ref="' + ref + '"]',
      bounds: [rect.x, rect.y, rect.width, rect.height],
      disabled: Boolean(element.disabled) || element.getAttribute('aria-disabled') === 'true',
      checked: 'checked' in element ? Boolean(element.checked) : nullable('aria-checked'),
      selected: 'selected' in element ? Boolean(element.selected) : nullable('aria-selected'),
      expanded: nullable('aria-expanded')
    });
  }
  return {nodes, truncated: candidates.length > MAX};
}
"""


_SANITIZED_HTML_SCRIPT = r"""
() => {
  const clone = document.documentElement.cloneNode(true);
  clone.querySelectorAll('script,style,noscript,template,iframe,object,embed').forEach(node => node.remove());
  const comments = [];
  const walker = document.createTreeWalker(clone, NodeFilter.SHOW_COMMENT);
  while (walker.nextNode()) comments.push(walker.currentNode);
  comments.forEach(node => node.remove());
  const sensitive = /(token|secret|password|passwd|authorization|cookie|session|api[-_]?key)/i;
  const dangerous = /^\s*(javascript|data|vbscript|file|chrome|devtools):/i;
  const urlAttributes = new Set(['href', 'src', 'action', 'formaction', 'poster']);
  clone.querySelectorAll('*').forEach(element => {
    element.removeAttribute('data-nexus-browser-ref');
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase();
      if (name.startsWith('on') || name === 'srcdoc' || sensitive.test(name)) {
        element.removeAttribute(attribute.name);
      } else if (urlAttributes.has(name) && (dangerous.test(attribute.value) || sensitive.test(attribute.value))) {
        element.removeAttribute(attribute.name);
      }
    }
    if (element.tagName === 'INPUT') {
      const type = (element.getAttribute('type') || 'text').toLowerCase();
      if (type === 'password' || type === 'hidden' || sensitive.test(element.getAttribute('name') || '')) {
        element.setAttribute('value', '[REDACTED]');
      }
    }
  });
  return '<!doctype html>\n' + clone.outerHTML;
}
"""


def _discover_chrome() -> Optional[str]:
    configured = str(os.environ.get("NEXUS_BROWSER_EXECUTABLE") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise NexusBrowserUnavailable("Configured Chrome executable is unavailable")
        return str(path)
    candidates = []
    if sys.platform == "win32":
        candidates.extend(
            [
                Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
                Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
            ]
        )
    elif sys.platform == "darwin":
        candidates.extend([
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            Path.home() / "Applications/Chromium.app/Contents/MacOS/Chromium",
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ])
    else:
        for name in (
            "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
            "microsoft-edge", "microsoft-edge-stable",
        ):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
    return next((str(path) for path in candidates if path.is_file()), None)


def _headless_default() -> bool:
    return str(os.environ.get("NEXUS_BROWSER_HEADLESS", "true")).strip().lower() not in {
        "0", "false", "no",
    }


class _BrowserWorker:
    def __init__(self) -> None:
        self.queue: "queue.Queue[Optional[_WorkItem]]" = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="nexus-browser-worker", daemon=True)
        self.ready = threading.Event()
        self.start_error: Optional[BaseException] = None
        self.playwright: Any = None
        self.browser: Any = None
        self.states: Dict[str, _RunBrowserState] = {}
        self.generation = 0
        self.idle_seconds = max(float(os.environ.get("NEXUS_BROWSER_IDLE_SECONDS", "3600")), 60.0)
        self.max_contexts = max(int(os.environ.get("NEXUS_BROWSER_MAX_CONTEXTS", "8")), 1)
        self.thread.start()

    def _run(self) -> None:
        try:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise NexusBrowserUnavailable(
                    "Browser automation requires nexilume[browser]"
                ) from exc
            self.playwright = sync_playwright().start()
            self._launch_browser()
        except BaseException as exc:
            self.start_error = exc
        finally:
            self.ready.set()
        if self.start_error is not None:
            return
        while True:
            try:
                item = self.queue.get(timeout=5.0)
            except queue.Empty:
                self._cleanup_idle()
                continue
            if item is None:
                break
            try:
                self._cleanup_idle()
                item.result.put((True, item.operation(self)))
            except BaseException as exc:
                item.result.put((False, self._safe_error(exc)))
        self._shutdown()

    def _safe_error(self, exc: BaseException) -> BaseException:
        if isinstance(exc, NexusBrowserError):
            return exc
        if self.browser is not None and not self.browser.is_connected():
            try:
                self._restart_browser()
            except Exception:
                pass
            return NexusBrowserSessionLost("Chrome exited; the Run browser state was lost")
        # Playwright errors can contain selectors, page text and target URLs.  Do
        # not let those values escape through an SDK exception or application log.
        return NexusBrowserActionFailed("Chrome could not complete the browser action")

    def _launch_browser(self) -> None:
        launch: Dict[str, Any] = {"headless": _headless_default()}
        executable = _discover_chrome()
        if executable:
            launch["executable_path"] = executable
        if sys.platform.startswith("linux"):
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                raise NexusBrowserUnavailable(
                    "Attached browser requires Nexus Computer Runtime to run as a non-root Linux user"
                )
            launch["args"] = ["--disable-dev-shm-usage"]
        self.browser = self.playwright.chromium.launch(**launch)
        self.generation += 1

    def _restart_browser(self) -> None:
        for run_id in list(self.states):
            self._close_state(run_id)
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:
            pass
        self._launch_browser()

    def call(self, operation: Callable[["_BrowserWorker"], Any]) -> Any:
        self.ready.wait(timeout=30.0)
        if self.start_error is not None:
            error = self.start_error
            if isinstance(error, NexusBrowserError):
                raise error
            raise NexusBrowserUnavailable("Chrome browser worker could not start") from error
        if not self.thread.is_alive():
            raise NexusBrowserSessionLost("Chrome browser worker is not running")
        result: "queue.Queue[Tuple[bool, Any]]" = queue.Queue(maxsize=1)
        self.queue.put(_WorkItem(operation=operation, result=result))
        ok, value = result.get()
        if ok:
            return value
        raise value

    def state(self, run_id: str, viewport: Tuple[int, int]) -> _RunBrowserState:
        state = self.states.get(run_id)
        if state is not None:
            state.last_used = time.monotonic()
            return state
        if len(self.states) >= self.max_contexts:
            idle = sorted(
                (item for item in self.states.items() if item[1].active == 0),
                key=lambda item: item[1].last_used,
            )
            if not idle:
                raise NexusBrowserUnavailable("All Agent browser contexts are busy")
            self._close_state(idle[0][0])
        width, height = viewport
        context = self.browser.new_context(viewport={"width": width, "height": height})
        page = context.new_page()
        state = _RunBrowserState(context=context, page=page, viewport=viewport)
        self.states[run_id] = state
        return state

    def _cleanup_idle(self) -> None:
        cutoff = time.monotonic() - self.idle_seconds
        for run_id, state in list(self.states.items()):
            if state.active == 0 and state.last_used < cutoff:
                self._close_state(run_id)

    def _close_state(self, run_id: str) -> None:
        state = self.states.pop(run_id, None)
        if state is not None:
            try:
                state.context.close()
            except Exception:
                pass

    def _shutdown(self) -> None:
        for run_id in list(self.states):
            self._close_state(run_id)
        try:
            if self.browser is not None:
                self.browser.close()
        finally:
            if self.playwright is not None:
                self.playwright.stop()

    def close(self) -> None:
        if self.thread.is_alive():
            self.queue.put(None)
            self.thread.join(timeout=10.0)


_worker_lock = threading.Lock()
_worker: Optional[_BrowserWorker] = None


def _browser_worker() -> _BrowserWorker:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.thread.is_alive():
            _worker = _BrowserWorker()
        return _worker


def close_browser_worker() -> None:
    global _worker
    with _worker_lock:
        worker, _worker = _worker, None
    if worker is not None:
        worker.close()


atexit.register(close_browser_worker)


def _validated_viewport(viewport: Sequence[int]) -> Tuple[int, int]:
    if len(viewport) != 2:
        raise ValueError("browser viewport requires width and height")
    width, height = int(viewport[0]), int(viewport[1])
    if not 320 <= width <= 3840 or not 240 <= height <= 2160:
        raise ValueError("browser viewport is outside supported bounds")
    return width, height


def _validate_http_url(url: str) -> str:
    value = str(url or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise NexusBrowserActionFailed("Browser navigation requires an HTTP or HTTPS URL")
    return value


def _capture_viewport_image(page: Any) -> bytes:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    for quality in (75, 45, 25, 10):
        try:
            image = page.screenshot(
                type="jpeg",
                quality=quality,
                full_page=False,
                timeout=5000,
            )
        except PlaywrightTimeoutError as exc:
            raise NexusBrowserActionFailed(
                "Browser viewport did not become ready for capture"
            ) from exc
        if len(image) <= 512 * 1024:
            return bytes(image)
    raise NexusBrowserActionFailed("Browser viewport image exceeds the Display limit")


def _snapshot(state: _RunBrowserState) -> NexusBrowserObservation:
    state.page.wait_for_timeout(100)
    state.revision += 1
    semantic = state.page.evaluate(_SEMANTIC_DOM_SCRIPT)
    html = str(state.page.evaluate(_SANITIZED_HTML_SCRIPT) or "")[: 512 * 1024]
    image = _capture_viewport_image(state.page)
    nodes = tuple(
        NexusBrowserNode(
            ref=str(item.get("ref") or ""),
            tag=str(item.get("tag") or ""),
            role=str(item.get("role") or ""),
            name=str(item.get("name") or ""),
            text=str(item.get("text") or ""),
            selector=str(item.get("selector") or ""),
            bounds=tuple(float(value or 0) for value in item.get("bounds", [0, 0, 0, 0]))[:4],
            disabled=bool(item.get("disabled")),
            checked=item.get("checked") if isinstance(item.get("checked"), bool) else None,
            selected=item.get("selected") if isinstance(item.get("selected"), bool) else None,
            expanded=item.get("expanded") if isinstance(item.get("expanded"), bool) else None,
        )
        for item in list(semantic.get("nodes") or [])[:2000]
        if isinstance(item, Mapping) and item.get("ref")
    )
    state.last_used = time.monotonic()
    return NexusBrowserObservation(
        observation_id=str(uuid.uuid4()),
        revision=state.revision,
        url=str(state.page.url or ""),
        title=str(state.page.title() or "")[:512],
        viewport=state.viewport,
        image=bytes(image),
        content_type="image/jpeg",
        dom=NexusBrowserDOMSnapshot(
            revision=state.revision,
            nodes=nodes,
            truncated=bool(semantic.get("truncated")),
        ),
        _html=html,
    )


class NexusBrowserLocator:
    def __init__(
        self,
        session: "NexusBrowserSession",
        target: str,
        *,
        is_ref: bool,
        revision: Optional[int],
    ) -> None:
        self._session = session
        self._target = str(target or "").strip()
        self._is_ref = bool(is_ref)
        self._revision = revision
        if not self._target:
            raise ValueError("browser locator target is required")

    def click(self, *, timeout: float = 30.0) -> NexusBrowserObservation:
        return self._session.perform(NexusBrowserAction(
            "locator_click",
            {"target": self._target, "is_ref": self._is_ref, "timeout": timeout},
            self._revision,
        ))

    def fill(self, value: str, *, timeout: float = 30.0) -> NexusBrowserObservation:
        return self._session.perform(NexusBrowserAction(
            "fill",
            {"target": self._target, "is_ref": self._is_ref, "value": str(value), "timeout": timeout},
            self._revision,
        ))

    def select(self, value: str, *, timeout: float = 30.0) -> NexusBrowserObservation:
        return self._session.perform(NexusBrowserAction(
            "select",
            {"target": self._target, "is_ref": self._is_ref, "value": str(value), "timeout": timeout},
            self._revision,
        ))


class NexusBrowserSession:
    def __init__(
        self,
        *,
        run_id: str,
        publisher: Callable[[NexusBrowserObservation, str, str], None],
        viewport: Sequence[int] = (1280, 720),
    ) -> None:
        self.run_id = str(run_id or "").strip()
        if not self.run_id:
            raise NexusBrowserUnavailable("Browser Session requires a Nexus Run")
        self.viewport = _validated_viewport(viewport)
        self._publisher = publisher
        self._revision: Optional[int] = None

    @property
    def revision(self) -> Optional[int]:
        return self._revision

    def _execute(self, operation: Callable[[_RunBrowserState], NexusBrowserObservation], action: str) -> NexusBrowserObservation:
        def run(worker: _BrowserWorker) -> NexusBrowserObservation:
            state = worker.state(self.run_id, self.viewport)
            state.active += 1
            try:
                return operation(state)
            finally:
                state.active -= 1
                state.last_used = time.monotonic()

        observation = _browser_worker().call(run)
        self._revision = observation.revision
        self._publisher(observation, action, "succeeded")
        return observation

    def open(self, url: str, *, timeout: float = 30.0) -> NexusBrowserObservation:
        target = _validate_http_url(url)

        def navigate(state: _RunBrowserState) -> NexusBrowserObservation:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            timeout_ms = max(int(timeout * 1000), 1)
            try:
                # A committed document is already observable. Some sites keep
                # loading scripts or subresources long enough that waiting for
                # DOMContentLoaded turns a usable page into an action failure.
                state.page.goto(target, wait_until="commit", timeout=timeout_ms)
            except PlaywrightTimeoutError as exc:
                raise NexusBrowserActionFailed(
                    "Browser navigation timed out before the server responded"
                ) from exc
            try:
                state.page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=min(timeout_ms, 5000),
                )
            except PlaywrightTimeoutError:
                state.page.evaluate("window.stop()")
            return _snapshot(state)

        return self._execute(navigate, "navigate")

    def observe(self) -> NexusBrowserObservation:
        return self._execute(_snapshot, "observe")

    def locator(self, target: str, *, ref: bool = False) -> NexusBrowserLocator:
        return NexusBrowserLocator(
            self,
            target,
            is_ref=ref,
            revision=self._revision,
        )

    def element(self, ref: str) -> NexusBrowserLocator:
        return self.locator(ref, ref=True)

    def perform(self, action: NexusBrowserAction) -> NexusBrowserObservation:
        if not isinstance(action, NexusBrowserAction):
            raise TypeError("browser action must be NexusBrowserAction")
        kind = str(action.kind or "").strip().lower()
        parameters = dict(action.parameters or {})

        def apply(state: _RunBrowserState) -> NexusBrowserObservation:
            if action.expected_revision is not None and action.expected_revision != state.revision:
                raise NexusBrowserStaleObservation("Browser observation is stale; observe the page again")
            timeout_ms = int(float(parameters.get("timeout", 30.0)) * 1000)
            if kind in {"locator_click", "fill", "select"}:
                target = str(parameters.get("target") or "")
                selector = (
                    f'[data-nexus-browser-ref="{target}"]'
                    if parameters.get("is_ref")
                    else target
                )
                locator = state.page.locator(selector)
                if kind == "locator_click":
                    locator.click(timeout=timeout_ms)
                elif kind == "fill":
                    locator.fill(str(parameters.get("value") or ""), timeout=timeout_ms)
                else:
                    locator.select_option(str(parameters.get("value") or ""), timeout=timeout_ms)
            elif kind == "click":
                x, y = self._viewport_point(state, parameters["x"], parameters["y"])
                state.page.mouse.click(x, y)
            elif kind == "scroll":
                state.page.mouse.wheel(float(parameters.get("delta_x", 0)), float(parameters.get("delta_y", 0)))
            elif kind == "type":
                state.page.keyboard.type(str(parameters.get("text") or ""))
            elif kind == "drag":
                start_x, start_y = self._viewport_point(
                    state, parameters["from_x"], parameters["from_y"]
                )
                end_x, end_y = self._viewport_point(
                    state, parameters["to_x"], parameters["to_y"]
                )
                state.page.mouse.move(start_x, start_y)
                state.page.mouse.down()
                state.page.mouse.move(end_x, end_y, steps=max(int(parameters.get("steps", 10)), 1))
                state.page.mouse.up()
            elif kind == "reload":
                state.page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
            else:
                raise NexusBrowserActionFailed("Unsupported browser action")
            return _snapshot(state)

        return self._execute(apply, kind)

    @staticmethod
    def _viewport_point(
        state: _RunBrowserState,
        x_value: Any,
        y_value: Any,
    ) -> Tuple[float, float]:
        x, y = float(x_value), float(y_value)
        width, height = state.viewport
        if not 0 <= x < width or not 0 <= y < height:
            raise NexusBrowserActionFailed("Browser coordinates are outside the viewport")
        return x, y

    def click(self, *, x: float, y: float) -> NexusBrowserObservation:
        return self.perform(NexusBrowserAction(
            "click", {"x": x, "y": y}, expected_revision=self._revision
        ))

    def scroll(self, *, delta_y: float, delta_x: float = 0) -> NexusBrowserObservation:
        return self.perform(NexusBrowserAction(
            "scroll",
            {"delta_x": delta_x, "delta_y": delta_y},
            expected_revision=self._revision,
        ))

    def type(self, text: str) -> NexusBrowserObservation:
        return self.perform(NexusBrowserAction(
            "type", {"text": str(text)}, expected_revision=self._revision
        ))

    def drag(
        self,
        *,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        steps: int = 10,
    ) -> NexusBrowserObservation:
        return self.perform(NexusBrowserAction(
            "drag",
            {
                "from_x": from_x,
                "from_y": from_y,
                "to_x": to_x,
                "to_y": to_y,
                "steps": steps,
            },
            expected_revision=self._revision,
        ))

    def close(self) -> None:
        _browser_worker().call(lambda worker: worker._close_state(self.run_id))


class NexusAttachedBrowserSession(NexusBrowserSession):
    """Run-scoped browser hosted by the caller's Attached Computer."""

    def __init__(
        self,
        *,
        run_id: str,
        requester: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
        viewport: Sequence[int] = (1280, 720),
    ) -> None:
        super().__init__(run_id=run_id, publisher=lambda *_args: None, viewport=viewport)
        self._requester = requester

    def _remote(self, operation: str, payload: Mapping[str, Any]) -> NexusBrowserObservation:
        value = self._requester(
            operation,
            {"viewport": list(self.viewport), **dict(payload)},
        )
        image_value = str(value.get("image_base64") or "")
        try:
            image = base64.b64decode(image_value, validate=True) if image_value else b""
        except (ValueError, TypeError) as exc:
            raise NexusBrowserSessionLost("Attached Computer returned an invalid browser observation") from exc
        dom_value = value.get("dom") if isinstance(value.get("dom"), Mapping) else {}
        nodes = []
        for item in dom_value.get("nodes") or ():
            if not isinstance(item, Mapping):
                continue
            bounds = item.get("bounds")
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
                bounds = (0, 0, 0, 0)
            nodes.append(NexusBrowserNode(
                ref=str(item.get("ref") or "")[:128],
                tag=str(item.get("tag") or "")[:64],
                role=str(item.get("role") or "")[:64],
                name=str(item.get("name") or "")[:500],
                text=str(item.get("text") or "")[:1000],
                selector=str(item.get("selector") or "")[:256],
                bounds=tuple(float(part) for part in bounds),
                disabled=bool(item.get("disabled")),
                checked=item.get("checked") if isinstance(item.get("checked"), bool) else None,
                selected=item.get("selected") if isinstance(item.get("selected"), bool) else None,
                expanded=item.get("expanded") if isinstance(item.get("expanded"), bool) else None,
            ))
        viewport = value.get("viewport")
        if not isinstance(viewport, (list, tuple)) or len(viewport) != 2:
            viewport = self.viewport
        observation = NexusBrowserObservation(
            observation_id=str(value.get("observation_id") or ""),
            revision=max(int(value.get("revision") or 0), 0),
            url=str(value.get("url") or ""),
            title=str(value.get("title") or ""),
            viewport=(int(viewport[0]), int(viewport[1])),
            image=image,
            content_type=str(value.get("content_type") or "image/jpeg"),
            dom=NexusBrowserDOMSnapshot(
                revision=max(int(value.get("revision") or 0), 0),
                nodes=tuple(nodes),
                truncated=bool(dom_value.get("truncated")),
            ),
            _html=str(value.get("html") or "")[:262144],
        )
        self._revision = observation.revision
        return observation

    def open(self, url: str, *, timeout: float = 30.0) -> NexusBrowserObservation:
        return self._remote("open", {"url": _validate_http_url(url), "timeout": timeout})

    def observe(self) -> NexusBrowserObservation:
        return self._remote("observe", {})

    def perform(self, action: NexusBrowserAction) -> NexusBrowserObservation:
        if not isinstance(action, NexusBrowserAction):
            raise TypeError("browser action must be NexusBrowserAction")
        return self._remote("action", {
            "action": {
                "kind": str(action.kind or ""),
                "parameters": dict(action.parameters or {}),
                "expected_revision": action.expected_revision,
            }
        })

    def close(self) -> None:
        self._requester("close", {})
        self._revision = None


class NexusAsyncBrowserLocator:
    def __init__(self, sync: NexusBrowserLocator) -> None:
        self._sync = sync

    async def click(self, **options: Any) -> NexusBrowserObservation:
        return await asyncio.to_thread(self._sync.click, **options)

    async def fill(self, value: str, **options: Any) -> NexusBrowserObservation:
        return await asyncio.to_thread(self._sync.fill, value, **options)

    async def select(self, value: str, **options: Any) -> NexusBrowserObservation:
        return await asyncio.to_thread(self._sync.select, value, **options)


class NexusAsyncBrowserSession:
    def __init__(self, sync: NexusBrowserSession) -> None:
        self._sync = sync

    @property
    def revision(self) -> Optional[int]:
        return self._sync.revision

    async def open(self, url: str, **options: Any) -> NexusBrowserObservation:
        return await asyncio.to_thread(self._sync.open, url, **options)

    async def observe(self) -> NexusBrowserObservation:
        return await asyncio.to_thread(self._sync.observe)

    def locator(self, target: str, *, ref: bool = False) -> NexusAsyncBrowserLocator:
        return NexusAsyncBrowserLocator(self._sync.locator(target, ref=ref))

    def element(self, ref: str) -> NexusAsyncBrowserLocator:
        return NexusAsyncBrowserLocator(self._sync.element(ref))

    async def perform(self, action: NexusBrowserAction) -> NexusBrowserObservation:
        return await asyncio.to_thread(self._sync.perform, action)

    async def click(self, **options: Any) -> NexusBrowserObservation:
        return await asyncio.to_thread(self._sync.click, **options)

    async def scroll(self, **options: Any) -> NexusBrowserObservation:
        return await asyncio.to_thread(self._sync.scroll, **options)

    async def type(self, text: str) -> NexusBrowserObservation:
        return await asyncio.to_thread(self._sync.type, text)

    async def drag(self, **options: Any) -> NexusBrowserObservation:
        return await asyncio.to_thread(self._sync.drag, **options)

    async def close(self) -> None:
        await asyncio.to_thread(self._sync.close)
