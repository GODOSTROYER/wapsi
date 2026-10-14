"""Record the prose an open model writes for the demo cases, so the public demo can replay it offline.

    # the endpoint comes from the environment (never from a file in the repository):
    #   WAPSI_LLM_BASE_URL  WAPSI_LLM_API_KEY  WAPSI_LLM_MODEL      (wapsi.adapters.llm_env)
    uv run python scripts/record_demo_prose.py                         # the two showcase cases
    uv run python scripts/record_demo_prose.py --source demo --max-cases 12   # the demo world's first 12 cases
    uv run python scripts/record_demo_prose.py --dry-run               # what would be asked; no call is made

For every case it builds the hold plan exactly as the app does, lets the configured model write each prose field
(``wapsi.draft.drafter.draft_case``: placeholders in, validated text out) and stores the accepted answers in
``data/recordings/demo_prose.json`` (``wapsi.adapters.llm_replay.Recording``). Answers that are already recorded are
not asked again (``--refresh`` asks anyway); an answer the validator rejected twice is not recorded. A recording
holds prose with ``⟦slot⟧`` placeholders only: no amounts, no account numbers, no key. All cases are synthetic.

Good manners on a shared key: requests are sequential, ``--min-interval`` seconds apart (default 4), at most
``--rpm`` a minute (default 12), HTTP 429 / 503 are retried after the server's ``Retry-After``, every answer is
cached under ``.cache/llm`` (a rerun costs nothing) and ``--max-calls`` (default 40) stops the run.

The run prints what it measured (calls, latency, tokens, the cost the endpoint reported) and keeps it in the
recording's ``meta.runs``. These are measurements of the endpoint that served the answers: label them with it.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

DEFAULT_OUT = ROOT / "data" / "recordings" / "demo_prose.json"
DEFAULT_CACHE = ROOT / ".cache" / "llm"


class BudgetSpent(RuntimeError):
    """The run's live-call budget (``--max-calls``) is used up."""


class Budgeted:
    """The live LLM with a cap on the calls it may send (cache hits are free)."""

    def __init__(self, llm: Any, max_calls: int) -> None:
        self.llm = llm
        self.max_calls = max_calls
        self.name = llm.name

    @property
    def last_from_cache(self) -> bool:
        return bool(getattr(self.llm, "last_from_cache", False))

    def complete(self, messages, **kwargs):  # noqa: ANN001, ANN003 - wapsi.ports.LLM
        if self.max_calls and self.llm.stats.api_calls >= self.max_calls:
            raise BudgetSpent(f"live-call budget of {self.max_calls} spent")
        return self.llm.complete(messages, **kwargs)


def case_groups(source: str, max_cases: int | None) -> tuple[Any, list[tuple[str | None, list[str], str | None]]]:
    """(ledger, [(case_id, ncrp_ids, assigned_to)]) of a data source, the way ``ui.data_source`` builds its cases."""
    from ui.data_source import resolve_source, world_case_groups

    spec = resolve_source(source)
    if spec.kind != "world" or spec.path is None:
        from wapsi.fixtures.bundles import all_showcase_bundles
        from wapsi.fixtures.showcase import showcase_ledger

        groups = [(b.case.case_id, sorted(b.case.ncrp_ids), b.case.assigned_to) for b in all_showcase_bundles()]
        return showcase_ledger(), groups[:max_cases] if max_cases else groups
    from wapsi.adapters.parquet_ledger import ParquetLedger

    ledger = ParquetLedger(spec.path)
    return ledger, [(None, ids, None) for ids in world_case_groups(ledger, spec.path, max_cases)]


def record_source(source: str, recording: Any, live: Any | None, *, max_cases: int | None = None,
                  refresh: bool = False, search: Any | None = None, log=print) -> dict[str, Any]:
    """Record the prose of every case of ``source`` into ``recording``. ``live=None`` is a dry run: nothing is
    asked, the result says how many answers are recorded and how many are missing. Returns the run's counts."""
    from wapsi.adapters.llm_fake import FakeLLM
    from wapsi.adapters.llm_replay import LIVE, DemoLLM, LiveGate, prose_signature
    from wapsi.draft.drafter import draft_case
    from wapsi.pipeline.run import process_case

    ledger, groups = case_groups(source, max_cases)
    gate = LiveGate(cooldown_s=3600.0, max_cooldown_s=3600.0)  # a failed live call ends the live part of the run
    out: dict[str, Any] = {"source": source, "cases": 0, "fields": 0, "recorded": 0, "already": 0, "rejected": 0,
                           "missing": 0, "latency_s": []}
    for case_id, ncrp_ids, assigned in groups:
        bundle = process_case(ledger, ncrp_ids, llm=FakeLLM(), search=search, case_id=case_id, assigned_to=assigned,
                              draft=False)
        llm = DemoLLM(live=live, recording=None if refresh else recording, stub=None if live is not None else FakeLLM(),
                      prefer="recorded", gate=gate)
        exchanges: list[Any] = []
        draft_case(bundle, ledger, llm, search, prose_log=exchanges)
        out["cases"] += 1
        for ex in exchanges:
            out["fields"] += 1
            sig = prose_signature(ex.request)
            lives = [a for a in llm.answers if a.signature == sig and a.source == LIVE]
            if sig is None:
                continue
            if not lives:
                known = recording.get(sig) is not None
                out["already" if known else "missing"] += 1
                continue
            accepted = ex.accepted
            if ex.outcome.fallback or accepted is None or accepted.response is None:
                out["rejected"] += 1
                log(f"  {bundle.case.case_id} {ex.task}: answer rejected ({', '.join(ex.outcome.issues[-3:])})")
                continue
            latency = lives[-1].latency_s if not lives[-1].from_cache else None
            recording.put(sig, accepted.response, served_by=str(live.name), task=ex.task,
                          case_id=bundle.case.case_id, draft_id=ex.draft_id, attempts=ex.outcome.attempts,
                          latency_s=latency)
            out["recorded"] += 1
            out["latency_s"] += [a.latency_s for a in lives if a.latency_s is not None and not a.from_cache]
        log(f"  {bundle.case.case_id}: {len(exchanges)} prose field(s)")
    return out


