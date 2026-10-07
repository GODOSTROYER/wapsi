"""The judging world and the seeded showcase cases for the one-command Cloud Run deploy.

**Judging world.** The synthetic demo world (seed 7, 39 cases) plus the hand-computed showcase world (2 cases) in one
Parquet directory. Their account, customer, transaction, device and complaint ids are disjoint and they share the
same three synthetic banks, so every case resolves exactly as in its own world (tests check both golden sets). The
deploy loads it into BigQuery (``wapsi.adapters.bigquery_ledger load``) and the image bakes it for
``WAPSI_LEDGER=parquet``::

    uv run python -m editions.gcp.app.seed world --out data/generated/judging

**Seeded cases.** With ``WAPSI_SEED_CASES=showcase`` the API service processes the showcase cases once, in the
background, with the deployment's own ledger and LLM (Gemini writes the prose; numbers still come from data and the
verifier re-checks them), and stores them, so the judged URL opens on a ready case without anyone logging in.
Cases already in the store are never overwritten (approvals and audit history survive restarts).

All data is synthetic.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger("wapsi.gcp.seed")

SEED_ENV = "WAPSI_SEED_CASES"
SEED_CHOICES = ("", "showcase")
DEMO_SEED = 7
SHOWCASE_CASE_IDS = ("CASE-31009260000101", "CASE-31109260000102")
SUMMED_META = ("n_accounts", "n_transactions", "n_rings", "n_episodes", "n_complaints", "n_cases", "n_adversarial",
               "reference_warnings")


# --------------------------------------------------------------------------- judging world


def judging_frames(demo_seed: int = DEMO_SEED) -> dict[str, Any]:
    """Frames of the demo world (``demo_seed``) merged with the showcase world."""
    import pandas as pd

    from wapsi.synth.world import WorldConfig, build_world

    demo, _ = build_world(WorldConfig(name="demo", size="demo", seed=demo_seed))
    show, _ = build_world(WorldConfig(name="showcase", size="showcase", seed=0))
    out: dict[str, Any] = {}
    for table in sorted(set(demo) | set(show)):
        if table == "meta":
            continue
        parts = [f for f in (demo.get(table), show.get(table)) if f is not None and len(f)]
        if not parts:
            out[table] = demo.get(table, show.get(table))
            continue
        merged = pd.concat(parts, ignore_index=True)
        key = merged.columns[0]
        if table == "banks":  # the same synthetic banks in both worlds
            merged = merged.drop_duplicates(subset=[key], keep="first")
        out[table] = merged.reset_index(drop=True)
    dm = dict(zip(demo["meta"]["key"], demo["meta"]["value"], strict=True))
    sm = dict(zip(show["meta"]["key"], show["meta"]["value"], strict=True))
    meta = dict(dm)
    for k in SUMMED_META:
        if k in dm or k in sm:
            meta[k] = str(int(float(dm.get(k) or 0)) + int(float(sm.get(k) or 0)))
    meta.update({
        "world": "judging",
        "size": "judging",
        "world_start": min(dm["world_start"], sm["world_start"]),
        "world_end": max(dm["world_end"], sm["world_end"]),
        "source": f"wapsi.synth demo seed {demo_seed} + wapsi.fixtures.showcase (editions.gcp.app.seed)",
    })
    out["meta"] = pd.DataFrame({"key": list(meta), "value": [str(v) for v in meta.values()]})
    return out


def build_judging_world(out: str | Path, demo_seed: int = DEMO_SEED) -> dict[str, int]:
    """Write the judging world to ``out``; returns rows per table. Idempotent (deterministic bytes)."""
    from wapsi.synth.export import write_frames

    frames = judging_frames(demo_seed)
    write_frames(frames, out)
    return {t: len(f) for t, f in frames.items() if t != "meta"}


# --------------------------------------------------------------------------- seeded cases


def showcase_groups() -> list[tuple[str, list[str], str, Any]]:
    """(case_id, ncrp_ids, assigned_to, opened_at) of the showcase cases."""
    from wapsi.fixtures.bundles import all_showcase_bundles

    return [(b.case.case_id, sorted(b.case.ncrp_ids), b.case.assigned_to or "nodal.officer", b.case.opened_at)
            for b in all_showcase_bundles()]


def seed_cases(services: Any, which: str = "showcase") -> dict[str, str]:
    """Process and store the ``which`` cases that are not in the store yet. Returns ``{case_id: outcome}``
    (``seeded`` | ``exists`` | ``missing_in_ledger`` | ``error: ...``). Never overwrites a stored case."""
    from wapsi.pipeline.run import process_case

    if which not in ("showcase",):
        raise ValueError(f"{SEED_ENV} must be one of {', '.join(c for c in SEED_CHOICES if c)} (got {which!r})")
    out: dict[str, str] = {}
    for case_id, ncrp_ids, assigned, opened_at in showcase_groups():
        try:
            if services.store.get_bundle(case_id) is not None:
                out[case_id] = "exists"
                continue
            try:
                for n in ncrp_ids:
                    services.ledger.complaint(n)
            except KeyError:
                out[case_id] = "missing_in_ledger"
                log.warning("seed: %s is not in the ledger (load the judging world: editions.gcp.app.seed world)",
                            case_id)
                continue
            with services.lock:
                if services.store.get_bundle(case_id) is not None:
                    out[case_id] = "exists"
                    continue
                bundle = process_case(services.ledger, ncrp_ids, llm=services.llm, cfg=services.cfg,
                                      search=services.search, guard=services.guard, case_id=case_id,
                                      assigned_to=assigned)
                bundle.case.opened_at = opened_at  # the notice time of the showcase story (see WAPSI_NOW)
                services.store.put_bundle(bundle)
            out[case_id] = "seeded"
        except Exception as exc:  # noqa: BLE001 - seeding must never take the service down
            log.exception("seed: %s failed", case_id)
            out[case_id] = f"error: {type(exc).__name__}"
    return out


class SeedState:
    """Status of the background seeding, for ``/demo/status``."""

    def __init__(self) -> None:
        self.state = "idle"  # idle | running | done | failed
        self.results: dict[str, str] = {}
        self.started_at: float | None = None
        self.seconds: float | None = None
        self._thread: threading.Thread | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "results": dict(self.results), "seconds": self.seconds}

    def start(self, services: Any, which: str, *, background: bool = True) -> None:
        if not which or self.state == "running":
            return

        self.state, self.started_at, self.seconds = "running", time.monotonic(), None

        def run() -> None:
            try:
                self.results = seed_cases(services, which)
                failed = any(v.startswith("error") for v in self.results.values())
                self.state = "failed" if failed else "done"
            except Exception:  # noqa: BLE001
                log.exception("seed: failed")
                self.state = "failed"
            finally:
                self.seconds = round(time.monotonic() - (self.started_at or time.monotonic()), 2)
            log.info("seed %s: %s", which, self.results)

        if background:
            self._thread = threading.Thread(target=run, name="wapsi-seed", daemon=True)
            self._thread.start()
        else:
            run()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


def seed_choice(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v not in SEED_CHOICES:
        raise ValueError(f"{SEED_ENV} must be empty or one of {', '.join(c for c in SEED_CHOICES if c)} (got {v!r})")
    return v


# --------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m editions.gcp.app.seed", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("world", help="write the judging world (demo seed 7 + showcase) as Parquet")
    w.add_argument("--out", default="data/generated/judging")
    w.add_argument("--seed", type=int, default=DEMO_SEED)
    args = ap.parse_args(argv)
    counts = build_judging_world(args.out, args.seed)
    print(f"judging world -> {args.out}: " + ", ".join(f"{t}={n}" for t, n in sorted(counts.items())
                                                       if not t.startswith("labels_")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
