"""OpenAI-compatible mock endpoint backed by any in-process ``LLM`` (FakeLLM by default).

- ``mock_client(llm)``: the real ``OpenAICompatLLM`` client wired to an ``httpx.MockTransport`` that answers
  ``POST /chat/completions`` with ``llm`` -- the full request/JSON/response path, no sockets (used by tests).
- ``python -m editions.amd.lora.mock_endpoint --port 8011 --llm fake``: a local HTTP server with the same
  behaviour, for dry-running the event-week commands (dataset teacher, evaluate) on a laptop without a GPU.
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


def _completion(llm: LLM, body: dict[str, Any]) -> dict[str, Any]:
    fmt = body.get("response_format") or {}
    schema = None
    if fmt.get("type") == "json_schema":
        schema = (fmt.get("json_schema") or {}).get("schema") or {"type": "object"}
    elif fmt.get("type") == "json_object":
        schema = {"type": "object"}
    text = llm.complete(
        body.get("messages") or [],
        temperature=float(body.get("temperature") or 0.0),
        max_tokens=int(body.get("max_tokens") or 1024),
        json_schema=schema,
    )
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", getattr(llm, "name", "mock")),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


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


def serve(llm: LLM, host: str = "127.0.0.1", port: int = 8011) -> None:
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
            self._send(200, _completion(llm, body))

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/").endswith("/models"):
                self._send(200, {"object": "list", "data": [{"id": getattr(llm, "name", "mock")}]})
            else:
                self._send(404, {"error": {"message": "not found"}})

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"mock OpenAI-compatible endpoint ({getattr(llm, 'name', 'llm')}) on http://{host}:{port}/v1")
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
    args = ap.parse_args(argv)
    serve(make_llm(args.llm), args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
