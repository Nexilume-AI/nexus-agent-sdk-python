"""Cache controls and usage traverse the actual HTTP/WebSocket Relay unchanged."""
import json
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_model_relay import ModelRelayTests


class ModelRelayCacheTests(ModelRelayTests):
    async def test_chat_responses_cache_body_and_json_sse_usage_are_byte_exact(self):
        await self.upstream.close()
        received = []
        usage = {"input_tokens": 12000, "output_tokens": 20, "input_tokens_details": {
            "cached_tokens": 4000, "cache_write_tokens": 7000,
            "cache_write_tokens_details": {"ttl_30m_tokens": 3000, "ttl_12h_tokens": 4000}}}
        raw = json.dumps({"usage": usage}, separators=(",", ":")).encode()

        async def upstream(request):
            self.assertEqual(request.headers.get("Authorization"), "Bearer upstream-key")
            body = await request.read()
            received.append((request.path, body))
            if json.loads(body).get("stream"):
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                event = b"data: " + raw + b"\n\ndata: [DONE]\n\n"
                for offset in range(0, len(event), 13):
                    await response.write(event[offset:offset+13])
                await response.write_eof()
                return response
            return web.Response(body=raw, content_type="application/json")

        app = web.Application()
        app.router.add_route("POST", "/{path:.*}", upstream)
        self.upstream = TestServer(app)
        self.ip_patch.stop()
        await self.upstream.start_server()
        self.ip_patch.start()
        for path in ("chat/completions", "responses"):
            for stream in (False, True):
                payload = {"model": "model-1", "stream": stream, "prompt_cache_key": "stable Unicode 前缀",
                           "prompt_cache_options": {"mode": "explicit", "ttl": "12h", "prewarm": True},
                           "input" if path == "responses" else "messages": [{"role": "user", "content": [{
                               "type": "input_text" if path == "responses" else "text", "text": "prefix " * 8000,
                               "prompt_cache_breakpoint": {"mode": "explicit", "ttl": "12h"}}]}]}
                body = json.dumps(payload, ensure_ascii=False, indent=2).encode()
                async with self.client.post(self.url(path), headers={**self.headers, "Content-Type": "application/json"}, data=body) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.read(), b"data: " + raw + b"\n\ndata: [DONE]\n\n" if stream else raw)
                self.assertEqual(received[-1], ("/v1/" + path, body))
        self.assertEqual(len(received), 4)
