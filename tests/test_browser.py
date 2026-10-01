import asyncio
import base64
import hashlib
import os
from pathlib import Path
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nexus_agent import (  # noqa: E402
    NexusAgentClient,
    NexusAgentServer,
    NexusAsyncBrowserSession,
    NexusBrowserAction,
    NexusBrowserActionFailed,
    NexusBrowserNode,
    NexusBrowserObservation,
    NexusBrowserSession,
    NexusBrowserSessionLost,
    NexusBrowserStaleObservation,
)
from nexus_agent.browser import (  # noqa: E402
    _browser_worker,
    _capture_viewport_image,
    close_browser_worker,
)


PAGE = b"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Nexus Browser Scenario</title>
  <style>
    body { font-family: sans-serif; margin: 24px; }
    label, input, select, button { display: block; margin: 10px 0; }
    #spacer { height: 700px; }
    #drag-source, #drop-target { display: inline-flex; width: 130px; height: 60px;
      align-items: center; justify-content: center; border: 2px solid #17372b; margin-right: 80px; }
  </style>
</head>
<body>
  <h1>Browser loop</h1>
  <label>Name <input id="name" aria-label="Name" /></label>
  <label>Model <select id="model" aria-label="Model">
    <option value="gpt-4">GPT-4</option><option value="gpt-5">GPT-5</option>
  </select></label>
  <button id="submit" onclick="document.querySelector('#result').textContent =
    document.querySelector('#name').value + ':' + document.querySelector('#model').value">Submit</button>
  <button id="gui-click" onclick="this.textContent='GUI clicked'">GUI target</button>
  <p id="result">Pending</p>
  <!-- private browser comment -->
  <a href="javascript:alert('unsafe')">Unsafe link</a>
  <input type="password" name="password" value="do-not-expose" />
  <script>window.forbiddenSecret = 'do-not-expose';</script>
  <div id="spacer"></div>
  <input id="type-target" aria-label="Type target" />
  <div id="drag-source" role="button">Drag me</div><div id="drop-target" role="button">Drop here</div>
  <script>
    let dragging = false;
    document.querySelector('#drag-source').addEventListener('pointerdown', () => { dragging = true; });
    document.addEventListener('pointerup', event => {
      if (!dragging) return;
      dragging = false;
      const target = document.elementFromPoint(event.clientX, event.clientY);
      if (target && target.id === 'drop-target') target.textContent = 'Dropped';
    });
  </script>
