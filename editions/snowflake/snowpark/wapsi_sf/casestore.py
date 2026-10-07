"""``SnowflakeCaseStore``: ``wapsi.ports.CaseStore`` over ``WAPSI.CASEMGMT``.

``CASE_BUNDLES.bundle`` (the full ``CaseBundle`` JSON) is the source of truth for Python; the other tables are
flattened, query-friendly copies for SQL, Cortex Analyst (the semantic view) and the Streamlit app. The one
exception is the audit log: ``AUDIT_LOG`` is the record, and ``get_bundle`` reads a case's events from it.

Writes use one statement per table: the rows travel as a single JSON bind and are cast in SQL
(``INSERT ... SELECT f.value:"col"::TYPE FROM TABLE(FLATTEN(INPUT => PARSE_JSON(?))) f``), so no value is ever
formatted into SQL text. ``APPROVALS`` is append-only (rows inserted once, deterministic ids); ``ACTIONS`` rows are
inserted once and later updated only by the actions runner. The other case tables are replaced per case
(DELETE ... WHERE case_id = ? then INSERT).

Audit chain (``wapsi.audit``), exactly like ``InMemoryCaseStore``: every write seals the bundle's new events onto
the case's stored chain (``extend_stored``: recorded events are never dropped, changed or reordered) and
``append_audit`` seals one event as the next link. ``AUDIT_LOG`` rows carry the seal (``seq``, ``prev_hash``,
``hash``) and are INSERT-only; after every append the store INSERTs a checkpoint (event count + head hash) into
``AUDIT_ANCHOR``, so ``verify_audit`` also catches events removed from the end and a log whose hashes were all
recomputed. Details are stored JSON-stable (integral floats as integers) so a digest survives the VARIANT round
trip. One writer per case at a time: two concurrent appends would both take the next ``seq``, and the verifier
reports the duplicate (``SEQUENCE``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic_core import to_jsonable_python

from wapsi.adapters.snowflake_ledger import row_dict, ts_param
from wapsi.audit import ChainAnchor, ChainReport, anchor_of, append_event, extend_stored, verify_chain
from wapsi.models import (
    Action,
    Approval,
    AuditEvent,
    Case,
    CaseBundle,
    CaseStatus,
)
from wapsi.money import r2
from wapsi.timeutil import ensure_utc, utcnow

DATABASE = "WAPSI"
M = "NUMBER(18,2)"
S = "STRING"
TS = "TIMESTAMP_NTZ"

# table -> [(column, Snowflake type)]; sql/20_casemgmt.sql must declare exactly these columns (tested).
COLUMNS: dict[str, list[tuple[str, str]]] = {
    "CASES": [
        ("case_id", S), ("world", S), ("raw_schema", S), ("ncrp_ids", "ARRAY"), ("opened_at", TS),
        ("status", S), ("assigned_to", S), ("priority_amount", M), ("rule", S),
        ("stolen", M), ("holdable", M), ("cashed_out", M), ("outbound", M), ("intermediary", M),
        ("merchant_settled", M), ("unlocated", M),
        ("total_hold", M), ("total_forward_hold", M), ("total_recoverable", M), ("over_hold_amount", M),
        ("baseline_freeze_amount", M), ("baseline_freeze_accounts", "NUMBER"),
        ("restoration_process", S), ("str_ground", S), ("n_accounts", "NUMBER"), ("n_edges", "NUMBER"),
        ("updated_at", TS),
    ],
    "CASE_BUNDLES": [("case_id", S), ("bundle", "VARIANT"), ("version", "NUMBER"), ("updated_at", TS)],
    "TRAIL_EDGES": [
        ("case_id", S), ("txn_id", S), ("ts", TS), ("from_account", S), ("to_account", S),
        ("from_bank_id", S), ("to_bank_id", S), ("channel", S), ("utr", S), ("amount", M), ("depth", "NUMBER"),
        ("tainted", "VARIANT"), ("tainted_total", M), ("via_consortium", "BOOLEAN"),
    ],
    "ACCOUNT_STATE": [
        ("case_id", S), ("account_id", S), ("bank_id", S), ("account_type", S), ("is_home_bank", "BOOLEAN"),
        ("layer", "NUMBER"), ("first_tainted_at", TS), ("balance_now", M), ("tainted_now", "VARIANT"),
        ("tainted_now_total", M), ("tainted_received_total", M), ("tainted_sent_total", M), ("terminal", S),
        ("parents", "ARRAY"), ("branch_city", S),
    ],
    "CLASSIFICATIONS": [
        ("case_id", S), ("account_id", S), ("role", S), ("score", "FLOAT"), ("reasons", "ARRAY"), ("model", S),
    ],
    "HOLD_PLAN": [
        ("case_id", S), ("account_id", S), ("bank_id", S), ("account_type", S), ("branch_city", S),
        ("disposition", S), ("hold_amount", M), ("forward_hold_amount", M), ("tainted_amount", M),
        ("balance_now", M), ("layer", "NUMBER"), ("role", S), ("per_victim", "VARIANT"),
        ("citation_ids", "ARRAY"), ("reason_codes", "ARRAY"), ("requires_human", "BOOLEAN"), ("notes", S),
        ("created_at", TS),
    ],
    "DRAFTS": [
        ("draft_id", S), ("case_id", S), ("kind", S), ("account_id", S), ("title", S), ("body", S),
        ("slots", "VARIANT"), ("structured", "VARIANT"), ("citation_ids", "ARRAY"), ("llm_model", S),
        ("created_at", TS), ("version", "NUMBER"), ("created_by", S), ("draft_sha256", S),
    ],
    "VERIFICATIONS": [
        ("draft_id", S), ("case_id", S), ("passed", "BOOLEAN"), ("checks", "VARIANT"), ("verified_at", TS),
    ],
    "APPROVALS": [
        ("approval_id", S), ("case_id", S), ("draft_id", S), ("decision", S), ("approver", S),
        ("approver_role", S), ("reason_code", S), ("approval_comment", S), ("decided_at", TS),
        ("draft_sha256", S),
    ],
    "ACTIONS": [
        ("action_id", S), ("case_id", S), ("kind", S), ("payload", "VARIANT"), ("status", S),
        ("created_at", TS), ("executed_at", TS), ("result", S), ("attempts", "NUMBER"), ("claimed_at", TS),
        ("claimed_by", S),
    ],
    "CLOCKS": [
        ("clock_id", S), ("case_id", S), ("kind", S), ("subject", S), ("start_ts", TS), ("due", TS),
        ("warn_at", TS), ("business_days", "BOOLEAN"), ("status", S), ("citation_id", S), ("notes", S),
    ],
    "AUDIT_LOG": [
        ("event_id", S), ("case_id", S), ("event_at", TS), ("actor", S), ("actor_role", S), ("action", S),
        ("object_type", S), ("object_id", S), ("details", "VARIANT"), ("seq", "NUMBER"), ("prev_hash", S),
        ("hash", S),
    ],
    "AUDIT_ANCHOR": [("case_id", S), ("event_count", "NUMBER"), ("head_hash", S), ("anchored_at", TS)],
    "LIENS": [
        ("lien_id", S), ("case_id", S), ("account_id", S), ("amount", M), ("placed_at", TS), ("released_at", TS),
        ("source_action_id", S), ("scope", S),
    ],
}
REPLACED_PER_CASE = ("CASES", "TRAIL_EDGES", "ACCOUNT_STATE", "CLASSIFICATIONS", "HOLD_PLAN", "DRAFTS",
                     "VERIFICATIONS", "CLOCKS")
PARTS = {
    "trace": ("TRAIL_EDGES", "ACCOUNT_STATE"),
    "plan": ("CLASSIFICATIONS", "HOLD_PLAN"),
    "drafts": ("DRAFTS", "VERIFICATIONS"),
}
_CAST = {"VARIANT": "", "ARRAY": "::ARRAY", "OBJECT": "::OBJECT"}
MAX_BATCH_CHARS = 8_000_000  # one JSON bind per INSERT stays well under Snowflake's 16 MB value limit


# --------------------------------------------------------------------------- serialisation


def jsonable(value: Any) -> Any:
    """Row value -> JSON: UTC wall-clock timestamp strings, enum values, plain containers."""
    if isinstance(value, datetime):
        return ts_param(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump(mode="json"))
    return value


def insert_sql(table: str, database: str = DATABASE) -> str:
    cols = COLUMNS[table]
    select = ", ".join(f'f.value:"{c}"{_CAST.get(t, "::" + t)}' for c, t in cols)
    names = ", ".join(c for c, _ in cols)
    return (f"INSERT INTO {database}.CASEMGMT.{table} ({names}) SELECT {select} "
            "FROM TABLE(FLATTEN(INPUT => PARSE_JSON(?))) f")


def batches(rows: Sequence[Mapping[str, Any]], max_chars: int = MAX_BATCH_CHARS) -> Iterable[str]:
    """JSON arrays of rows, each below ``max_chars``."""
    batch: list[str] = []
    size = 2
    for r in rows:
        s = json.dumps(jsonable(r), ensure_ascii=False, separators=(",", ":"))
        if batch and size + len(s) + 1 > max_chars:
            yield "[" + ",".join(batch) + "]"
            batch, size = [], 2
        batch.append(s)
        size += len(s) + 1
    if batch:
        yield "[" + ",".join(batch) + "]"


def event_id(case_id: str, e: AuditEvent) -> str:
    """Row id: from the seal (``hash``) when the event is chained, else from its content."""
    if e.hash:
        return "EV-" + e.hash[:24]
    payload = json.dumps(
        [case_id, jsonable(e.at), e.actor, e.role, e.action, e.object_type, e.object_id, jsonable(e.details)],
        sort_keys=True, default=str,
    )
    return "EV-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def stable_json(value: Any) -> Any:
    """JSON-native value that survives a VARIANT round trip unchanged: datetimes -> ISO strings (as the audit
    digest serialises them), integral floats -> integers (``142848.0`` -> ``142848``), containers recursed."""
    native = to_jsonable_python(value, fallback=str)

    def fix(v: Any) -> Any:
        if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
            return int(v)
        if isinstance(v, dict):
            return {str(k): fix(x) for k, x in v.items()}
        if isinstance(v, list):
            return [fix(x) for x in v]
        return v

    return fix(native)


def stored_form(e: AuditEvent) -> AuditEvent:
    """``e`` as AUDIT_LOG stores it (UTC time, JSON-stable details). Matching and sealing use this form, so a
    sealed event verifies after it is read back."""
    return e.model_copy(update={"at": ensure_utc(e.at), "details": stable_json(e.details)}, deep=True)


def event_from_row(r: Mapping[str, Any]) -> AuditEvent:
    """An AUDIT_LOG row (``row_dict``) -> ``AuditEvent`` with its seal."""
    details = r.get("details")
    if isinstance(details, str):
        details = json.loads(details)
    seq = r.get("seq")
    return AuditEvent(at=ensure_utc(r["event_at"]), actor=r["actor"] or "", role=r["actor_role"] or "",
                      action=r["action"] or "", object_type=r["object_type"] or "", object_id=r["object_id"] or "",
                      details=details or {}, seq=int(seq) if seq is not None else None, case_id=r["case_id"],
                      prev_hash=r.get("prev_hash"), hash=r.get("hash"))


def draft_makers(bundle: CaseBundle) -> dict[str, str]:
    """draft_id -> actor of its latest ``draft_created`` audit event (the maker, for maker-checker)."""
    out: dict[str, str] = {}
    for e in bundle.audit:
        if e.action == "draft_created" and e.object_type == "draft":
            out[e.object_id] = e.actor
    return out


# --------------------------------------------------------------------------- bundle -> rows


def case_row(bundle: CaseBundle, *, world: str | None, raw_schema: str | None) -> dict[str, Any]:
    c = bundle.case
    t = bundle.trace.totals if bundle.trace else None
    p = bundle.plan
    return {
        "case_id": c.case_id, "world": world, "raw_schema": raw_schema, "ncrp_ids": list(c.ncrp_ids),
        "opened_at": c.opened_at, "status": c.status, "assigned_to": c.assigned_to,
        "priority_amount": r2(c.priority_amount), "rule": bundle.trace.rule if bundle.trace else None,
        "stolen": t.stolen if t else None, "holdable": t.holdable if t else None,
        "cashed_out": t.cashed_out if t else None, "outbound": t.outbound if t else None,
        "intermediary": t.intermediary if t else None, "merchant_settled": t.merchant_settled if t else None,
        "unlocated": t.unlocated if t else None,
        "total_hold": p.total_hold if p else None, "total_forward_hold": p.total_forward_hold if p else None,
        "total_recoverable": p.total_recoverable if p else None,
        "over_hold_amount": p.over_hold_amount if p else None,
        "baseline_freeze_amount": p.baseline_freeze_amount if p else None,
        "baseline_freeze_accounts": p.baseline_freeze_accounts if p else None,
        "restoration_process": p.restoration_process if p else None, "str_ground": p.str_ground if p else None,
        "n_accounts": len(bundle.trace.accounts) if bundle.trace else 0,
        "n_edges": len(bundle.trace.edges) if bundle.trace else 0,
        "updated_at": utcnow(),
    }


def table_rows(bundle: CaseBundle, table: str, branch: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Flattened rows of one case table (TRAIL_EDGES, ACCOUNT_STATE, CLASSIFICATIONS, HOLD_PLAN, DRAFTS,
    VERIFICATIONS, CLOCKS)."""
    cid = bundle.case.case_id
    branch = branch or {}
    if table == "TRAIL_EDGES":
        return [{"case_id": cid, **e.model_dump()} for e in (bundle.trace.edges if bundle.trace else [])]
    if table == "ACCOUNT_STATE":
        rows = []
        for st in (bundle.trace.accounts.values() if bundle.trace else []):
            d = st.model_dump()
            d.update(case_id=cid, branch_city=branch.get(st.account_id))
            rows.append(d)
        return rows
    if table == "CLASSIFICATIONS":
        return [{"case_id": cid, "account_id": a, "role": c.role, "score": c.score, "reasons": c.reasons,
                 "model": c.model} for a, c in bundle.classifications.items()]
    if table == "HOLD_PLAN":
        if bundle.plan is None:
            return []
        states = bundle.trace.accounts if bundle.trace else {}
        now = utcnow()
        rows = []
        for d in bundle.plan.decisions:
            row = d.model_dump()
            st = states.get(d.account_id)
            row.update(case_id=cid, account_type=st.account_type if st else None,
                       branch_city=branch.get(d.account_id), created_at=now)
            rows.append(row)
        return rows
    if table == "DRAFTS":
        from wapsi.pipeline.run import draft_fingerprint

        makers = draft_makers(bundle)
        return [{**d.model_dump(), "created_by": makers.get(d.draft_id),
                 "draft_sha256": draft_fingerprint(d)} for d in bundle.drafts]
    if table == "VERIFICATIONS":
        return [{"draft_id": k, "case_id": cid, "passed": v.passed,
                 "checks": [c.model_dump(mode="json") for c in v.checks], "verified_at": v.verified_at}
                for k, v in bundle.verifications.items()]
    if table == "CLOCKS":
        return [{"clock_id": c.clock_id, "case_id": cid, "kind": c.kind, "subject": c.subject,
                 "start_ts": c.start, "due": c.due, "warn_at": c.warn_at, "business_days": c.business_days,
                 "status": c.status, "citation_id": c.citation_id, "notes": c.notes} for c in bundle.clocks]
    raise KeyError(table)


