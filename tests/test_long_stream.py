import io
import unittest
from nexus_agent.server import NexusAgentServer, SseEvent

class Request:
    def __init__(self): self.wfile = io.BytesIO()
    def send_response(self, *args): pass
    def send_header(self, *args): pass
    def end_headers(self): pass

class LongStreamTests(unittest.TestCase):
    def server(self):
        server = object.__new__(NexusAgentServer)
        server.max_response_bytes = 1024
        server.max_stream_event_bytes = 4096
        return server

    def test_cumulative_stream_can_exceed_json_response_budget(self):
        request = Request()
        self.server()._send_stream(request, (SseEvent(event="progress", data="x"*1000) for _ in range(2048)))
        self.assertGreater(len(request.wfile.getvalue()), 2_000_000)
        self.assertTrue(request.wfile.getvalue().endswith(b": nexus-stream-complete\n\n"))

    def test_oversized_event_still_stops_and_closes_generator(self):
        request = Request(); closed = []
        def items():
            try: yield SseEvent(event="progress", data="x"*5000)
            finally: closed.append(True)
        self.server()._send_stream(request, items())
        self.assertEqual(request.wfile.getvalue(), b"")
        self.assertEqual(closed, [True])
