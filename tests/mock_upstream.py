import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockUpstream:
    """Scriptable OpenAI-compatible upstream. Captures every request."""

    def __init__(self, chunk_delay=0.15, bind_host="127.0.0.1"):
        self.chunk_delay = chunk_delay
        # Number of SSE chunks per streamed completion; tests stretch this
        # to keep a stream alive past a shutdown drain window.
        self.sse_parts = 5
        self.script = []
        self._requests = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args, **kwargs):
                pass

            def do_POST(self):
                outer._handle_post(self)

            def do_GET(self):
                outer._handle_get(self)

        self.server = ThreadingHTTPServer((bind_host, 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def url(self, path="/"):
        return f"http://127.0.0.1:{self.port}{path}"

    def captured(self):
        with self._lock:
            return list(self._requests)

    def chat_posts(self):
        return [
            r for r in self.captured()
            if r["method"] == "POST" and r["path"].endswith("chat/completions")
        ]

    def _record(self, handler, body):
        with self._lock:
            self._requests.append({
                "method": handler.command,
                "path": handler.path,
                "auth": handler.headers.get("Authorization"),
                "body": body.decode("utf-8", "replace"),
                "headers": {k: v for k, v in handler.headers.items()},
                "ts": time.time(),
            })

    def _send_json(self, handler, status, payload, extra_headers=None):
        out = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        handler.send_response(status)
        for k, v in (extra_headers or {}).items():
            handler.send_header(k, v)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(out)))
        handler.send_header("Server", "MockUpstream/1.0")
        handler.end_headers()
        handler.wfile.write(out)

    def _handle_post(self, handler):
        n = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(n)
        self._record(handler, body)
        with self._lock:
            scripted = self.script.pop(0) if self.script else None
        if scripted is not None:
            status, extra, payload = scripted
            self._send_json(handler, status, payload, extra)
            return
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {}
        if isinstance(parsed, dict) and parsed.get("stream"):
            self._send_sse(handler)
        else:
            self._send_json(handler, 200, {
                "id": "cmpl-1",
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
            })

    def _send_sse(self, handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Connection", "close")
        handler.send_header("Server", "MockUpstream/1.0")
        handler.end_headers()
        for i in range(self.sse_parts):
            chunk = {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [{"delta": {"content": f"part{i} "}}],
            }
            handler.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            handler.wfile.flush()
            time.sleep(self.chunk_delay)
        final = {
            "id": "c1",
            "object": "chat.completion.chunk",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14},
        }
        handler.wfile.write(f"data: {json.dumps(final)}\n\ndata: [DONE]\n\n".encode())
        handler.wfile.flush()
        handler.close_connection = True

    def _handle_get(self, handler):
        self._record(handler, b"")
        self._send_json(handler, 200, {"object": "list", "data": [{"id": "gpt-4o"}]})