def approval_row(a: Approval) -> dict[str, Any]:
    return {"approval_id": a.approval_id, "case_id": a.case_id, "draft_id": a.draft_id, "decision": a.decision,
            "approver": a.approver, "approver_role": a.role, "reason_code": a.reason_code,
            "approval_comment": a.comment, "decided_at": a.at, "draft_sha256": a.draft_sha256}


def action_row(a: Action) -> dict[str, Any]:
    return {"action_id": a.action_id, "case_id": a.case_id, "kind": a.kind, "payload": a.payload,
            "status": a.status, "created_at": a.created_at, "executed_at": a.executed_at, "result": a.result,
            "attempts": 0, "claimed_at": None, "claimed_by": None}


def audit_row(case_id: str, e: AuditEvent) -> dict[str, Any]:
    return {"event_id": event_id(case_id, e), "case_id": case_id, "event_at": e.at, "actor": e.actor,
            "actor_role": e.role, "action": e.action, "object_type": e.object_type, "object_id": e.object_id,
            "details": stable_json(e.details), "seq": e.seq, "prev_hash": e.prev_hash, "hash": e.hash}


def anchor_row(case_id: str, anchor: ChainAnchor) -> dict[str, Any]:
    return {"case_id": case_id, "event_count": anchor.length, "head_hash": anchor.head, "anchored_at": utcnow()}


