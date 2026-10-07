"""Tamper drill: build the synthetic showcase case, record a supervisor approval, then tamper with the stored
audit log in six ways and show that every one is detected.

    uv run python -m wapsi.audit drill [--json]

Offline (showcase ledger, ``FakeLLM``, ``InMemoryCaseStore``); exit status 0 only if the clean log verifies and
every tampering attempt is detected.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass

from wapsi.audit.chain import (
    CONTENT,
    ChainAnchor,
    ChainReport,
    link_hash,
    verify_chain,
)
from wapsi.models import AuditEvent

APPROVER = "ravi.supervisor"


@dataclass(frozen=True)
class DrillResult:
    name: str
    tamper: str  # what was done to the stored log
    detected: bool  # with the store's anchor
    code: str
    first_bad_seq: int | None
    reason: str
    detected_without_anchor: bool  # from the log alone


@dataclass(frozen=True)
class Drill:
    case_id: str
    events: int
    head: str
    clean: ChainReport
    approval_seq: int
    approval_fingerprint: str
    stored_edit: ChainReport  # the report after one event was edited inside the store itself
    results: list[DrillResult]

    @property
    def passed(self) -> bool:
        return self.clean.ok and not self.stored_edit.ok and all(r.detected for r in self.results)


def _copy(events: list[AuditEvent]) -> list[AuditEvent]:
    return [e.model_copy(deep=True) for e in events]


def _rehash_from(events: list[AuditEvent], start: int) -> None:
    """What a careful forger does: recompute every hash from ``start`` so each link looks valid."""
    for i in range(start, len(events)):
        e = events[i]
        e.seq = i
        e.prev_hash = events[i - 1].hash if i else e.prev_hash
        e.hash = link_hash(e, seq=i, prev_hash=e.prev_hash or "", case_id=e.case_id)


def scenarios(approval_seq: int) -> list[tuple[str, str, Callable[[list[AuditEvent]], None]]]:
    k = approval_seq

    def edit(ev: list[AuditEvent]) -> None:
        ev[k].details["draft_sha256"] = "0" * 64

    def edit_rehash(ev: list[AuditEvent]) -> None:
        ev[k].actor = "asha.analyst"
        ev[k].hash = link_hash(ev[k], seq=k, prev_hash=ev[k].prev_hash or "", case_id=ev[k].case_id)

    def delete(ev: list[AuditEvent]) -> None:
        del ev[k]

    def reorder(ev: list[AuditEvent]) -> None:
        ev[k - 1], ev[k] = ev[k], ev[k - 1]

    def truncate(ev: list[AuditEvent]) -> None:
        del ev[-2:]

    def rechain(ev: list[AuditEvent]) -> None:
        ev[k].details["reason_code"] = "BACKDATED"
        _rehash_from(ev, k)

    return [
        ("edit", "change the approved draft's fingerprint in the approval event", edit),
        ("edit+rehash", "change who approved and recompute that event's hash", edit_rehash),
        ("delete", "delete the approval event", delete),
        ("reorder", "swap the approval with the event before it", reorder),
        ("truncate", "drop the last two events", truncate),
        ("rechain", "edit the approval and recompute every later hash", rechain),
    ]


def run_drill() -> Drill:
    from wapsi.adapters.llm_fake import FakeLLM
    from wapsi.adapters.memory import InMemoryCaseStore, RecordingActionSink
    from wapsi.fixtures import showcase as sc
    from wapsi.models import DraftKind
    from wapsi.pipeline.run import approve, process_case

    ledger = sc.showcase_ledger()
    store = InMemoryCaseStore()
    bundle = process_case(ledger, [sc.N1], llm=FakeLLM(), store=store)
    hold = next(d for d in bundle.drafts if d.kind == DraftKind.HOLD_INSTRUCTION and d.account_id == sc.A)
    approve(bundle, hold.draft_id, approver=APPROVER, role="supervisor", reason_code="CHECKS_PASSED",
            sink=RecordingActionSink(), ledger=ledger)
    store.put_bundle(bundle)
    case_id = bundle.case.case_id

    clean = store.verify_audit(case_id)
    stored = store.get_bundle(case_id)
    assert stored is not None
    events = stored.audit
    anchor = store.audit_anchor(case_id) or ChainAnchor(len(events), events[-1].hash or "")
    approval_seq = next(i for i, e in enumerate(events) if e.action == "draft_approved")

    results: list[DrillResult] = []
    for name, what, tamper in scenarios(approval_seq):
        forged = _copy(events)
        tamper(forged)
        with_anchor = verify_chain(forged, case_id=case_id, anchor=anchor)
        alone = verify_chain(forged, case_id=case_id)
        results.append(DrillResult(name=name, tamper=what, detected=not with_anchor.ok, code=with_anchor.code,
                                   first_bad_seq=with_anchor.first_bad_seq, reason=with_anchor.reason,
                                   detected_without_anchor=not alone.ok))

    # finally the real thing: edit one event inside the store (an insider with database access), then verify
    store._bundles[case_id].audit[approval_seq].details["draft_sha256"] = "f" * 64
    stored_edit = store.verify_audit(case_id)

    return Drill(case_id=case_id, events=len(events), head=clean.head, clean=clean, approval_seq=approval_seq,
                 approval_fingerprint=str(events[approval_seq].details.get("draft_sha256")),
                 stored_edit=stored_edit, results=results)


def render(d: Drill) -> str:
    lines = [
        f"Wapsi audit-chain tamper drill - synthetic showcase case {d.case_id}",
        f"Clean log: {d.clean.summary()}",
        f"  head hash {d.head[:16]}...  approval at event #{d.approval_seq}, fingerprint "
        f"{d.approval_fingerprint[:16]}...",
        "",
        f"{'Tamper':<62} {'Detected':<9} {'At':<5} {'Code':<10} {'Log alone'}",
    ]
    for r in d.results:
        at = "-" if r.first_bad_seq is None else f"#{r.first_bad_seq}"
        lines.append(f"{r.tamper:<62} {'yes' if r.detected else 'NO':<9} {at:<5} {r.code:<10} "
                     f"{'yes' if r.detected_without_anchor else 'no (anchor needed)'}")
    lines += [
        "",
        f"Edited event #{d.approval_seq} inside the store: {d.stored_edit.summary()}",
        "",
        f"Result: {sum(r.detected for r in d.results)}/{len(d.results)} tampering attempts detected; "
        f"in-store edit {'detected' if not d.stored_edit.ok and d.stored_edit.code == CONTENT else 'MISSED'}. "
        + ("PASS" if d.passed else "FAIL"),
    ]
    return "\n".join(lines)


def as_json(d: Drill) -> dict:
    return {
        "case_id": d.case_id, "events": d.events, "head": d.head, "clean": asdict(d.clean),
        "approval_seq": d.approval_seq, "approval_fingerprint": d.approval_fingerprint,
        "stored_edit": asdict(d.stored_edit), "results": [asdict(r) for r in d.results], "passed": d.passed,
    }
