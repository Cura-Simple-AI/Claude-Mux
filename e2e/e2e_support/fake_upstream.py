"""A fake LLM provider: a real HTTP server on 127.0.0.1 with programmable routes.

It stands in for api.anthropic.com / DeepSeek / z.ai etc. Every request is
recorded so tests can assert on what actually went over the wire (path, query,
headers, body) after it passed through claude-mux.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlsplit


@dataclass
class Recorded:
    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes

    def json(self):
        return json.loads(self.body)


@dataclass
class Reply:
    status: int = 200
    json: object = None
    body: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)
    #: Server-sent events: list of (event_name, data_dict). Sent as a stream.
    sse: list[tuple[str, dict]] | None = None


Handler = Callable[[Recorded], Reply]

MODELS = ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"]


def anthropic_message(model: str, text: str | None = None, *, input_tokens=12, output_tokens=7) -> dict:
    return {
        "id": "msg_e2e",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text or f"Hello from fake upstream ({model})"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _default_messages(req: Recorded) -> Reply:
    try:
        model = req.json().get("model", "")
    except ValueError:
        model = ""
    return Reply(json=anthropic_message(model))


def _default_models(_req: Recorded) -> Reply:
    return Reply(json={"data": [{"id": m, "type": "model"} for m in MODELS]})


class FakeUpstream:
    def __init__(self):
        self.requests: list[Recorded] = []
        self._routes: dict[tuple[str, str], Handler] = {
            ("POST", "/v1/messages"): _default_messages,
            ("GET", "/v1/models"): _default_models,
        }
        self._lock = threading.Lock()
        upstream = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep test output clean
                pass

            def _serve(self):
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                rec = Recorded(self.command, parts.path, parts.query,
                               {k.lower(): v for k, v in self.headers.items()}, body)
                with upstream._lock:
                    upstream.requests.append(rec)
                    handler = upstream._routes.get((self.command, parts.path))
                reply = handler(rec) if handler else Reply(404, json={"error": {"message": "no route"}})
                self._write(reply)

            def _write(self, reply: Reply):
                self.send_response(reply.status)
                for k, v in reply.headers.items():
                    self.send_header(k, v)
                if reply.sse is not None:
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for name, data in reply.sse:
                        self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
                        self.wfile.flush()
                    return
                payload = reply.body if reply.body is not None else json.dumps(reply.json).encode()
                if "Content-Type" not in reply.headers:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_DELETE = _serve

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    # -- configuration ----------------------------------------------------
    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def route(self, method: str, path: str, handler: Handler | Reply):
        fn = handler if callable(handler) else (lambda _req, r=handler: r)
        with self._lock:
            self._routes[(method, path)] = fn

    def fail_models(self, *models: str, status: int = 429, message: str = "rate limited"):
        """Make POST /v1/messages fail for the given model ids only."""
        bad = set(models)

        def handler(req: Recorded) -> Reply:
            model = req.json().get("model", "")
            if model in bad:
                return Reply(status, json={"type": "error", "error": {"type": "rate_limit_error", "message": message}})
            return _default_messages(req)

        self.route("POST", "/v1/messages", handler)

    # -- inspection -------------------------------------------------------
    def received(self, method: str | None = None, path: str | None = None) -> list[Recorded]:
        with self._lock:
            return [r for r in self.requests
                    if (method is None or r.method == method) and (path is None or r.path == path)]

    def close(self):
        self._server.shutdown()
        self._server.server_close()
