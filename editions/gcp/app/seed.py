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
Cases already in the store are never overwritten (approvals and audit history survive restarts), with one
exception the deploy's smoke test asks for (``redo_fallback``): a stored case whose prose fell back to the
deterministic paragraphs (Gemini was not reachable, e.g. a missing key or a wrong model id on the first deploy) is
processed again onto its history (one audit hash chain, earlier approvals kept), so fixing the key and re-running
the smoke test puts Gemini prose in front of the judges instead of the fallback forever.

All data is synthetic.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger("wapsi.gcp.seed")

SEED_ENV = "WAPSI_SEED_CASES"
SEED_CHOICES = ("", "showcase")
DEMO_SEED = 7
SHOWCASE_CASE_IDS = ("CASE-31009260000101", "CASE-31109260000102")
SNAPSHOT_COLLECTION = "demo_snapshots"  # the freshly seeded copy of each showcase case (what a reset restores)
LOCK_COLLECTION, LOCK_ID = "demo_locks", "seed-showcase"  # the seeding lease (one seeder or resetter at a time)
LEASE_TTL_S = 180.0  # a lease nobody renewed for this long belongs to a dead instance and is taken over
LEASE_WAIT_S = 200.0  # how long a seeder waits for another instance's run (it then finds the cases in place)
RESET_ACTION = "demo_reset"
IDLE_RESET_ENV = "WAPSI_DEMO_IDLE_RESET_MIN"
DEFAULT_IDLE_RESET_MIN = 30.0
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


def prose_fell_back(bundle: Any) -> bool:
    """True when any draft's LLM prose was replaced by the deterministic paragraph (``llm_fallback``)."""
    return any(bool((d.structured or {}).get("llm_fallback")) for d in bundle.drafts)


def snapshot_store(services: Any) -> Any:
    """Where the freshly seeded copy of each showcase case is kept: what a reset restores. On Cloud Run a second
    Firestore collection next to the cases (``demo_snapshots``, never listed by the UI), so the API and the UI
    service restore the same copy; elsewhere an in-memory store held by the services."""
    snaps = getattr(services, "_demo_snapshots", None)
    if snaps is None:
        from wapsi.adapters.firestore_store import FirestoreCaseStore
        from wapsi.adapters.memory import InMemoryCaseStore

        store = services.store
        if isinstance(store, FirestoreCaseStore):
            snaps = FirestoreCaseStore(store.client, SNAPSHOT_COLLECTION, project=store.project,
                                       database=store.database)
        else:
            snaps = InMemoryCaseStore()
        services._demo_snapshots = snaps
    return snaps


def _snapshot(services: Any, case_id: str) -> Any | None:
    try:
        return snapshot_store(services).get_bundle(case_id)
    except Exception:  # noqa: BLE001 - an unreadable snapshot is re-made from the ledger
        log.warning("seed: the snapshot of %s could not be read; it will be processed again", case_id,
                    exc_info=True)
        return None


def _save_snapshot(services: Any, bundle: Any) -> None:
    try:
        snapshot_store(services).put_bundle(bundle.model_copy(deep=True))
    except Exception:  # noqa: BLE001 - the case itself is stored; a later reset re-processes instead
        log.warning("seed: could not keep the snapshot of %s", bundle.case.case_id, exc_info=True)


class SeedLease:
    """One seeder or resetter at a time, across processes and Cloud Run revisions.

    During a redeploy the old and the new revision of the API both start seeding, and the UI service resets from
    its own process. On Firestore the lease is a document (``demo_locks/seed-showcase``: holder and expiry) taken
    and released in transactions, so exactly one of them works at a time and the others wait for it (then find
    the cases in place: seeding is idempotent). A lease whose holder died (an instance stopped mid-seed) expires
    after ``ttl_s`` and is taken over; the holder renews it after every case. Without Firestore (demo mode, tests)
    it is a process-wide lock. If Firestore cannot be reached the lease is skipped with a warning: seeding never
    takes the service down."""

    _local = threading.Lock()

    def __init__(self, services: Any, *, ttl_s: float = LEASE_TTL_S, holder: str | None = None) -> None:
        from wapsi.adapters.firestore_store import FirestoreCaseStore

        store = services.store
        self.ttl_s = float(ttl_s)
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._ref: Any = None
        self._client: Any = None
        if isinstance(store, FirestoreCaseStore):
            self._client = store.client
            self._ref = self._client.collection(LOCK_COLLECTION).document(LOCK_ID)
        self.held = False
        self._local_held = False

    def _transact(self, fn: Any) -> Any:
        from google.cloud.firestore import transactional

        return transactional(fn)(self._client.transaction(max_attempts=5))

    def _take(self) -> bool:
        ref, now = self._ref, time.time()

        def take(txn: Any) -> bool:
            snap = ref.get(transaction=txn)
            data = (snap.to_dict() or {}) if getattr(snap, "exists", False) else {}
            if data and data.get("holder") != self.holder and float(data.get("expires_at") or 0) > now:
                return False
            txn.set(ref, {"holder": self.holder, "expires_at": now + self.ttl_s, "renewed_at": now,
                          "purpose": "seed or reset the showcase cases (editions.gcp.app.seed)"})
            return True

        return bool(self._transact(take))

    def acquire(self, wait_s: float = LEASE_WAIT_S, poll_s: float = 1.0) -> bool:
        if self._ref is None:
            self._local_held = self._local.acquire(timeout=max(0.0, wait_s))
            self.held = self._local_held
            return self.held
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            try:
                if self._take():
                    self.held = True
                    return True
            except Exception:  # noqa: BLE001 - fail open: idempotent seeding beats no seeding
                log.warning("seed: the Firestore lease could not be taken; seeding without it", exc_info=True)
                self._ref = None
                self.held = True
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_s)

    def renew(self) -> None:
        if self._ref is not None and self.held:
            try:
                self._take()
            except Exception:  # noqa: BLE001
                log.warning("seed: could not renew the lease", exc_info=True)

    def release(self) -> None:
        if self._local_held:
            self._local_held = False
            self._local.release()
        if self._ref is not None and self.held:
            ref = self._ref

            def drop(txn: Any) -> None:
                snap = ref.get(transaction=txn)
                if getattr(snap, "exists", False) and (snap.to_dict() or {}).get("holder") == self.holder:
                    txn.delete(ref)

            try:
                self._transact(drop)
            except Exception:  # noqa: BLE001 - the lease then expires on its own
                log.warning("seed: could not release the lease", exc_info=True)
        self.held = False