# --------------------------------------------------------------------------- store


class SnowflakeCaseStore:
    """Implements ``wapsi.ports.CaseStore`` (plus the flattened CASEMGMT tables) on a Snowpark session."""

    def __init__(self, session: Any, database: str = DATABASE) -> None:
        self.session = session
        self.database = database
        self.statements: list[str] = []  # executed SQL (observability + tests)

    # ------------------------------------------------------------------ plumbing
    def t(self, table: str, schema: str = "CASEMGMT") -> str:
        return f"{self.database}.{schema}.{table}"

    def _exec(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        self.statements.append(sql)
        return [row_dict(r) for r in self.session.sql(sql, params=list(params) if params else None).collect()]

    def _insert(self, table: str, rows: Sequence[Mapping[str, Any]]) -> int:
        n = 0
        for payload in batches(rows):
            self._exec(insert_sql(table, self.database), [payload])
            n += 1
        return n

    def _existing_ids(self, table: str, id_col: str, case_id: str) -> set[str]:
        rows = self._exec(f"SELECT {id_col} AS id FROM {self.t(table)} WHERE case_id = ?", [case_id])
        return {r["id"] for r in rows}

    def _append_only(self, table: str, id_col: str, case_id: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        have = self._existing_ids(table, id_col, case_id)
        new = [r for r in rows if r[id_col] not in have]
        self._insert(table, new)
        return len(new)

    # ------------------------------------------------------------------ reads
    def get_bundle(self, case_id: str) -> CaseBundle | None:
        """The stored bundle; its ``audit`` is the case's AUDIT_LOG (the record), in chain order."""
        rows = self._exec(f"SELECT bundle FROM {self.t('CASE_BUNDLES')} WHERE case_id = ?", [case_id])
        if not rows or rows[0]["bundle"] is None:
            return None
        raw = rows[0]["bundle"]
        bundle = CaseBundle.model_validate_json(raw) if isinstance(raw, str) else CaseBundle.model_validate(raw)
        bundle.audit = self.audit_log(case_id)
        return bundle

    # ------------------------------------------------------------------ audit chain
    def audit_log(self, case_id: str) -> list[AuditEvent]:
        """The case's AUDIT_LOG rows as events, in chain order (rows written before sealing first)."""
        rows = self._exec(
            f"SELECT event_at, actor, actor_role, action, object_type, object_id, details, seq, case_id, prev_hash, "
            f"hash FROM {self.t('AUDIT_LOG')} WHERE case_id = ? ORDER BY COALESCE(seq, -1), event_at, event_id",
            [case_id])
        return [event_from_row(r) for r in rows]

    def audit_anchor(self, case_id: str) -> ChainAnchor | None:
        """The case's latest checkpoint (most events) from AUDIT_ANCHOR, or None."""
        rows = self._exec(
            f"SELECT event_count, head_hash FROM {self.t('AUDIT_ANCHOR')} WHERE case_id = ? "
            "ORDER BY event_count DESC, anchored_at DESC LIMIT 1", [case_id])
        if not rows:
            return None
        return ChainAnchor(length=int(rows[0]["event_count"]), head=str(rows[0]["head_hash"]))

    def verify_audit(self, case_id: str) -> ChainReport:
        """Read the case's AUDIT_LOG back and verify its chain against the latest AUDIT_ANCHOR checkpoint."""
        return verify_chain(self.audit_log(case_id), case_id=case_id, anchor=self.audit_anchor(case_id))

    def _extend_audit(self, case_id: str, incoming: Sequence[AuditEvent]) -> tuple[list[AuditEvent], int]:
        """Seal the events of ``incoming`` the stored log does not hold onto the stored chain, INSERT them and a
        new anchor. Returns (the full stored log, events appended)."""
        stored = self.audit_log(case_id)
        full = extend_stored(stored, [stored_form(e) for e in incoming], case_id=case_id)
        new = full[len(stored):]
        if new:
            self._insert("AUDIT_LOG", [audit_row(case_id, e) for e in new])
            self._insert("AUDIT_ANCHOR", [anchor_row(case_id, anchor_of(full))])
        return full, len(new)

    def sync_audit(self, bundle: CaseBundle) -> int:
        """Append only the bundle's new audit events (sealed); ``bundle.audit`` becomes the stored chain."""
        bundle.audit, n = self._extend_audit(bundle.case.case_id, bundle.audit)
        return n

    def case_info(self, case_id: str) -> dict[str, Any] | None:
        rows = self._exec(f"SELECT case_id, world, raw_schema, status FROM {self.t('CASES')} WHERE case_id = ?",
                          [case_id])
        return rows[0] if rows else None

    def case_ids(self) -> list[str]:
        rows = self._exec(f"SELECT case_id FROM {self.t('CASE_BUNDLES')} ORDER BY case_id")
        return [r["case_id"] for r in rows]

    def list_cases(self) -> list[Case]:
        rows = self._exec(
            f"SELECT case_id, ncrp_ids, opened_at, status, assigned_to, priority_amount FROM {self.t('CASES')} "
            "ORDER BY opened_at DESC, case_id"
        )
        out = []
        for r in rows:
            ids = r["ncrp_ids"]
            out.append(Case(case_id=r["case_id"], ncrp_ids=json.loads(ids) if isinstance(ids, str) else list(ids),
                            opened_at=r["opened_at"], status=CaseStatus(r["status"]), assigned_to=r["assigned_to"],
                            priority_amount=float(r["priority_amount"] or 0.0)))
        return out

    def case_for_draft(self, draft_id: str) -> str | None:
        rows = self._exec(f"SELECT case_id FROM {self.t('DRAFTS')} WHERE draft_id = ? LIMIT 1", [draft_id])
        return rows[0]["case_id"] if rows else None

    def branch_map(self, case_id: str) -> dict[str, str]:
        rows = self._exec(f"SELECT account_id, branch_city FROM {self.t('ACCOUNT_STATE')} WHERE case_id = ?",
                          [case_id])
        return {r["account_id"]: r["branch_city"] for r in rows if r["branch_city"]}

    # ------------------------------------------------------------------ writes
    def put_bundle(self, bundle: CaseBundle) -> None:
        """CaseStore protocol: write every part (keeps the case's world / schema / branch cities)."""
        self.save(bundle)

    def save(
        self,
        bundle: CaseBundle,
        *,
        world: str | None = None,
        raw_schema: str | None = None,
        branch: Mapping[str, str] | None = None,
        parts: Iterable[str] = ("trace", "plan", "drafts"),
    ) -> dict[str, int]:
        """Replace the case's rows for ``parts`` (+ CASES, CASE_BUNDLES, CLOCKS) and append new approvals,
        actions and audit events (sealed onto the stored chain; ``bundle.audit`` becomes that chain, the
        caller's receipt). Returns rows written per table."""
        cid = bundle.case.case_id
        info = self.case_info(cid) or {}
        world = world or info.get("world")
        raw_schema = raw_schema or info.get("raw_schema")
        tables = [t for p in parts for t in PARTS[p]]
        if branch is None and any(t in ("ACCOUNT_STATE", "HOLD_PLAN") for t in tables):
            branch = self.branch_map(cid)
        version_rows = self._exec(f"SELECT version FROM {self.t('CASE_BUNDLES')} WHERE case_id = ?", [cid])
        version = int(version_rows[0]["version"] or 0) + 1 if version_rows else 1

        written: dict[str, int] = {}
        # the audit chain first: the bundle copy below then carries the sealed log exactly as stored
        bundle.audit, written["AUDIT_LOG"] = self._extend_audit(cid, bundle.audit)
        self._exec(f"DELETE FROM {self.t('CASE_BUNDLES')} WHERE case_id = ?", [cid])
        self._insert("CASE_BUNDLES", [{"case_id": cid, "bundle": json.loads(bundle.model_dump_json()),
                                       "version": version, "updated_at": utcnow()}])
        written["CASE_BUNDLES"] = 1
        self._exec(f"DELETE FROM {self.t('CASES')} WHERE case_id = ?", [cid])
        self._insert("CASES", [case_row(bundle, world=world, raw_schema=raw_schema)])
        written["CASES"] = 1
        for table in [*tables, "CLOCKS"]:
            rows = table_rows(bundle, table, branch)
            self._exec(f"DELETE FROM {self.t(table)} WHERE case_id = ?", [cid])
            self._insert(table, rows)
            written[table] = len(rows)
        written["APPROVALS"] = self._append_only("APPROVALS", "approval_id", cid,
                                                 [approval_row(a) for a in bundle.approvals])
        written["ACTIONS"] = self._append_only("ACTIONS", "action_id", cid, [action_row(a) for a in bundle.actions])
        return written

    def append_audit(self, case_id: str, event: AuditEvent) -> None:
        """Seal ``event`` as the next link of the case's stored chain and INSERT it (plus a new anchor). The
        caller's event is stamped (``seq``, ``case_id``, ``prev_hash``, ``hash``): its receipt."""
        log = self.audit_log(case_id)
        event.at, event.details = ensure_utc(event.at), stable_json(event.details)
        append_event(log, event, case_id=case_id)
        self._insert("AUDIT_LOG", [audit_row(case_id, event)])
        self._insert("AUDIT_ANCHOR", [anchor_row(case_id, anchor_of(log))])

    def record_approval(self, bundle: CaseBundle, liens: Sequence[Mapping[str, Any]] = ()) -> dict[str, int]:
        """Narrow write path for APPROVE (caller's rights): append approvals, actions, liens and (sealed) audit
        events; update the bundle and the case status; rebuild the case's clocks. No other table is touched."""
        cid = bundle.case.case_id
        written = {
            "APPROVALS": self._append_only("APPROVALS", "approval_id", cid,
                                           [approval_row(a) for a in bundle.approvals]),
            "ACTIONS": self._append_only("ACTIONS", "action_id", cid, [action_row(a) for a in bundle.actions]),
            "LIENS": self._append_only("LIENS", "lien_id", cid, [{"scope": "AMOUNT", **r} for r in liens]),
            "AUDIT_LOG": self.sync_audit(bundle),
        }
        now = ts_param(utcnow())
        self._exec(f"UPDATE {self.t('CASE_BUNDLES')} SET bundle = PARSE_JSON(?), version = version + 1, "
                   "updated_at = TO_TIMESTAMP_NTZ(?) WHERE case_id = ?", [bundle.model_dump_json(), now, cid])
        self._exec(f"UPDATE {self.t('CASES')} SET status = ?, updated_at = TO_TIMESTAMP_NTZ(?) WHERE case_id = ?",
                   [bundle.case.status.value, now, cid])
        self._exec(f"DELETE FROM {self.t('CLOCKS')} WHERE case_id = ?", [cid])
        written["CLOCKS"] = len(clock_rows := table_rows(bundle, "CLOCKS"))
        self._insert("CLOCKS", clock_rows)
        return written