def measurements(out: dict[str, Any], live: Any, cfg: Any) -> dict[str, Any]:
    """What this run measured, for ``meta.runs`` (non-secret)."""
    lat = sorted(out["latency_s"])
    rep = live.report() if hasattr(live, "report") else {}
    today = datetime.now(UTC).date().isoformat()
    return {
        "measured_on": today,
        "label": f"{cfg.host}, {cfg.model}, measured {today}",
        "served_by": cfg.served_name,
        "source": out["source"], "cases": out["cases"], "prose_fields": out["fields"],
        "recorded": out["recorded"], "already_recorded": out["already"], "rejected": out["rejected"],
        "api_calls": rep.get("api_calls"), "cache_hits": rep.get("cache_hits"), "retries": rep.get("retries"),
        "rate_limited_429": rep.get("rate_limited_429"),
        "latency_s": ({"n": len(lat), "p50": round(statistics.median(lat), 3), "min": lat[0], "max": lat[-1]}
                      if lat else None),
        "tokens": rep.get("tokens"), "reported_cost_usd": rep.get("reported_cost_usd"),
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append", default=None,
                    help="showcase (default) | demo | <world name or path>; repeat for several")
    ap.add_argument("--max-cases", type=int, default=12, help="cases per world source (0 = all; default 12, what "
                    "the app shows)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE), help="prompt-hash cache of the live answers")
    ap.add_argument("--rpm", type=int, default=12)
    ap.add_argument("--min-interval", type=float, default=4.0, help="seconds between two live calls")
    ap.add_argument("--max-calls", type=int, default=40, help="stop asking after this many live calls (0 = no cap)")
    ap.add_argument("--refresh", action="store_true", help="ask again for answers that are already recorded")
    ap.add_argument("--dry-run", action="store_true", help="make no call: report what is recorded and what is not")
    args = ap.parse_args(argv)

    from wapsi.adapters.llm_env import SETUP_MESSAGE, endpoint_config, resilient_endpoint_llm
    from wapsi.adapters.llm_replay import Recording, prose_answer_problem

    recording = Recording.load(args.out)
    recording.path = args.out
    cfg = endpoint_config()
    live = None
    if not args.dry_run:
        if not cfg.configured:
            print(SETUP_MESSAGE, file=sys.stderr)
            return 2
        # accept: an HTTP 200 with degenerate output is refused, so it is neither cached nor recorded
        inner = resilient_endpoint_llm(cfg, cache_dir=args.cache_dir, rpm=args.rpm, min_interval_s=args.min_interval,
                                       read_cache=not args.refresh, accept=prose_answer_problem)
        live = Budgeted(inner, args.max_calls)
        print(f"recording from {cfg.served_name} (key {'set' if cfg.has_key else 'not set'})")
    search = None
    try:
        from wapsi.search.index import LocalDocSearch

        search = LocalDocSearch.load()
    except Exception:  # noqa: BLE001 - the index is optional; prose does not depend on it
        search = None

    runs = []
    for source in args.source or ["showcase"]:
        print(f"{source}:")
        out = record_source(source, recording, live, max_cases=args.max_cases or None, refresh=args.refresh,
                            search=search)
        print(f"  {out['cases']} case(s), {out['fields']} prose field(s): {out['recorded']} recorded now, "
              f"{out['already']} already recorded, {out['rejected']} rejected, {out['missing']} not recorded")
        runs.append(out)
    if live is None:
        return 0
    for out in runs:
        m = measurements(out, live.llm, cfg)
        recording.meta.setdefault("runs", []).append(m)
        lat = m["latency_s"]
        print(f"measured ({m['label']}): {m['api_calls']} API call(s) so far, {m['cache_hits']} cache hit(s), "
              f"{m['retries']} retries, {m['rate_limited_429']} rate-limited"
              + (f"; latency p50 {lat['p50']} s (min {lat['min']}, max {lat['max']}, n={lat['n']})" if lat else "")
              + (f"; tokens {m['tokens']}" if m.get("tokens") else "")
              + (f"; endpoint-reported cost ${m['reported_cost_usd']:.6f}" if m.get("reported_cost_usd") else ""))
    if any(r["recorded"] for r in runs):
        path = recording.save(args.out)
        print(f"wrote {path} ({len(recording)} recorded answer(s))")
    else:
        print("nothing new to record")
    return 0


if __name__ == "__main__":
    sys.exit(main())