def seed_cases(services: Any, which: str = "showcase", *, redo_fallback: bool = False, reset: bool = False,
               actor: str = "wapsi.demo", role: str = "system", reason: str = "",
               wait_s: float = LEASE_WAIT_S) -> dict[str, str]:
    """Process and store the ``which`` cases. Returns ``{case_id: outcome}``: ``seeded`` (it was not in the store),
    ``exists`` (kept as is), ``reseeded`` (``redo_fallback``: its prose had fallen back, so it was processed again
    onto its history, approvals kept), ``reset`` (``reset``: restored to its freshly seeded state), ``fresh``
    (``reset``, but nobody had worked on it since it was seeded or reset: left as is), ``missing_in_ledger``, ``busy`` (another instance held the seeding lease for ``wait_s``) or ``error: ...``.

    A reset restores the snapshot taken when the case was seeded (plan, drafts and verifications as computed, no
    approvals), so every visitor of the shared demo starts on a fresh case. The audit log stays append-only: it
    keeps every earlier event, including each decision, and records the reset as a ``demo_reset`` event."""
    if which not in ("showcase",):
        raise ValueError(f"{SEED_ENV} must be one of {', '.join(c for c in SEED_CHOICES if c)} (got {which!r})")
    groups = showcase_groups()
    lease = SeedLease(services)
    if not lease.acquire(wait_s):
        log.warning("seed: another instance is seeding or resetting; gave up after %.0f s", wait_s)
        return {case_id: "busy" for case_id, *_ in groups}
    out: dict[str, str] = {}
    try:
        for case_id, ncrp_ids, assigned, opened_at in groups:
            try:
                out[case_id] = _seed_one(services, case_id, ncrp_ids, assigned, opened_at,
                                         redo_fallback=redo_fallback, reset=reset, actor=actor, role=role,
                                         reason=reason)
            except Exception as exc:  # noqa: BLE001 - seeding must never take the service down
                log.exception("seed: %s failed", case_id)
                out[case_id] = f"error: {type(exc).__name__}"
            lease.renew()
    finally:
        lease.release()
    return out


