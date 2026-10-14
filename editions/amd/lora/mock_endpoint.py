"""OpenAI-compatible mock endpoint backed by any in-process ``LLM`` (FakeLLM by default).

- ``mock_client(llm)``: the real ``OpenAICompatLLM`` client wired to an ``httpx.MockTransport`` that answers
  ``POST /chat/completions`` with ``llm`` -- the full request/JSON/response path, no sockets (used by tests).
- ``python -m editions.amd.lora.mock_endpoint --port 8011 --llm fake``: a local HTTP server with the same
  behaviour, for dry-running the event-week commands (dataset teacher, evaluate) on a laptop without a GPU.
  ``"stream": true`` requests get server-sent events like vLLM (one chunk per word, a final ``usage`` chunk when
  ``stream_options.include_usage`` is set, then ``[DONE]``), so ``bench/llm_bench.py`` and ``serving/smoke_test.py``
  run against it in the CPU rehearsal (``editions/amd/kickoff/run_all.sh --rehearsal``). ``--served-name`` sets the
  model id it reports. Its timings measure the stub, never a GPU.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from wapsi.ports import LLM


def _complete_text(llm: LLM, body: dict[str, Any]) -> str:
    fmt = body.get("response_format") or {}
    schema = None
    if fmt.get("type") == "json_schema":
        schema = (fmt.get("json_schema") or {}).get("schema") or {"type": "object"}
    elif fmt.get("type") == "json_object":
        schema = {"type": "object"}
    return llm.complete(
        body.get("messages") or [],
        temperature=float(body.get("temperature") or 0.0),
        max_tokens=int(body.get("max_tokens") or 1024),
        json_schema=schema,
    )


def _words(text: str) -> int:
    return len(text.split())


def _prompt_words(body: dict[str, Any]) -> int:
    return sum(_words(str(m.get("content") or "")) for m in body.get("messages") or [] if isinstance(m, dict))


def _completion(llm: LLM, body: dict[str, Any], served_name: str | None = None) -> dict[str, Any]:
    text = _complete_text(llm, body)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", served_name or getattr(llm, "name", "mock")),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def stream_events(llm: LLM, body: dict[str, Any], served_name: str | None = None) -> list[dict[str, Any]]:
    """The SSE ``data:`` objects of a streamed completion (OpenAI / vLLM chunk shape), one chunk per word."""
    text = _complete_text(llm, body)
    cid, created = f"chatcmpl-{uuid.uuid4().hex[:12]}", int(time.time())
    model = body.get("model", served_name or getattr(llm, "name", "mock"))
    words = text.split(" ") if text else []
    pieces = [w if i == 0 else " " + w for i, w in enumerate(words)]
    base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}
    first = {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}
    events: list[dict[str, Any]] = [{**base, "choices": [first]}]
    events += [{**base, "choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}]} for p in pieces]
    events.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    if (body.get("stream_options") or {}).get("include_usage"):
        prompt, completion = _prompt_words(body), _words(text)
        usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
        events.append({**base, "choices": [], "usage": usage})
    return events


def fake_openai_transport(llm: LLM) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        if request.method == "POST" and path.endswith("/chat/completions"):
            return httpx.Response(200, json=_completion(llm, json.loads(request.content or b"{}")))
        if request.method == "GET" and path.endswith("/models"):
            return httpx.Response(
                200, json={"object": "list", "data": [{"id": getattr(llm, "name", "mock")}]}
            )
        return httpx.Response(404, json={"error": {"message": f"no route {request.method} {path}"}})

    return httpx.MockTransport(handler)


def mock_client(llm: LLM, *, name: str | None = None, model: str = "mock-model"):
    from wapsi.adapters.llm_openai_compat import OpenAICompatLLM

    return OpenAICompatLLM(
        base_url="http://mock.invalid/v1",
        model=model,
        api_key="",
        name=name or f"mock-{getattr(llm, 'name', 'llm')}",
        transport=fake_openai_transport(llm),
        max_retries=0,
    )


def serve(llm: LLM, host: str = "127.0.0.1", port: int = 8011, served_name: str | None = None) -> None:
    model_id = served_name or getattr(llm, "name", "mock")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, data: dict[str, Any]) -> None:
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._send(404, {"error": {"message": "not found"}})
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for event in stream_events(llm, body, model_id):
                    self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            self._send(200, _completion(llm, body, model_id))

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/").endswith("/models"):
                card = {"id": model_id, "object": "model", "root": "stub (no model weights)"}
                self._send(200, {"object": "list", "data": [card]})
            else:
                self._send(404, {"error": {"message": "not found"}})

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"mock OpenAI-compatible endpoint ({model_id}) on http://{host}:{port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    from editions.amd.lora.common import make_llm

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--llm", default="fake", help="fake | fake-noisy | fake-hallucinate | fake-inject | hf:<path>"
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8011)
    ap.add_argument("--served-name", default=None, help="model id reported by /v1/models (default: the LLM's name)")
    args = ap.parse_args(argv)
    serve(make_llm(args.llm), args.host, args.port, served_name=args.served_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
