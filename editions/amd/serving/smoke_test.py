"""Smoke test for a vLLM (or any OpenAI-compatible) endpoint, then one Wapsi showcase case through the pipeline.

    uv run python -m editions.amd.serving.smoke_test --base-url http://127.0.0.1:8000/v1
    uv run python -m editions.amd.serving.smoke_test --base-url http://127.0.0.1:8001/v1 --model wapsi-verifier --skip-pipeline

Steps
    1. GET  {base}/models                  lists served models (vLLM also reports ``root`` and ``max_model_len``)
    2. POST {base}/chat/completions        one short completion; prints latency and the reply
    3. process_case(showcase case)         the real pipeline with ``OpenAICompatLLM`` against the endpoint,
                                           printing the verification summary (drafts, passed, prose fallbacks)
Exit code: 0 ok, 1 endpoint error, 2 the pipeline ran but a draft failed verification.
The API key (if any) comes from ``WAPSI_LLM_API_KEY`` / ``VLLM_API_KEY``; it is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

EXIT_OK, EXIT_ENDPOINT, EXIT_VERIFY = 0, 1, 2


def _api_key(explicit: str | None) -> str | None:
    return explicit or os.environ.get("WAPSI_LLM_API_KEY") or os.environ.get("VLLM_API_KEY") or None


def _headers(key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key else {}


def list_models(client: httpx.Client, base_url: str) -> list[dict[str, Any]]:
    resp = client.get(f"{base_url}/models")
    resp.raise_for_status()
    data = resp.json()
    models = data.get("data") if isinstance(data, dict) else None
    if not isinstance(models, list) or not models:
        raise RuntimeError(f"{base_url}/models returned no models: {str(data)[:200]}")
    return models


def chat_once(client: httpx.Client, base_url: str, model: str, max_tokens: int = 32) -> tuple[str, float, dict]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a terse assistant."},
            {"role": "user", "content": "Reply with the single word OK."},
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    t = time.perf_counter()
    resp = client.post(f"{base_url}/chat/completions", json=payload)
    latency = time.perf_counter() - t
    resp.raise_for_status()
    data = resp.json()
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    text = msg.get("content") or msg.get("reasoning_content") or ""
    return str(text), latency, data.get("usage") or {}


def run_showcase_case(base_url: str, model: str, *, api_key: str | None, timeout: float,
                      transport: httpx.BaseTransport | None = None, json_mode: str = "json_schema",
                      ncrp_ids: list[str] | None = None) -> dict[str, Any]:
    """Process one showcase case with the real pipeline against the endpoint; returns ``case_summary``."""
    from editions.amd.app.summary import case_summary
    from wapsi.adapters.llm_openai_compat import OpenAICompatLLM
    from wapsi.fixtures import showcase as sc
    from wapsi.pipeline.run import process_case

    llm = OpenAICompatLLM(base_url=base_url, model=model, api_key=api_key or "", timeout=timeout,
                          json_mode=json_mode, transport=transport, name=f"smoke:{model}")
    try:
        ledger = sc.showcase_ledger()
        bundle = process_case(ledger, ncrp_ids or [sc.N1], llm=llm)
    finally:
        llm.close()
    return case_summary(bundle)


def print_summary(s: dict[str, Any], out=sys.stdout) -> None:
    t = s["timings_s"]
    print(f"case {s['case_id']}  status={s['status']}  llm={s['llm']}", file=out)
    print(f"  drafts: {s['drafts']}  passed verification: {s['drafts_passed']}  "
          f"LLM prose drafts: {s['llm_drafts']}  prose fallbacks: {s['prose_fallbacks']}", file=out)
    print(f"  hold plan in {t.get('time_to_hold_plan_s', 0):.2f}s  drafting {t.get('draft_s', 0):.2f}s  "
          f"total {t.get('total_s', 0):.2f}s", file=out)
    for f in s["failed_checks"][:10]:
        print(f"  FAILED {f['draft_id']} [{f['check']}] {f['message']}", file=out)


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.environ.get("WAPSI_LLM_BASE_URL", "http://127.0.0.1:8000/v1"))
    ap.add_argument("--model", default=None, help="served model name (default: first id from /models)")
    ap.add_argument("--api-key", default=None, help="default: $WAPSI_LLM_API_KEY or $VLLM_API_KEY")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--json-mode", default="json_schema", choices=["json_schema", "json_object"])
    ap.add_argument("--skip-pipeline", action="store_true", help="only /models and one completion")
    ap.add_argument("--json", action="store_true", help="print a JSON result instead of text")
    args = ap.parse_args(argv)
    base = args.base_url.rstrip("/")
    key = _api_key(args.api_key)
    result: dict[str, Any] = {"base_url": base}

    try:
        with httpx.Client(timeout=args.timeout, headers=_headers(key), transport=transport) as client:
            models = list_models(client, base)
            model = args.model or str(models[0].get("id"))
            card = next((m for m in models if m.get("id") == model), None)
            if card is None:
                raise RuntimeError(f"model {model!r} not served; available: {[m.get('id') for m in models]}")
            result["model"] = model
            result["model_root"] = card.get("root")
            result["max_model_len"] = card.get("max_model_len")
            if not args.json:
                print(f"[1/3] {base}/models -> {[m.get('id') for m in models]} "
                      f"(root={card.get('root')}, max_model_len={card.get('max_model_len')})")
            text, latency, usage = chat_once(client, base, model)
            result["chat"] = {"latency_s": round(latency, 3), "reply": text[:200], "usage": usage}
            if not args.json:
                print(f"[2/3] chat completion in {latency:.2f}s -> {text.strip()[:80]!r} usage={usage}")
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        msg = f"endpoint check failed: {type(exc).__name__}: {exc}"
        print(json.dumps({**result, "ok": False, "error": msg}) if args.json else msg, file=sys.stderr)
        return EXIT_ENDPOINT

    code = EXIT_OK
    if not args.skip_pipeline:
        try:
            summary = run_showcase_case(base, model, api_key=key, timeout=args.timeout, transport=transport,
                                        json_mode=args.json_mode)
        except Exception as exc:  # noqa: BLE001 - report any pipeline error as an endpoint failure
            msg = f"pipeline failed against {base}: {type(exc).__name__}: {exc}"
            print(json.dumps({**result, "ok": False, "error": msg}) if args.json else msg, file=sys.stderr)
            return EXIT_ENDPOINT
        result["pipeline"] = summary
        if not args.json:
            print("[3/3] showcase case through the Wapsi pipeline:")
            print_summary(summary)
        if not summary["all_verified"]:
            code = EXIT_VERIFY
    result["ok"] = code == EXIT_OK
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    elif code == EXIT_OK:
        print("smoke test passed")
    else:
        print("smoke test: a draft failed verification (see FAILED lines)")
    return code


if __name__ == "__main__":
    sys.exit(main())
