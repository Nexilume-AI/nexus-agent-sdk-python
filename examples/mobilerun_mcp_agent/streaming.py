"""A disconnected synchronous stream must stop its owned execution."""
import contextvars
import json
import queue
import threading
from nexus_agent import McpToolResult, SseEvent


def task_stream(call, heartbeat_seconds=5):
    stopped = threading.Event()
    inbox = queue.Queue(maxsize=1)
    context = contextvars.copy_context()
    def execute():
        try:
            result = call(stopped)
        except Exception as exc:
            safe = str(exc) if isinstance(exc, ValueError) and str(exc).replace('_', '').isalnum() else type(exc).__name__
            result = McpToolResult(content=[{'type':'text','text':'Mobilerun could not complete the task: '+safe[:120]}], is_error=True)
        inbox.put(result)
    threading.Thread(target=lambda:context.run(execute), daemon=True).start()
    try:
        yield SseEvent(event='progress', data='{"progress":0,"message":"Mobilerun is preparing this task"}')
        while True:
            try:
                result = inbox.get(timeout=heartbeat_seconds)
                break
            except queue.Empty:
                yield SseEvent(event='heartbeat', data='{}')
        yield SseEvent(event='result', data=json.dumps({'result':result.to_wire()}))
    finally:
        # Do not release the caller slot here. The worker owns it until cleanup
        # completes; new work must never overlap an unfinished phone action.
        stopped.set()
