"""A local OpenAI-compatible endpoint, so any project in any language can use the router.

    free-llm serve            -> http://127.0.0.1:8787/v1

Point an OpenAI client at that base URL, with any API key (or FREE_LLM_PROXY_KEY when set)
and model "auto" or a name from `free-llm list`. `stream: true` is answered as a single chunk.
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from free_llm.router import AllModelsFailed, Router


def make_handler(router: Router, proxy_key: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "free-llm/0.1"

        def log_message(self, fmt, *args):  # one short line per request
            print(f"{self.address_string()} {fmt % args}")

        def _json(self, status: int, data: dict, headers: dict | None = None) -> None:
            raw = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def _error(self, status: int, message: str, kind: str, **extra) -> None:
            self._json(status, {"error": {"message": message, "type": kind, **extra}})

        def _allowed(self) -> bool:
            if not proxy_key:
                return True
            if self.headers.get("Authorization", "") == f"Bearer {proxy_key}":
                return True
            self._error(401, "wrong FREE_LLM_PROXY_KEY", "auth")
            return False

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/")
            if path == "/health":
                return self._json(200, {"ok": True})
            if not self._allowed():
                return
            if path in ("/v1/models", "/models"):
                rows = [{"id": "auto", "object": "model", "owned_by": "free-llm", "ready": True}]
                rows += [
                    {"id": r["model"], "object": "model", "owned_by": r["model"].split(":")[0],
                     "ready": r["ready"], "why": r["why"]}
                    for r in router.status()
                ]
                return self._json(200, {"object": "list", "data": rows})
            self._error(404, f"no route {path}", "not_found")

        def do_POST(self):
            path = self.path.split("?")[0].rstrip("/")
            if path not in ("/v1/chat/completions", "/chat/completions"):
                return self._error(404, f"no route {path}", "not_found")
            if not self._allowed():
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                messages = body.pop("messages")
            except (ValueError, KeyError):
                return self._error(400, "send JSON with `messages`", "invalid_request_error")
            stream = bool(body.pop("stream", False))
            body.pop("stream_options", None)
            model = body.pop("model", "auto")
            try:
                reply = router.chat(messages, model=model, **body)
            except KeyError as e:
                return self._error(404, str(e).strip("'\""), "model_not_found")
            except AllModelsFailed as e:
                return self._error(503, str(e), "all_models_failed", attempts=e.attempts)
            used = reply["x_free_llm"]["model"]
            if stream:
                return self._stream(reply, used)
            self._json(200, reply, {"X-Free-LLM-Model": used})

        def _stream(self, reply: dict, used: str) -> None:
            msg = reply["choices"][0].get("message") or {}
            delta = {"role": "assistant", "content": msg.get("content") or ""}
            if msg.get("tool_calls"):
                delta["tool_calls"] = [{**c, "index": i} for i, c in enumerate(msg["tool_calls"])]
            base = {"id": reply.get("id", "free-llm"), "object": "chat.completion.chunk",
                    "created": reply.get("created", int(time.time())), "model": reply.get("model")}
            chunks = [
                {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {},
                                      "finish_reason": reply["choices"][0].get("finish_reason")}],
                 "usage": reply.get("usage")},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("X-Free-LLM-Model", used)
            self.end_headers()
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

    return Handler


def serve(router: Router, host: str = "127.0.0.1", port: int = 8787, proxy_key: str | None = None):
    httpd = ThreadingHTTPServer((host, port), make_handler(router, proxy_key))
    print(f"free-llm on http://{host}:{port}/v1  (model: auto, or a name from `free-llm list`)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