def _seed_one(services: Any, case_id: str, ncrp_ids: list[str], assigned: str, opened_at: Any, *,
              redo_fallback: bool, reset: bool, actor: str, role: str, reason: str) -> str:
    from wapsi.audit.chain import chain_extend
    from wapsi.models import AuditEvent
    from wapsi.pipeline.run import process_case
    from wapsi.timeutil import utcnow

    store = services.store
    stored = store.get_bundle(case_id)
    stale = stored is not None and redo_fallback and prose_fell_back(stored)
    if stored is not None and not reset and not stale:
        return "exists"
    try:
        for n in ncrp_ids:
            services.ledger.complaint(n)
    except KeyError:
        log.warning("seed: %s is not in the ledger (load the judging world: editions.gcp.app.seed world)", case_id)
        return "missing_in_ledger"
    with services.lock:
        pristine = _snapshot(services, case_id)
        if (stored is not None and reset and not stale and pristine is not None
                and not (redo_fallback and prose_fell_back(pristine)) and is_fresh(stored, pristine)):
            return "fresh"  # nobody has worked on it since it was seeded or reset: nothing to undo
        run_events: list[Any] = []
        if pristine is None or (redo_fallback and prose_fell_back(pristine)) or (stale and not reset):
            fresh = process_case(services.ledger, ncrp_ids, llm=services.llm, cfg=services.cfg,
                                 search=services.search, guard=services.guard, case_id=case_id,
                                 assigned_to=assigned)
            fresh.case.opened_at = opened_at  # the notice time of the showcase story (see WAPSI_NOW)
            _save_snapshot(services, fresh)
            base, run_events = fresh, list(fresh.audit)
        else:
            base = pristine
        bundle = base.model_copy(deep=True)
        if stored is None:
            store.put_bundle(bundle)
            return "seeded"
        if reset:
            event = AuditEvent(
                at=utcnow(), actor=actor, role=role, action=RESET_ACTION, object_type="case", object_id=case_id,
                details={"approvals_cleared": len(stored.approvals),
                         "restored": "freshly processed" if run_events else "the copy taken when it was seeded",
                         "reason": reason or "shared demo: a fresh case for the next visitor",
                         "note": "earlier events, including every decision, stay in this log"})
            bundle.approvals = []
            bundle.audit = chain_extend(stored.audit, [*run_events, event], case_id=case_id)
            store.put_bundle(bundle)
            return "reset"
        # redo_fallback over a stored case: the new run goes onto its history, earlier approvals kept
        bundle.approvals = list(stored.approvals) + list(bundle.approvals)
        bundle.audit = chain_extend(stored.audit, run_events, case_id=case_id)
        store.put_bundle(bundle)
        return "reseeded"


def is_fresh(stored: Any, pristine: Any | None) -> bool:
    """True when nobody has worked on the stored case since it was seeded or reset: no decisions, and the same
    drafts and status as the snapshot (an account-level request or a re-run changes them)."""
    if stored.approvals:
        return False
    if pristine is None:
        return True
    return (stored.case.status == pristine.case.status
            and sorted(d.draft_id for d in stored.drafts) == sorted(d.draft_id for d in pristine.drafts))


def last_activity(bundle: Any) -> Any | None:
    """Time of the case's newest audit event (page views count: someone is looking at it)."""
    from wapsi.timeutil import ensure_utc

    times = [ensure_utc(e.at) for e in bundle.audit if e.at is not None]
    return max(times) if times else None


def idle_reset_seconds() -> float:
    """``WAPSI_DEMO_IDLE_RESET_MIN`` (default 30; 0 = off), in seconds."""
    raw = os.environ.get(IDLE_RESET_ENV, "").strip()
    try:
        minutes = float(raw) if raw else DEFAULT_IDLE_RESET_MIN
    except ValueError:
        log.warning("%s=%r is not a number of minutes; using %s", IDLE_RESET_ENV, raw, DEFAULT_IDLE_RESET_MIN)
        minutes = DEFAULT_IDLE_RESET_MIN
    return max(0.0, minutes) * 60.0


def reset_if_idle(services: Any, idle_s: float | None = None, *, now: Any = None) -> dict[str, str] | None:
    """For a new visitor of the shared demo: reset the showcase cases when one of them was worked on (decisions,
    changed drafts) and nobody has touched any of them for ``idle_s`` seconds. Returns the reset's outcomes, or
    ``None`` when nothing needed resetting (fresh cases, recent activity, or idle reset turned off)."""
    from datetime import timedelta

    from wapsi.timeutil import utcnow

    idle_s = idle_reset_seconds() if idle_s is None else float(idle_s)
    if idle_s <= 0:
        return None
    now = now or utcnow()
    worked_on, latest = 0, None
    for case_id, *_ in showcase_groups():
        stored = services.store.get_bundle(case_id)
        if stored is None:
            continue
        seen = last_activity(stored)
        if seen is not None and (latest is None or seen > latest):
            latest = seen
        if not is_fresh(stored, _snapshot(services, case_id)):
            worked_on += 1
    if not worked_on or (latest is not None and now - latest < timedelta(seconds=idle_s)):
        return None
    minutes = round(idle_s / 60)
    return seed_cases(services, "showcase", reset=True, actor="wapsi.demo", role="system", wait_s=10.0,
                      reason=f"shared demo: idle for {minutes}+ minutes, so the next visitor gets a fresh case")


class SeedState:
    """Status of the background seeding, for ``/demo/status``."""

    def __init__(self) -> None:
        self.state = "idle"  # idle | running | done | failed
        self.results: dict[str, str] = {}
        self.started_at: float | None = None
        self.seconds: float | None = None
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "results": dict(self.results), "seconds": self.seconds}

    def start(self, services: Any, which: str, *, background: bool = True, redo_fallback: bool = False,
              reset: bool = False) -> bool:
        """Start one run (a no-op while one is running); returns whether this call started it."""
        with self._guard:
            if not which or self.state == "running":
                return False
            self.state, self.started_at, self.seconds = "running", time.monotonic(), None

        def run() -> None:
            try:
                self.results = seed_cases(services, which, redo_fallback=redo_fallback, reset=reset)
                bad = any(v.startswith("error") or v == "busy" for v in self.results.values())
                self.state = "failed" if bad else "done"
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
        return True

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
