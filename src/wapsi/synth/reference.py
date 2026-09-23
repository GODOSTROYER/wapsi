"""Independent ground-truth reference: exact pro-rata taint and the AGENTS.md disposition rules.

This module deliberately shares no code with ``wapsi.graph`` / ``wapsi.classify`` (it only reads the world
frames), so golden answers are an independent check of the product.

Trace semantics (default ``TraceConfig``):
- seeds = the complaint transactions; window = [first seed ts, first seed ts + 72 h]; victims are layer 0.
- Events are replayed in (ts, txn_id) order. When a tracked account sends money, the transfer carries
  ``taint_vector * amount / balance_before`` (pro-rata, SOP 5(xiii)); clean credits dilute the ratio.
- The first tainted receipt starts tracking an account with its ledger balance just before that transaction.
- Merchants and nodal/pool/escrow accounts are terminals (keep everything they received; their later
  outflows are not followed); ATM and crypto are exits. Other-bank accounts are followed (consortium) when
  ``follow_consortium``; accounts reached at layer >= ``max_depth`` are not expanded further.
- Layer = minimum depth at which taint arrived; edge depth = receiver layer (exits: sender layer + 1).

Dispositions follow AGENTS.md item 6 (see ``_decide``); home nodal/pool/escrow accounts get an amount-specific
``HOLD_AMOUNT_INTERMEDIARY`` (SOP 9.1(iii)), never a forward hold or an escalation flag. Money is rounded per
victim with ``r2``; totals are sums of rounded components and ``unlocated`` is the residual.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from wapsi.config import TraceConfig
from wapsi.models import Disposition, RestorationProcess, Role, StrGround
from wapsi.money import r2

INTERMEDIARY = {"NODAL", "POOL", "ESCROW"}
EXIT_CHANNELS = {"ATM", "CRYPTO"}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def ts_us(series: pd.Series) -> np.ndarray:
    s = pd.to_datetime(series, utc=True)
    return s.dt.as_unit("us").astype("int64").to_numpy()


def dt_us(ts: datetime) -> int:
    """Epoch microseconds (naive datetimes are taken as UTC)."""
    t = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
    d = t - _EPOCH
    return (d.days * 86_400 + d.seconds) * 1_000_000 + d.microseconds


def us_to_dt(v: int) -> datetime:
    return _EPOCH + timedelta(microseconds=int(v))


def _clean(v: float) -> float:
    x = r2(v)
    return 0.0 if abs(x) < 0.005 else x


def _rvec(vec: dict[str, float]) -> dict[str, float]:
    out = {k: _clean(v) for k, v in sorted(vec.items())}
    return {k: v for k, v in out.items() if v != 0.0}


def _sum(vec: dict[str, float]) -> float:
    return _clean(sum(vec.values()))


@dataclass
class RefAccount:
    account_id: str
    bank_id: str
    account_type: str
    is_home: bool
    layer: int
    first_tainted_us: int | None = None
    balance_now: float = 0.0
    tainted_now: dict[str, float] = field(default_factory=dict)
    received: dict[str, float] = field(default_factory=dict)
    sent: dict[str, float] = field(default_factory=dict)
    exits: dict[str, float] = field(default_factory=dict)
    delivered: dict[str, float] = field(default_factory=dict)
    undelivered: dict[str, float] = field(default_factory=dict)
    terminal: str | None = None
    parents: list[str] = field(default_factory=list)

    @property
    def tainted_now_total(self) -> float:
        return _sum(self.tainted_now)

    @property
    def received_total(self) -> float:
        return _sum(self.received)


@dataclass
class RefEdge:
    txn_id: str
    ts_us: int
    from_account: str | None
    to_account: str | None
    channel: str
    amount: float
    depth: int
    tainted: dict[str, float]

    @property
    def tainted_total(self) -> float:
        return _sum(self.tainted)


@dataclass
class RefDecision:
    account_id: str
    bank_id: str
    disposition: Disposition
    hold_amount: float
    forward_hold_amount: float
    tainted_amount: float
    balance_now: float
    layer: int
    role: Role
    per_victim: dict[str, float]
    forward_per_victim: dict[str, float]
    reason_codes: list[str]
    requires_human: bool


@dataclass
class RefCase:
    case_id: str
    ncrp_ids: list[str]
    window_start_us: int
    window_end_us: int
    victim_seed: dict[str, float]
    edges: list[RefEdge]
    accounts: dict[str, RefAccount]
    totals: dict[str, float]
    decisions: list[RefDecision]
    total_hold: float
    total_forward_hold: float
    total_recoverable: float
    per_victim_recoverable: dict[str, float]
    baseline_freeze_amount: float
    baseline_freeze_accounts: int
    over_hold_amount: float
    process: RestorationProcess
    str_ground: StrGround | None
    escalation_candidates: list[str]
    warnings: list[str]

    def decision(self, account_id: str) -> RefDecision:
        return next(d for d in self.decisions if d.account_id == account_id)


class _State:
    __slots__ = (
        "code",
        "layer",
        "bal",
        "vec",
        "received",
        "sent",
        "exits",
        "propagates",
        "kind",
        "first_us",
        "parents",
        "inbound",
    )

    def __init__(self, code: int, layer: int, bal: float, kind: str | None, propagates: bool, first_us: int):
        self.code = code
        self.layer = layer
        self.bal = bal
        self.vec: dict[str, float] = {}
        self.received: dict[str, float] = {}
        self.sent: dict[str, float] = {}
        self.exits: dict[str, float] = {}
        self.propagates = propagates
        self.kind = kind
        self.first_us = first_us
        self.parents: list[int] = []
        self.inbound: list[int] = []


def _add(dst: dict[str, float], src: dict[str, float], sign: float = 1.0) -> None:
    for k, v in src.items():
        dst[k] = dst.get(k, 0.0) + sign * v


class ReferenceWorld:
    """Ground-truth engine over world frames (the dict produced by ``generate_world`` / ``load_frames``)."""

    def __init__(
        self,
        frames: dict[str, pd.DataFrame],
        roles: dict[str, str] | None = None,
        cfg: TraceConfig | None = None,
        escalation_min_prior_reports: int = 2,
        as_of: datetime | None = None,
    ) -> None:
        self.cfg = cfg or TraceConfig()
        self.min_prior = escalation_min_prior_reports
        banks = frames["banks"]
        self.home = str(banks.loc[banks["is_home"].astype(bool), "bank_id"].iloc[0])
        acc = frames["accounts"]
        ids = acc["account_id"].astype(str).tolist()
        tx = frames["transactions"].sort_values(["ts", "txn_id"], kind="mergesort").reset_index(drop=True)
        index = pd.Index(ids)
        frm = index.get_indexer(tx["from_account"])
        to = index.get_indexer(tx["to_account"])
        unknown = sorted(
            set(tx.loc[(frm < 0) & tx["from_account"].notna(), "from_account"])
            | set(tx.loc[(to < 0) & tx["to_account"].notna(), "to_account"])
        )
        n_known = len(ids)
        if unknown:
            ids = ids + unknown
            index = pd.Index(ids)
            frm = index.get_indexer(tx["from_account"])
            to = index.get_indexer(tx["to_account"])
        self.ids = ids
        self.code = {a: i for i, a in enumerate(ids)}
        self.bank = acc["bank_id"].astype(str).tolist() + [u.split("-")[0] for u in unknown]
        self.atype = acc["account_type"].astype(str).tolist() + ["UNKNOWN"] * len(unknown)
        self.opening = np.concatenate([acc["opening_balance"].to_numpy(float), np.zeros(len(unknown))])
        self.n_known = n_known
        self.ts = ts_us(tx["ts"])
        self.frm = frm.astype(np.int64)
        self.to = to.astype(np.int64)
        self.amt = tx["amount"].to_numpy(float)
        self.chan = tx["channel"].astype(str).to_numpy()
        self.txn_id = tx["txn_id"].astype(str).to_numpy()
        # per-account rows (CSR)
        rows = np.arange(len(tx), dtype=np.int64)
        m_from = self.frm >= 0
        m_to = (self.to >= 0) & (self.to != self.frm)
        e_acct = np.concatenate([self.frm[m_from], self.to[m_to]])
        e_row = np.concatenate([rows[m_from], rows[m_to]])
        order = np.lexsort((e_row, e_acct))
        self._rows = e_row[order]
        self._starts = np.searchsorted(e_acct[order], np.arange(len(ids) + 1))
        self._cum: dict[int, np.ndarray] = {}
        # side tables
        inv = frames.get("invoices")
        self.inv_status: dict[str, list[str]] = {}
        if inv is not None and len(inv):
            for t, s in zip(inv["txn_id"].tolist(), inv["order_status"].astype(str).tolist(), strict=True):
                if isinstance(t, str):
                    self.inv_status.setdefault(t, []).append(s)
        sig = frames.get("external_signals")
        self.prior = (
            {}
            if sig is None
            else dict(zip(sig["account_id"], sig["ncrp_prior_reports"].astype(int), strict=True))
        )
        comp = frames["complaints"]
        self.fir = {
            n: (isinstance(f, str) and bool(f.strip()))
            for n, f in zip(comp["ncrp_id"], comp["fir_number"], strict=True)
        }
        ct = frames["complaint_txns"]
        wanted = set(ct["txn_id"])
        pos = np.flatnonzero(np.isin(self.txn_id, list(wanted)))
        row_of = {self.txn_id[p]: int(p) for p in pos}
        self.seed_rows: dict[str, list[int]] = {}
        for n, t in zip(ct["ncrp_id"], ct["txn_id"], strict=True):
            if t in row_of:
                self.seed_rows.setdefault(n, []).append(row_of[t])
        if roles is None and "labels_accounts" in frames:
            la = frames["labels_accounts"]
            roles = dict(zip(la["account_id"], la["role_truth"].astype(str), strict=True))
        self.roles = roles or {}
        if as_of is None:
            meta = frames.get("meta")
            end = None
            if meta is not None:
                kv = dict(zip(meta["key"], meta["value"], strict=True))
                end = kv.get("world_end")
            as_of = datetime.fromisoformat(end) if end else us_to_dt(int(self.ts.max()) + 1)
        self.as_of_us = dt_us(as_of)

    # ------------------------------------------------------------------ balances
    def _slice(self, code: int) -> np.ndarray:
        return self._rows[self._starts[code] : self._starts[code + 1]]

    def _cumulative(self, code: int) -> np.ndarray:
        c = self._cum.get(code)
        if c is None:
            rows = self._slice(code)
            signed = np.where(self.to[rows] == code, self.amt[rows], 0.0) - np.where(
                self.frm[rows] == code, self.amt[rows], 0.0
            )
            c = np.cumsum(np.concatenate([[self.opening[code]], signed]))
            self._cum[code] = c
        return c

    def balance_before_row(self, code: int, row: int) -> float:
        pos = int(np.searchsorted(self._slice(code), row, "left"))
        return float(self._cumulative(code)[pos])

    def balance_at_us(self, code: int, t_us: int) -> float:
        rows = self._slice(code)
        pos = int(np.searchsorted(self.ts[rows], t_us, "left"))
        return round(float(self._cumulative(code)[pos]), 2)

    def balance_at(self, account_id: str, ts: datetime) -> float:
        return self.balance_at_us(self.code[account_id], dt_us(ts))

    # ------------------------------------------------------------------ trace
    def _kind(self, code: int) -> str | None:
        t = self.atype[code]
        if t in INTERMEDIARY:
            return "NODAL_POOL_ESCROW"
        if t == "MERCHANT":
            return "MERCHANT"
        if self.bank[code] != self.home and not self.cfg.follow_consortium:
            return "OTHER_BANK"
        return None

    def trace(self, ncrp_ids: list[str], case_id: str | None = None) -> RefCase:
        cfg = self.cfg
        seed_of: dict[int, str] = {}
        for n in ncrp_ids:
            for r in self.seed_rows.get(n, []):
                if r in seed_of:
                    raise ValueError(f"transaction {self.txn_id[r]} is claimed by two complaints")
                seed_of[r] = n
        if not seed_of:
            raise ValueError(f"no complaint transactions for {ncrp_ids}")
        start = int(min(self.ts[r] for r in seed_of))
        end = start + int(cfg.window_hours * 3600 * 1_000_000)
        heap = sorted(seed_of)
        queued = set(heap)
        state: dict[int, _State] = {}
        victims: dict[int, None] = {}
        edges: dict[int, tuple[dict[str, float], int]] = {}
        warnings: list[str] = []
        seed_amount: dict[str, float] = {n: 0.0 for n in ncrp_ids}

        while heap:
            r = heapq.heappop(heap)
            if self.ts[r] > end:
                continue
            f, t, amt = int(self.frm[r]), int(self.to[r]), float(self.amt[r])
            carry: dict[str, float] = {}
            src_layer = 0
            sst = state.get(f) if f >= 0 else None
            if r in seed_of:
                n = seed_of[r]
                carry = {n: amt}
                seed_amount[n] += amt
                if f >= 0:
                    victims[f] = None
                if sst is not None and sst.propagates:
                    sst.bal -= amt
            elif sst is not None:
                src_layer = sst.layer
                if sst.propagates:
                    total = sum(sst.vec.values())
                    if sst.bal > 0 and total > 0:
                        ratio = min(1.0, amt / sst.bal)
                        carry = {k: v * ratio for k, v in sst.vec.items() if v > 0}
                        _add(sst.vec, carry, -1.0)
                    sst.bal -= amt
            ctotal = sum(carry.values())
            if t >= 0:
                tst = state.get(t)
                if tst is not None:
                    if tst.propagates:
                        tst.bal += amt
                    if ctotal > 0:
                        _add(tst.vec, carry)
                        _add(tst.received, carry)
                        tst.layer = min(tst.layer, src_layer + 1)
                        tst.inbound.append(r)
                        if f >= 0 and f not in tst.parents:
                            tst.parents.append(f)
                elif ctotal > 0 and t not in victims:
                    layer = src_layer + 1
                    kind = self._kind(t)
                    prop = kind is None and layer < cfg.max_depth and self.atype[t] != "UNKNOWN"
                    if kind is None and not prop:
                        warnings.append(f"not expanded (depth {layer}): {self.ids[t]}")
                    tst = _State(t, layer, self.balance_before_row(t, r) + amt, kind, prop, int(self.ts[r]))
                    _add(tst.vec, carry)
                    _add(tst.received, carry)
                    tst.inbound.append(r)
                    if f >= 0:
                        tst.parents.append(f)
                    state[t] = tst
                    if prop:
                        rows = self._slice(t)
                        lo = int(np.searchsorted(rows, r, "right"))
                        hi = int(np.searchsorted(self.ts[rows], end, "right"))
                        for rr in rows[lo:hi].tolist():
                            if rr not in queued:
                                queued.add(rr)
                                heapq.heappush(heap, rr)
            elif ctotal > 0 and sst is not None:
                _add(sst.exits, carry)
            if ctotal > 0:
                edges[r] = (carry, src_layer)
                if sst is not None and r not in seed_of:
                    _add(sst.sent, carry)
                if 0 < ctotal < cfg.min_edge_amount:
                    warnings.append(f"edge below min_edge_amount: {self.txn_id[r]} carries {ctotal:.2f}")

        return self._finish(ncrp_ids, case_id, start, end, seed_amount, state, victims, edges, warnings)

    # ------------------------------------------------------------------ results
    def _finish(self, ncrp_ids, case_id, start, end, seed_amount, state, victims, edges, warnings) -> RefCase:
        accounts: dict[str, RefAccount] = {}
        for v in victims:
            accounts[self.ids[v]] = RefAccount(
                account_id=self.ids[v],
                bank_id=self.bank[v],
                account_type=self.atype[v],
                is_home=self.bank[v] == self.home,
                layer=0,
                balance_now=self.balance_at_us(v, self.as_of_us),
            )
        for code, st in state.items():
            now = st.vec if st.propagates else st.received
            ra = RefAccount(
                account_id=self.ids[code],
                bank_id=self.bank[code],
                account_type=self.atype[code],
                is_home=self.bank[code] == self.home,
                layer=st.layer,
                first_tainted_us=st.first_us,
                balance_now=self.balance_at_us(code, self.as_of_us),
                tainted_now=_rvec(now),
                received=_rvec(st.received),
                sent=_rvec(st.sent),
                exits=_rvec(st.exits),
                terminal=st.kind,
                parents=sorted(self.ids[p] for p in st.parents),
            )
            if st.kind == "MERCHANT":
                dl: dict[str, float] = {}
                ud: dict[str, float] = {}
                for r in st.inbound:
                    statuses = self.inv_status.get(self.txn_id[r], [])
                    _add(dl if "DELIVERED" in statuses else ud, edges[r][0])
                ra.delivered, ra.undelivered = _rvec(dl), _rvec(ud)
            accounts[ra.account_id] = ra

        ref_edges = []
        for r in sorted(edges):
            carry, src_layer = edges[r]
            t = int(self.to[r])
            f = int(self.frm[r])
            if t >= 0:
                depth = state[t].layer if t in state else src_layer + 1
            else:
                depth = (state[f].layer if f in state else src_layer) + 1
            ref_edges.append(
                RefEdge(
                    txn_id=str(self.txn_id[r]),
                    ts_us=int(self.ts[r]),
                    from_account=self.ids[f] if f >= 0 else None,
                    to_account=self.ids[t] if t >= 0 else None,
                    channel=str(self.chan[r]),
                    amount=float(self.amt[r]),
                    depth=int(depth),
                    tainted=_rvec(carry),
                )
            )

        stolen = _clean(sum(seed_amount.values()))
        parts = dict(holdable=0.0, cashed_out=0.0, outbound=0.0, intermediary=0.0, merchant_settled=0.0)
        for e in ref_edges:
            if e.to_account is None and e.channel in EXIT_CHANNELS:
                parts["cashed_out"] += e.tainted_total
        for a in accounts.values():
            if a.layer == 0:
                continue
            tn = a.tainted_now_total
            if a.account_type in INTERMEDIARY:
                parts["intermediary"] += tn
            elif not a.is_home:
                parts["outbound"] += tn
            elif a.account_type == "MERCHANT":
                parts["merchant_settled"] += _sum(a.delivered)
                parts["holdable"] += _sum(a.undelivered)
            else:
                parts["holdable"] += tn
        totals = {"stolen": stolen, **{k: _clean(v) for k, v in parts.items()}}
        totals["unlocated"] = _clean(stolen - sum(totals[k] for k in parts))

        decisions = self._decide(ncrp_ids, accounts)
        total_hold = _clean(sum(d.hold_amount for d in decisions))
        total_fwd = _clean(sum(d.forward_hold_amount for d in decisions))
        outbound = [d for d in decisions if d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST]
        total_rec = _clean(total_hold + sum(d.tainted_amount for d in outbound))
        pv = {n: 0.0 for n in sorted(ncrp_ids)}
        for d in decisions:
            if d.hold_amount > 0 or d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST:
                _add(pv, d.per_victim)
        pv = {k: _clean(v) for k, v in pv.items()}
        process = RestorationProcess.P2 if sum(1 for v in pv.values() if v > 0) > 1 else RestorationProcess.P1
        home_touched = [a for a in accounts.values() if a.layer >= 1 and a.is_home]
        mule_home = any(self.roles.get(a.account_id) == Role.MULE.value for a in home_touched)
        return RefCase(
            case_id=case_id or f"CASE-{ncrp_ids[0]}",
            ncrp_ids=list(ncrp_ids),
            window_start_us=start,
            window_end_us=end,
            victim_seed={n: _clean(v) for n, v in seed_amount.items()},
            edges=ref_edges,
            accounts=accounts,
            totals=totals,
            decisions=decisions,
            total_hold=total_hold,
            total_forward_hold=total_fwd,
            total_recoverable=total_rec,
            per_victim_recoverable=pv,
            baseline_freeze_amount=_clean(sum(a.balance_now for a in home_touched)),
            baseline_freeze_accounts=len(home_touched),
            over_hold_amount=_clean(
                sum(
                    max(0.0, d.hold_amount - d.tainted_amount)
                    for d in decisions
                    if d.disposition != Disposition.OUTBOUND_CFCFRMS_REQUEST
                )
            ),
            process=process,
            str_ground=StrGround.PROCEEDS_OF_CRIME if mule_home else None,
            escalation_candidates=sorted(d.account_id for d in decisions if d.requires_human),
            warnings=warnings,
        )

    def _decide(self, ncrp_ids: list[str], accounts: dict[str, RefAccount]) -> list[RefDecision]:
        """AGENTS.md item 6. Returns decisions for every account with layer >= 1, ordered by (layer, id)."""
        out: list[RefDecision] = []
        firs = {n for n in ncrp_ids if self.fir.get(n)}
        for a in sorted(
            (a for a in accounts.values() if a.layer >= 1), key=lambda a: (a.layer, a.account_id)
        ):
            role = Role(self.roles.get(a.account_id, Role.UNKNOWN.value))
            tn = a.tainted_now_total
            bal = a.balance_now
            hold = fwd = 0.0
            per_victim: dict[str, float] = {}
            fwd_pv: dict[str, float] = {}
            tainted_amount = tn
            codes: list[str] = []
            human = False
            if a.account_type in INTERMEDIARY and a.is_home:
                # SOP 9.1(iii): hold the disputed amount (never suspend the account); SOP 9.1(i): no forward
                # hold on nodal/pool/escrow; never an s.106 escalation flag.
                hold = _clean(min(tn, max(bal, 0.0)))
                disp = Disposition.HOLD_AMOUNT_INTERMEDIARY if hold > 0 else Disposition.NO_ACTION
                if hold > 0:
                    per_victim = _scale(a.tainted_now, hold / tn)
            elif (
                not a.is_home
            ):  # other-bank accounts (incl. other-bank intermediaries): ask that bank to hold
                if tn > 0:
                    disp = Disposition.OUTBOUND_CFCFRMS_REQUEST
                    per_victim = dict(a.tainted_now)
                else:
                    disp = Disposition.CASHED_OUT if a.exits else Disposition.NO_ACTION
            elif a.account_type == "MERCHANT":  # SOP 9.2
                und = _sum(a.undelivered)
                if und > 0:
                    disp = Disposition.HOLD_IN_POOL
                    hold = _clean(min(und, max(bal, 0.0)))
                    per_victim = _scale(a.undelivered, hold / und)
                else:
                    disp = Disposition.NO_HOLD_UPDATE_ONLY
            else:
                hold = _clean(min(tn, max(bal, 0.0)))  # SOP 5(ix), 9.1: tainted amount only
                if tn > 0 and hold > 0:
                    per_victim = _scale(a.tainted_now, hold / tn)
                if a.layer == 1 and r2(bal) < a.received_total and a.exits:  # SOP 5(x) forward hold
                    fwd = _sum(a.exits)
                    fwd_pv = dict(a.exits)
                if role == Role.INNOCENT_HOLDER:  # SOP 5(xii)
                    disp = (
                        Disposition.HOLD_AMOUNT_NO_ESCALATION
                        if hold > 0
                        else Disposition.FORWARD_HOLD
                        if fwd > 0
                        else Disposition.NO_ACTION
                    )
                else:
                    disp = (
                        Disposition.HOLD_AMOUNT
                        if hold > 0
                        else Disposition.FORWARD_HOLD
                        if fwd > 0
                        else Disposition.CASHED_OUT
                        if a.exits
                        else Disposition.NO_ACTION
                    )
                if fwd > 0:
                    codes.append("FORWARD_HOLD_FIRST_LAYER")
                if (
                    role == Role.MULE
                    and disp in (Disposition.HOLD_AMOUNT, Disposition.FORWARD_HOLD)
                    and self.prior.get(a.account_id, 0) >= self.min_prior
                    and any(n in firs for n, v in a.received.items() if v > 0)
                ):
                    codes.append("ESCALATE_S106_CANDIDATE")
                    human = True
            out.append(
                RefDecision(
                    account_id=a.account_id,
                    bank_id=a.bank_id,
                    disposition=disp,
                    hold_amount=hold,
                    forward_hold_amount=fwd,
                    tainted_amount=tainted_amount,
                    balance_now=bal,
                    layer=a.layer,
                    role=role,
                    per_victim=per_victim,
                    forward_per_victim=fwd_pv,
                    reason_codes=[disp.value, *codes],
                    requires_human=human,
                )
            )
        return out


def _scale(vec: dict[str, float], f: float) -> dict[str, float]:
    if abs(f - 1.0) < 1e-12:
        return dict(vec)
    return _rvec({k: v * f for k, v in vec.items()})