</body>
</html>"""


class ScenarioHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/slow":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<!doctype html><html><head><title>Slow committed page</title></head>"
                b"<body><button>Continue</button>"
            )
            self.wfile.flush()
            # Keep the document open long enough to exceed the Browser's DOM
            # readiness grace period after navigation has already committed.
            threading.Event().wait(2)
            self.wfile.write(b"</body></html>")
            return
        if self.path != "/scenario":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, _format, *_args):
        return


def envelope(url):
    return {
        "version": "1.0",
        "intent": "browser.inspect",
        "intent_version": 1,
        "task_id": "browser-scenario",
        "source_agent": "agent://tests/browser",
        "tenant": "tests",
        "hop_limit": 8,
        "payload": {"url": url},
    }


def node_center(observation, selector):
    node = next(item for item in observation.dom.nodes if item.selector == selector)
    x, y, width, height = node.bounds
    return x + width / 2, y + height / 2


class BrowserValueSafetyTest(unittest.TestCase):
    def test_sensitive_browser_values_are_not_in_repr(self):
        action = NexusBrowserAction("fill", {"target": "#secret", "value": "private-value"})
        node = NexusBrowserNode("e1", "input", name="private-name", text="private-text", selector="#secret")
        observation = NexusBrowserObservation(
            "observation-1", 1, "https://user:pass@example.test/?token=private",
            "Private title", (1280, 720), image=b"jpeg",
        )

        self.assertNotIn("private-value", repr(action))
        self.assertNotIn("private-name", repr(node))
        self.assertNotIn("private-text", repr(node))
        self.assertNotIn("user:pass", repr(observation))
        self.assertNotIn("token=private", repr(observation))

    def test_screenshot_timeout_is_bounded_and_actionable(self):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        page = mock.Mock()
        page.screenshot.side_effect = PlaywrightTimeoutError("screenshot did not settle")

        with self.assertRaisesRegex(
            NexusBrowserActionFailed,
            "Browser viewport did not become ready for capture",
        ):
            _capture_viewport_image(page)


@unittest.skipUnless(
    os.environ.get("NEXUS_REAL_BROWSER_TEST") == "1",
    "set NEXUS_REAL_BROWSER_TEST=1 to require a real Chrome scenario",
)
class AgentServingChromeScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), ScenarioHandler)
        cls.site_thread = threading.Thread(target=cls.site.serve_forever, daemon=True)
        cls.site_thread.start()
        cls.url = f"http://127.0.0.1:{cls.site.server_port}/scenario"
        cls.slow_url = f"http://127.0.0.1:{cls.site.server_port}/slow"

        cls.agent = NexusAgentServer(
            "127.0.0.1", 0,
            path="/agent/v1/invoke",
            stream_path="/agent/v1/invoke-stream",
        )

        @cls.agent.handler("browser.inspect")
        def inspect(request):
            browser = request.run_context.browser.session(viewport=(900, 640))
            observations = []
            first = browser.open(request.payload["url"])
            observations.append(first)

            stale = browser.element(next(node.ref for node in first.dom.nodes if node.tag == "button"))
            current = browser.locator("#name").fill("Nexus")
            observations.append(current)
            stale_rejected = False
            try:
                stale.click()
            except NexusBrowserStaleObservation:
                stale_rejected = True

            current = browser.locator("#model").select("gpt-5")
            observations.append(current)
            current = browser.locator("#submit").click()
            observations.append(current)

            gui_selector = next(
                node.selector for node in current.dom.nodes if node.name == "GUI target"
            )
            x, y = node_center(current, gui_selector)
            current = browser.click(x=x, y=y)
            observations.append(current)
            current = browser.scroll(delta_y=760)
            observations.append(current)
            current = browser.locator("#type-target").click()
            observations.append(current)
            current = browser.type("Hello from GUI")
            observations.append(current)

            source = next(node for node in current.dom.nodes if node.text == "Drag me")
            target = next(node for node in current.dom.nodes if node.text == "Drop here")
            sx, sy = source.bounds[0] + source.bounds[2] / 2, source.bounds[1] + source.bounds[3] / 2
            tx, ty = target.bounds[0] + target.bounds[2] / 2, target.bounds[1] + target.bounds[3] / 2
            current = browser.drag(from_x=sx, from_y=sy, to_x=tx, to_y=ty)
            observations.append(current)

            return {
                "revisions": [item.revision for item in observations],
                "result": next(node.text for node in current.dom.nodes if node.ref and node.text == "Nexus:gpt-5"),
                "typed": next(node.text for node in current.dom.nodes if node.name == "Type target"),
                "dropped": any(node.text == "Dropped" for node in current.dom.nodes),
                "stale_rejected": stale_rejected,
                "sanitized_html": (
                    "<script" not in current.html().lower()
                    and "do-not-expose" not in current.html()
                    and "private browser comment" not in current.html()
                    and "javascript:" not in current.html().lower()
                ),
            }

        cls.agent_thread = cls.agent.serve_in_thread()
        cls.client = NexusAgentClient(
            f"http://127.0.0.1:{cls.agent.port}",
            auth="none",
            timeout=60,
            use_environment_proxy=False,
        )

    @classmethod
    def tearDownClass(cls):
        cls.agent.shutdown()
        cls.agent.server_close()
        cls.agent_thread.join(timeout=3)
        cls.site.shutdown()
        cls.site.server_close()
        cls.site_thread.join(timeout=3)
        close_browser_worker()

    def test_real_agent_serving_handler_closes_browser_observation_loop(self):
        events = list(self.client.invoke_interactive(envelope(self.url)))
        self.assertTrue(
            any(item.event == "result" for item in events),
            [(item.event, item.data) for item in events],
        )
        result = next(item.data["result"] for item in events if item.event == "result")
        frames = [
            item for item in events
            if item.event == "display"
            and item.data.get("event", {}).get("name") == "nexus.computer.frame"
        ]

        self.assertEqual(result["revisions"], list(range(1, 10)))
        self.assertEqual(result["result"], "Nexus:gpt-5")
        self.assertEqual(result["typed"], "Hello from GUI")
        self.assertTrue(result["dropped"])
        self.assertTrue(result["stale_rejected"])
        self.assertTrue(result["sanitized_html"])
        self.assertEqual(len(frames), 9)

        task = frames[0]._task
        self.assertIsNotNone(task)
        image_hashes = set()
        frame_images = []
        for revision, frame in enumerate(frames, start=1):
            value = frame.data["event"]["value"]
            self.assertEqual(value["revision"], revision)
            self.assertGreater(value["dom_node_count"], 0)
            self.assertNotIn("html", value)
            self.assertNotIn("nodes", value)
            content = task.asset(value["frame_id"])
            self.assertTrue(content.startswith(b"\xff\xd8\xff"))
            frame_images.append(base64.b64encode(content).decode("ascii"))
            image_hashes.add(hashlib.sha256(content).hexdigest())
        self.assertGreaterEqual(len(image_hashes), 6)

        def decode_frames(worker):
            # JPEG byte size varies by platform/fonts. Check actual decoded
            # pixels, not an arbitrary compressed-size threshold.
            page = worker.browser.new_page()
            try:
                return page.evaluate("""async images => {
                    return Promise.all(images.map(async encoded => {
                        const response = await fetch('data:image/jpeg;base64,' + encoded);
                        const image = await createImageBitmap(await response.blob());
                        try {
                            const canvas = new OffscreenCanvas(image.width, image.height);
                            const ctx = canvas.getContext('2d');
                            ctx.drawImage(image, 0, 0);
                            const pixels = ctx.getImageData(0, 0, image.width, image.height).data;
                            const nonuniform = pixels.some((value, i) =>
                                i % 4 !== 3 && value !== pixels[i % 4]);
                            return [image.width, image.height, nonuniform];
                        } finally { image.close(); }
                    }));
                }""", frame_images)
            finally:
                page.close()

        self.assertEqual(_browser_worker().call(decode_frames), [[900, 640, True]] * 9)

    def test_committed_page_remains_usable_when_dom_content_loaded_stalls(self):
        session = NexusBrowserSession(
            run_id="slow-committed-run",
            publisher=lambda *_args: None,
            viewport=(900, 640),
        )
        try:
            observation = session.open(self.slow_url, timeout=0.25)
        finally:
            session.close()

        self.assertEqual(observation.title, "Slow committed page")
        self.assertTrue(any(node.name == "Continue" for node in observation.dom.nodes))

    def test_run_context_is_reused_and_other_runs_are_isolated(self):
        frames = []
        publisher = lambda observation, action, status: frames.append(
            (observation.revision, action, status)
        )
        first = NexusBrowserSession(run_id="same-run", publisher=publisher, viewport=(900, 640))
        resumed = NexusBrowserSession(run_id="same-run", publisher=publisher, viewport=(900, 640))
        isolated = NexusBrowserSession(run_id="other-run", publisher=publisher, viewport=(900, 640))
        try:
            first.open(self.url)
            first.locator("#name").fill("Persisted in this Run")
            same_observation = resumed.observe()
            other_observation = isolated.open(self.url)

            self.assertTrue(any(node.text == "Persisted in this Run" for node in same_observation.dom.nodes))
            self.assertFalse(any(node.text == "Persisted in this Run" for node in other_observation.dom.nodes))
        finally:
            first.close()
            isolated.close()

    def test_chrome_crash_reports_session_loss_then_worker_restarts(self):
        session = NexusBrowserSession(
            run_id="crash-run",
            publisher=lambda *_args: None,
            viewport=(900, 640),
        )
        session.open(self.url)
        _browser_worker().call(lambda worker: worker.browser.close())

        with self.assertRaises(NexusBrowserSessionLost):
            session.observe()

        recovered = session.open(self.url)
        self.assertEqual(recovered.revision, 1)
        session.close()

    def test_concurrent_run_callers_keep_separate_pages(self):
        barrier = threading.Barrier(2)
        results = {}
        failures = []

        def operate(run_id, value):
            session = NexusBrowserSession(
                run_id=run_id,
                publisher=lambda *_args: None,
                viewport=(900, 640),
            )
            try:
                barrier.wait(timeout=5)
                session.open(self.url)
                observation = session.locator("#name").fill(value)
                results[run_id] = [node.text for node in observation.dom.nodes]
            except Exception as exc:  # pragma: no cover - assertion reports the real error
                failures.append(exc)
            finally:
                session.close()

        threads = [
            threading.Thread(target=operate, args=("concurrent-a", "Caller A")),
            threading.Thread(target=operate, args=("concurrent-b", "Caller B")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertFalse(failures)
        self.assertIn("Caller A", results["concurrent-a"])
        self.assertNotIn("Caller B", results["concurrent-a"])
        self.assertIn("Caller B", results["concurrent-b"])
        self.assertNotIn("Caller A", results["concurrent-b"])

    def test_async_browser_api_matches_sync_session(self):
        sync_session = NexusBrowserSession(
            run_id="async-run",
            publisher=lambda *_args: None,
            viewport=(900, 640),
        )
        session = NexusAsyncBrowserSession(sync_session)

        async def scenario():
            await session.open(self.url)
            observation = await session.locator("#name").fill("Async Nexus")
            await session.close()
            return observation

        observation = asyncio.run(scenario())
        self.assertTrue(any(node.text == "Async Nexus" for node in observation.dom.nodes))


if __name__ == "__main__":
    unittest.main()
