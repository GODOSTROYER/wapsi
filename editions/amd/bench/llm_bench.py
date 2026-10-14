"""Streaming benchmark for an OpenAI-compatible endpoint (vLLM-ROCm on the MI300X, or a provider's endpoint).

    uv run python -m editions.amd.bench.llm_bench --base-url http://127.0.0.1:8000/v1 \
        --concurrency 1 4 16 64 --input-len 1024 --output-len 256 --out editions/amd/bench/results/<stamp>
    # a shared, rate-limited endpoint (AMD Token Factory: 20 requests/min, 8 at once for the whole team)
    uv run python -m editions.amd.bench.llm_bench --base-url $WAPSI_LLM_BASE_URL --model $WAPSI_LLM_MODEL \
        --concurrency 1 4 8 --requests 6 --rpm 16 --no-ignore-eos --out <evidence dir>/bench

For each concurrency level it sends ``--requests`` streaming chat completions (default ``max(8, 4 x level)``)
with at most ``level`` in flight and measures:

- TTFT: send -> first streamed text (``delta.content``, or ``reasoning_content`` / ``reasoning``)
- ITL: gaps between successive streamed text chunks (a chunk may carry more than one token)
- TPOT: (end-to-end - TTFT) / (output tokens - 1), per request
- output tokens/s (aggregate, over the level's wall time), output tokens/s per user, requests/s

Output tokens come from the final ``usage`` chunk (``stream_options.include_usage``); if the server sends none,
streamed text chunks are counted instead (``tokens_source = "chunks"``). Synthetic prompts start with a unique
nonce so prefix caching does not flatter the numbers; ``ignore_eos`` + ``min_tokens`` (vLLM extensions) pin the
output length. ``--prompts wapsi`` replays the real drafting prompts the Wapsi pipeline sends for the showcase
cases (natural output lengths). Writes ``<name>.json`` and ``<name>.md`` (``--name``, default ``llm_bench``).

Rate-limited endpoints (``--rpm N``): a level starts only when the last 60 s leave room for its whole burst, so the
concurrency is real; a request that had to be paced inside a level, or that got an HTTP 429 (retried after its
``Retry-After``, timed from the retry), is counted (``paced_s``, ``rate_limited``) and such a level is not a clean
throughput measurement. ``usage.cost`` (the cost the API reports) is summed per level when present.

Buffered streams: a gateway may deliver the whole stream in one burst. Then TTFT is the time the client waited for
that burst (still what a caller sees), and the per-chunk numbers (ITL, TPOT, tokens/s per user) mean nothing: the
level is marked ``stream_buffered`` and those are ``null``. ``request_tokens_per_s`` (output tokens / request time)
and the aggregate ``output_tokens_per_s`` do not depend on chunk timing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from editions.amd.bench.common import (  # noqa: E402
    SCHEMA_VERSION,
    dist,
    env_info,
    md_table,
    results_dir,
    utc_now,
    write_json,
)

DEFAULT_LEVELS = (1, 4, 16, 64)
_WORDS = (
    "account transfer victim mule layer hold amount complaint bank branch officer ledger balance credit debit "
    "trail money report suspicious cash withdrawal merchant invoice pool nodal escrow restoration grievance "
    "notice review customer device timeline channel reference evidence summary pattern network rapid"
).split()


@dataclass
class BenchConfig:
    base_url: str
    model: str | None = None
    api_key: str | None = None
    levels: Sequence[int] = DEFAULT_LEVELS
    requests: int | None = None  # per level; None = max(8, 4 x level)
    input_len: int = 1024  # approximate prompt tokens (synthetic prompts)
    output_len: int = 256
    prompts: str = "synthetic"  # synthetic | wapsi
    ignore_eos: bool = True
    temperature: float = 0.0
    timeout_s: float = 600.0
    warmup: int = 2
    seed: int = 0
    rpm: float = 0.0  # > 0: at most this many request starts per 60 s (shared, rate-limited endpoints)
    retry_429: int = 3  # retries of an HTTP 429 after its Retry-After (0 = record it as an error)
    json_mode: str = "json_schema"  # json_schema (vLLM guided decoding) | json_object (no strict schema served)
    extra_body: dict | None = None  # request fields the served model needs (e.g. reasoning_effort for a thinking model)
    label: str | None = None  # where this was measured, stored in the result

    def requests_for(self, level: int) -> int:
        return self.requests if self.requests else max(8, 4 * level)


@dataclass
class Prompt:
    messages: list[dict[str, str]]
    max_tokens: int
    fixed_length: bool
    json_schema: dict | None = None


@dataclass
class RequestRecord:
    ok: bool
    start: float
    end: float
    ttft: float | None = None
    chunk_times: list[float] = field(default_factory=list)  # seconds after start, text-bearing chunks
    output_tokens: int | None = None  # from usage
    prompt_tokens: int | None = None
    error: str | None = None
    cost: float | None = None  # usage.cost, when the API reports one
    rate_limited: int = 0  # HTTP 429s before the attempt that was timed
    paced_s: float = 0.0  # seconds this request waited for the --rpm window after its level had started
    retry_after: float | None = None  # set when this attempt was an HTTP 429

    @property
    def e2e(self) -> float:
        return self.end - self.start

    @property
    def tokens(self) -> int:
        return self.output_tokens if self.output_tokens is not None else len(self.chunk_times)

    @property
    def itls(self) -> list[float]:
        t = self.chunk_times
        return [b - a for a, b in zip(t, t[1:], strict=False)]

    @property
    def buffered(self) -> bool:
        """The stream arrived as one burst (all text chunks within 2% of the request time): no per-chunk timing."""
        t = self.chunk_times
        return len(t) >= 8 and self.e2e > 0 and (t[-1] - t[0]) / self.e2e < 0.02

    @property
    def tpot(self) -> float | None:
        if self.ttft is None or self.tokens < 2:
            return None
        return (self.e2e - self.ttft) / (self.tokens - 1)


# --------------------------------------------------------------------------- prompts


def synthetic_prompts(n: int, input_len: int, output_len: int, seed: int = 0) -> list[Prompt]:
    rng = random.Random(seed)
    words = max(8, int(input_len * 0.75))  # ~0.75 English words per token
    out = []
    for i in range(n):
        body = " ".join(rng.choice(_WORDS) for _ in range(words))
        nonce = f"{seed}-{i}-{rng.getrandbits(48):012x}"
        out.append(Prompt(
            messages=[{"role": "system", "content": f"Request {nonce}. You are a bank fraud analyst."},
                      {"role": "user", "content": f"Summarise these case notes in plain English: {body}"}],
            max_tokens=output_len, fixed_length=True))
    return out


def wapsi_prompts(limit: int | None = None) -> list[Prompt]:
    """The drafting requests the Wapsi pipeline sends for the showcase cases (recorded offline via FakeLLM)."""
    from wapsi.adapters.llm_fake import FakeLLM
    from wapsi.fixtures import showcase as sc
    from wapsi.pipeline.run import process_case

    class Recorder(FakeLLM):
        def __init__(self) -> None:
            super().__init__(name="recorder")
            self.requests: list[Prompt] = []

        def complete(self, messages, *, temperature=0.0, max_tokens=1024, json_schema=None):  # type: ignore[override]
            self.requests.append(Prompt([dict(m) for m in messages], max_tokens, False, json_schema))
            return super().complete(messages, temperature=temperature, max_tokens=max_tokens,
                                    json_schema=json_schema)

    rec = Recorder()
    ledger = sc.showcase_ledger()
    for ids in ([sc.N1], sorted([sc.N2, sc.N3])):
        process_case(ledger, ids, llm=rec)
    return rec.requests[:limit] if limit else rec.requests


# --------------------------------------------------------------------------- client


def _payload(cfg: BenchConfig, model: str, prompt: Prompt) -> dict[str, Any]:
    body: dict[str, Any] = {
        **(cfg.extra_body or {}),
        "model": model,
        "messages": prompt.messages,
        "temperature": cfg.temperature,
        "max_tokens": prompt.max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if prompt.fixed_length and cfg.ignore_eos:
        body["ignore_eos"] = True
        body["min_tokens"] = prompt.max_tokens
    if prompt.json_schema is not None and cfg.json_mode == "json_object":
        body["response_format"] = {"type": "json_object"}  # the endpoint serves no strict schema (Token Factory)
    elif prompt.json_schema is not None:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "response", "schema": prompt.json_schema}}
    return body


def _chunk_text(obj: dict[str, Any]) -> str:
    parts = []
    for c in obj.get("choices") or []:
        delta = c.get("delta") or {}
        parts.append(delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning") or "")
    return "".join(p for p in parts if isinstance(p, str))


async def stream_one(client: httpx.AsyncClient, cfg: BenchConfig, model: str, prompt: Prompt,
                     limiter: Any = None) -> RequestRecord:
    """One streamed completion. An HTTP 429 is the provider's queue, not the model: it is retried after its
    ``Retry-After`` (``cfg.retry_429`` times) and the request is timed from the attempt that was accepted."""
    rec = await _stream_attempt(client, cfg, model, prompt)
    limited = 0
    while rec.retry_after is not None and limited < cfg.retry_429:
        limited += 1
        if limiter is not None:
            limiter.penalize(rec.retry_after)
        await asyncio.sleep(rec.retry_after + 0.25)
        rec = await _stream_attempt(client, cfg, model, prompt)
    rec.rate_limited = limited
    return rec


async def _stream_attempt(client: httpx.AsyncClient, cfg: BenchConfig, model: str, prompt: Prompt) -> RequestRecord:
    url = f"{cfg.base_url.rstrip('/')}/chat/completions"
    start = time.perf_counter()
    rec = RequestRecord(ok=False, start=start, end=start)
    try:
        async with client.stream("POST", url, json=_payload(cfg, model, prompt)) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                rec.error = f"HTTP {resp.status_code}: {body[:200]}"
                rec.end = time.perf_counter()
                if resp.status_code == 429:
                    try:
                        rec.retry_after = max(0.0, float(resp.headers.get("retry-after") or 5.0))
                    except ValueError:
                        rec.retry_after = 5.0
                return rec
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if obj.get("error"):
                    rec.error = str(obj["error"])[:200]
                    break
                now = time.perf_counter() - start
                if _chunk_text(obj):
                    if rec.ttft is None:
                        rec.ttft = now
                    rec.chunk_times.append(now)
                usage = obj.get("usage")
                if isinstance(usage, dict):
                    rec.output_tokens = usage.get("completion_tokens", rec.output_tokens)
                    rec.prompt_tokens = usage.get("prompt_tokens", rec.prompt_tokens)
                    if isinstance(usage.get("cost"), int | float):
                        rec.cost = float(usage["cost"])
    except httpx.HTTPError as exc:
        rec.error = f"{type(exc).__name__}: {exc}"[:200]
    rec.end = time.perf_counter()
    rec.ok = rec.error is None and rec.ttft is not None
    if rec.error is None and rec.ttft is None:
        rec.error = "no streamed text"
    return rec


def summarize_level(records: Sequence[RequestRecord], concurrency: int, duration_s: float) -> dict[str, Any]:
    ok = [r for r in records if r.ok]
    out_tokens = sum(r.tokens for r in ok)
    in_tokens = sum(r.prompt_tokens or 0 for r in ok)
    buffered = sum(1 for r in ok if r.buffered)
    stream_buffered = bool(ok) and buffered * 2 > len(ok)
    per_user = [] if stream_buffered else [(r.tokens - 1) / (r.e2e - r.ttft) for r in ok
                                           if r.ttft is not None and r.tokens > 1 and r.e2e > r.ttft]
    costs = [r.cost for r in ok if r.cost is not None]
    none = dist([])
    dur = max(duration_s, 1e-9)
    return {
        "concurrency": concurrency,
        "requests": len(records),
        "completed": len(ok),
        "errors": len(records) - len(ok),
        "error_samples": sorted({r.error for r in records if r.error})[:3],
        "duration_s": round(duration_s, 4),
        "rate_limited": sum(r.rate_limited for r in records),
        "paced_s": round(sum(r.paced_s for r in records), 3),
        "stream_buffered": stream_buffered,
        "ttft_s": dist(r.ttft for r in ok),
        "itl_s": none if stream_buffered else dist(x for r in ok for x in r.itls),
        "tpot_s": none if stream_buffered else dist(r.tpot for r in ok),
        "e2e_s": dist(r.e2e for r in ok),
        "request_tokens_per_s": dist((r.tokens / r.e2e for r in ok if r.e2e > 0 and r.tokens), digits=2),
        "cost_usd": round(sum(costs), 8) if costs else None,
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "tokens_source": "usage" if ok and all(r.output_tokens is not None for r in ok) else "chunks",
        "output_tokens_per_s": round(out_tokens / dur, 2),
        "total_tokens_per_s": round((in_tokens + out_tokens) / dur, 2),
        "requests_per_s": round(len(ok) / dur, 4),
        "output_tokens_per_s_per_user": dist(per_user, digits=2)["mean"],
    }


async def run_level(client: httpx.AsyncClient, cfg: BenchConfig, model: str, level: int,
                    prompts: Sequence[Prompt], limiter: Any = None) -> dict[str, Any]:
    """``limiter`` (``--rpm``): wait until the 60 s window has room for the level's burst, then start; a request
    that still has to wait for the window inside the level records it (``paced_s``)."""
    sem = asyncio.Semaphore(level)
    if limiter is not None:
        room = limiter.room_wait(len(prompts))
        if room > 0:
            await asyncio.sleep(room)

    async def one(p: Prompt) -> RequestRecord:
        async with sem:
            paced = 0.0
            while limiter is not None:
                wait = limiter.try_start()
                if wait <= 0:
                    break
                paced += wait
                await asyncio.sleep(wait)
            rec = await stream_one(client, cfg, model, p, limiter)
            rec.paced_s = paced
            return rec

    t0 = time.perf_counter()
    records = await asyncio.gather(*(one(p) for p in prompts))
    return summarize_level(records, level, time.perf_counter() - t0)


async def resolve_model(client: httpx.AsyncClient, base_url: str, model: str | None) -> tuple[str, dict]:
    resp = await client.get(f"{base_url.rstrip('/')}/models")
    resp.raise_for_status()
    cards = resp.json().get("data") or []
    if not cards:
        raise RuntimeError("endpoint serves no models")
    card = next((c for c in cards if c.get("id") == model), None) if model else cards[0]
    if card is None:
        raise RuntimeError(f"model {model!r} not served; available: {[c.get('id') for c in cards]}")
    return str(card.get("id")), card


async def run_bench(cfg: BenchConfig, *, transport: httpx.AsyncBaseTransport | None = None,
                    progress: bool = False) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    limits = httpx.Limits(max_connections=max(cfg.levels) + 8, max_keepalive_connections=max(cfg.levels) + 8)
    started = utc_now()
    limiter = None
    if cfg.rpm > 0:
        from editions.amd.kickoff.endpoint import RateLimiter

        # WAPSI_LLM_MIN_INTERVAL_S: seconds between request starts (a shared key that asks for spaced-out calls)
        limiter = RateLimiter(cfg.rpm, max(cfg.levels), float(os.environ.get("WAPSI_LLM_MIN_INTERVAL_S") or 0))
    async with httpx.AsyncClient(timeout=cfg.timeout_s, headers=headers, transport=transport,
                                 limits=limits) as client:
        model, card = await resolve_model(client, cfg.base_url, cfg.model)
        base_prompts = wapsi_prompts() if cfg.prompts == "wapsi" else None
        if cfg.warmup:
            warm = base_prompts[: cfg.warmup] if base_prompts else synthetic_prompts(
                cfg.warmup, min(cfg.input_len, 128), min(cfg.output_len, 16), seed=cfg.seed + 10_000)
            await run_level(client, cfg, model, max(1, len(warm)), warm, limiter)
        levels = []
        for i, level in enumerate(cfg.levels):
            n = cfg.requests_for(level)
            if base_prompts:
                prompts = [base_prompts[j % len(base_prompts)] for j in range(n)]
            else:
                prompts = synthetic_prompts(n, cfg.input_len, cfg.output_len, seed=cfg.seed + 1000 * (i + 1))
            res = await run_level(client, cfg, model, level, prompts, limiter)
            levels.append(res)
            if progress:
                print(f"  concurrency {level:>3}: {res['completed']}/{res['requests']} ok, "
                      f"TTFT p50 {res['ttft_s']['p50']}s, {res['output_tokens_per_s']} out tok/s"
                      + (f", {res['rate_limited']} HTTP 429" if res["rate_limited"] else "")
                      + (" (stream buffered by the gateway)" if res["stream_buffered"] else ""), flush=True)
    return {
        "kind": "llm_bench",
        "schema_version": SCHEMA_VERSION,
        "generated_at": started.isoformat(),
        "label": cfg.label,
        "base_url": cfg.base_url,
        "model": model,
        "model_root": card.get("root"),
        "max_model_len": card.get("max_model_len") or card.get("context_length"),
        "settings": {"levels": list(cfg.levels), "requests": cfg.requests, "input_len": cfg.input_len,
                     "output_len": cfg.output_len, "prompts": cfg.prompts, "ignore_eos": cfg.ignore_eos,
                     "temperature": cfg.temperature, "warmup": cfg.warmup, "seed": cfg.seed,
                     "rpm": cfg.rpm or None, "retry_429": cfg.retry_429, "json_mode": cfg.json_mode},
        "env": env_info(),
        "levels": levels,
    }


def to_markdown(result: dict[str, Any]) -> str:
    s = result["settings"]
    rows = []
    for lv in result["levels"]:
        rows.append([lv["concurrency"], f"{lv['completed']}/{lv['requests']}", lv["ttft_s"]["p50"],
                     lv["ttft_s"]["p95"], lv["itl_s"]["p50"], lv["tpot_s"]["p50"], lv["output_tokens_per_s"],
                     lv["output_tokens_per_s_per_user"], lv["requests_per_s"]])
    notes = []
    if any(lv.get("stream_buffered") for lv in result["levels"]):
        notes.append("- The gateway delivered streams in one burst: TTFT is the wait for that burst; ITL, TPOT and "
                     "tokens/s per user are not measurable and shown as —.")
    if any(lv.get("rate_limited") or lv.get("paced_s") for lv in result["levels"]):
        notes.append("- Some requests were rate-limited (HTTP 429, retried) or paced by --rpm: those levels are not "
                     "clean throughput measurements.")
    return "\n".join([
        f"# LLM serving benchmark — `{result['model']}`",
        "",
        *([f"- Label: **{result['label']}**"] if result.get("label") else []),
        f"- Endpoint: `{result['base_url']}` · model root: `{result.get('model_root')}` · "
        f"max_model_len: {result.get('max_model_len')}",
        f"- Prompts: {s['prompts']} · input ≈ {s['input_len']} tokens · output {s['output_len']} tokens · "
        f"ignore_eos={s['ignore_eos']} · generated {result['generated_at']}",
        "",
        md_table(["Concurrency", "OK", "TTFT p50 (s)", "TTFT p95 (s)", "ITL p50 (s)", "TPOT p50 (s)",
                  "Output tok/s", "Tok/s per user", "Req/s"], rows),
        "",
        *notes,
        *([""] if notes else []),
    ])


def _extra_body(model: str | None, base_url: str) -> dict | None:
    """``WAPSI_LLM_EXTRA_BODY`` (JSON), else what the model plan knows a provider-served model needs."""
    raw = (os.environ.get("WAPSI_LLM_EXTRA_BODY") or "").strip()
    if raw:
        return dict(json.loads(raw))
    from editions.amd.kickoff.endpoint import is_local
    from editions.amd.kickoff.model_plan import request_defaults

    return None if is_local(base_url) else (request_defaults(model)[0] or None)


def _json_mode(explicit: str | None, base_url: str) -> str:
    if explicit:
        return explicit
    from editions.amd.kickoff.endpoint import default_json_mode

    return default_json_mode(base_url)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.environ.get("WAPSI_LLM_BASE_URL", "http://127.0.0.1:8000/v1"))
    ap.add_argument("--model", default=None, help="served model name (default: first from /models)")
    ap.add_argument("--api-key", default=None, help="default: $WAPSI_LLM_API_KEY or $VLLM_API_KEY")
    ap.add_argument("--concurrency", type=int, nargs="+", default=list(DEFAULT_LEVELS))
    ap.add_argument("--requests", type=int, default=None, help="requests per level (default max(8, 4 x level))")
    ap.add_argument("--input-len", type=int, default=1024)
    ap.add_argument("--output-len", type=int, default=256)
    ap.add_argument("--prompts", choices=["synthetic", "wapsi"], default="synthetic")
    ap.add_argument("--no-ignore-eos", action="store_true", help="let generation stop at EOS")
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--rpm", type=float, default=float(os.environ.get("WAPSI_BENCH_RPM") or 0),
                    help="rate-limited endpoint: at most this many request starts per 60 s (0 = no limit)")
    ap.add_argument("--retry-429", type=int, default=3, help="retries of an HTTP 429 after its Retry-After")
    ap.add_argument("--json-mode", choices=["json_schema", "json_object"], default=None,
                    help="how --prompts wapsi asks for JSON (default: $WAPSI_LLM_JSON_MODE, else json_schema for a "
                         "server on this machine and json_object for a provider's endpoint)")
    ap.add_argument("--label", default=None,
                    help="where this was measured (stored in the result); 'auto' names the endpoint, the model "
                         "and today's date")
    ap.add_argument("--out", default=None, help="results directory (default bench/results/<utc stamp>)")
    ap.add_argument("--name", default="llm_bench",
                    help="output file stem, e.g. llm_bench_verifier (report.py picks up llm_bench*.json)")
    args = ap.parse_args(argv)
    if not args.name.startswith("llm_bench"):
        ap.error("--name must start with llm_bench")
    cfg = BenchConfig(
        base_url=args.base_url, model=args.model,
        api_key=args.api_key or os.environ.get("WAPSI_LLM_API_KEY") or os.environ.get("VLLM_API_KEY"),
        levels=tuple(args.concurrency), requests=args.requests, input_len=args.input_len,
        output_len=args.output_len, prompts=args.prompts, ignore_eos=not args.no_ignore_eos,
        timeout_s=args.timeout, warmup=args.warmup, seed=args.seed, rpm=max(0.0, args.rpm),
        retry_429=max(0, args.retry_429), label=args.label, json_mode=_json_mode(args.json_mode, args.base_url),
        extra_body=_extra_body(args.model, args.base_url))
    print(f"benchmarking {cfg.base_url} at concurrency {list(cfg.levels)}", flush=True)
    result = asyncio.run(run_bench(cfg, progress=True))
    if args.label == "auto":
        from editions.amd.kickoff.endpoint import measured_label

        result["label"] = measured_label(cfg.base_url, result["model"])
    out = results_dir(args.out)
    write_json(out / f"{args.name}.json", result)  # --name keeps a sweep's files apart (llm_bench_verifier, ...)
    (out / f"{args.name}.md").write_text(to_markdown(result), encoding="utf-8", newline="\n")
    print(to_markdown(result))
    print(f"wrote {out / (args.name + '.json')}")
    return 0 if all(lv["errors"] == 0 for lv in result["levels"]) else 1


if __name__ == "__main__":
    sys.exit(main())
