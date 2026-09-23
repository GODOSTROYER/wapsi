"""Exact event simulation of tainted money over the candidate accounts of a case.

Every transaction touching a candidate account inside ``[window_start, window_end]`` is replayed in time order,
starting from ``Ledger.balances_at(candidates, window_start)``. Transactions in the same second are replayed so
that a credit into an account comes before the debits it funds (``wapsi.graph.common.event_order``). Victim
transfers (seeds) carry taint keyed by their ``ncrp_id``; every other credit is legitimate money.

Apportionment rules (``TraceConfig.rule``):

- ``PRO_RATA`` (SOP Para 5(xiii)): a debit ``a`` from balance ``B`` with taint vector ``tau`` carries
  ``a * tau[v] / B`` for each victim ``v``.
- ``FIFO`` / ``LIFO``: each account keeps a queue of lots (opening balance = one legitimate lot at the front);
  a lot carries its own taint vector and is consumed pro-rata when only part of it leaves.

Terminal accounts are never expanded:

- nodal / pool / escrow and merchant accounts keep the disputed amount while their balance covers it (lowest
  intermediate balance; SOP 9.1(iii) / 9.2 hold the disputed amount). A debit that takes the balance below the
  tainted amount carries the shortfall (split pro-rata across victims) off the trail: to an exit
  (``cashed_out``), to another bank that is not followed (``outbound``), otherwise ``unlocated`` with a warning.
  It never taints the receiving account, even a traced one. With ``follow_consortium`` off, other-bank
  merchants / intermediaries are only partly visible and absorb their taint like other-bank accounts.
- other-bank accounts that are not followed absorb tainted money (their debits carry no taint).

Money that leaves for an account outside the candidate set counts as ``unlocated`` (with a warning) unless it goes
to another bank that is not followed (``outbound``).

Victim accounts. A victim account of the case that receives money of *another* complaint of the case is
simulated like any other account (layer >= 1). Its reported transfers first carry the other complaints' taint
under the configured rule; only the rest is the reporting victim's own money and is tagged with that complaint.
``victim_seed`` holds these own-money amounts, so ``stolen`` never counts a rupee twice. Money of a complaint that
flows back to that complaint's own victim account is returned money: ``unlocated``, never taint.

Overdrafts are handled defensively: a debit larger than the balance carries all the taint (and warns); a credit
into a negative balance first repays the deficit, and the taint in that part is reported as unlocated.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from wapsi.config import TraceConfig
from wapsi.graph.common import (
    PASS_THROUGH_TERMINALS,
    TINY,
    by_timestamp,
    delivery_fractions,
    event_order,
    merchant_inflows,
    merchant_undelivered,
    round_vec,
    vec_total,
)
from wapsi.graph.expand import DROP_DEPTH, DROP_LIMIT, DROP_UNKNOWN, Expansion
from wapsi.models import (
    INTERMEDIARY_ACCOUNT_TYPES,
    Account,
    AccountState,
    AccountType,
    ApportionmentRule,
    TerminalKind,
    TraceTotals,
    TrailEdge,
    Transaction,
)
from wapsi.money import eq, fmt_inr, r2
from wapsi.ports import Ledger
from wapsi.timeutil import ensure_utc

OVERDRAFT_EPS = 0.005  # rupees

# destination kinds of a tainted debit
DEST_ACCOUNT = "account"
DEST_EXIT = "exit"
DEST_OUTBOUND = "outbound"
DEST_UNLOCATED = "unlocated"

_REASON_VICTIM = "victim"
_REASON_UNTRACED = "untraced"
_REASON_TERMINAL = "terminal"

# how a book passes taint on
MODE_RULE = "rule"  # the configured apportionment rule
MODE_LIBR = "libr"  # merchant / intermediary: lowest intermediate balance; payouts leave the trail
MODE_ABSORB = "absorb"  # other bank, not followed: debits carry no taint


class _Lot:
    __slots__ = ("amount", "vec")

    def __init__(self, amount: float, vec: dict[str, float]) -> None:
        self.amount = amount
        self.vec = vec


class _Book:
    """Running state of one candidate account."""

    __slots__ = (
        "account",
        "balance",
        "terminal",
        "mode",
        "own",
        "taint",
        "lots",
        "received_total",
        "sent_total",
        "layer",
        "first_tainted_at",
        "parents",
        "overdrafts",
    )

    def __init__(
        self,
        account: Account,
        balance: float,
        terminal: TerminalKind | None,
        mode: str,
        use_lots: bool,
        own: frozenset[str] = frozenset(),
    ) -> None:
        self.account = account
        self.balance = balance
        self.terminal = terminal
        self.mode = mode
        self.own = own  # complaints this account is the victim of: their money coming back is not taint
        self.taint: dict[str, float] = {}  # pro-rata, lowest-intermediate-balance and absorbing accounts
        self.lots: deque[_Lot] | None = None
        if use_lots and mode == MODE_RULE:
            self.lots = deque()
            if balance > TINY:
                self.lots.append(_Lot(balance, {}))
        self.received_total = 0.0
        self.sent_total = 0.0
        self.layer: int | None = None
        self.first_tainted_at: datetime | None = None
        self.parents: set[str] = set()
        self.overdrafts = 0

    def taint_now(self) -> dict[str, float]:
        if self.lots is None:
            return dict(self.taint)
        out: dict[str, float] = defaultdict(float)
        for lot in self.lots:
            for v, x in lot.vec.items():
                out[v] += x
        return dict(out)

    # -- debit
    def take(self, amount: float, lifo: bool) -> dict[str, float]:
        if self.mode == MODE_ABSORB:
            self.balance -= amount
            return {}
        if self.mode == MODE_LIBR:
            return self._take_libr(amount)
        if amount > max(self.balance, 0.0) + OVERDRAFT_EPS:
            self.overdrafts += 1
        moved = self._take_lots(amount, lifo) if self.lots is not None else self._take_pro_rata(amount)
        self.balance -= amount
        return moved

    def _take_libr(self, amount: float) -> dict[str, float]:
        """Lowest intermediate balance: tainted money stays while the balance covers it."""
        self.balance -= amount
        taint = self.taint
        if not taint:
            return {}
        total = sum(taint.values())
        keep = max(self.balance, 0.0)
        if total <= keep + TINY:
            return {}
        if keep <= TINY:
            self.taint = {}
            return taint
        f = keep / total
        moved = {}
        for v, x in list(taint.items()):
            rest = x * f
            moved[v] = x - rest
            if rest > TINY:
                taint[v] = rest
            else:
                del taint[v]
        return moved

    def _take_pro_rata(self, amount: float) -> dict[str, float]:
        taint = self.taint
        if not taint:
            return {}
        total = sum(taint.values())
        bal = self.balance
        if bal <= TINY:  # taint without money: inconsistent data, move what the debit can carry
            frac = min(amount, total) / total
        else:
            frac = min(amount / bal, 1.0)
            if total * frac > amount:
                frac = amount / total
        if frac >= 1.0 - 1e-12:
            self.taint = {}
            return taint
        moved = {}
        for v, x in list(taint.items()):
            part = x * frac
            moved[v] = part
            rest = x - part
            if rest > TINY:
                taint[v] = rest
            else:
                del taint[v]
        return moved

    def _take_lots(self, amount: float, lifo: bool) -> dict[str, float]:
        lots = self.lots
        assert lots is not None
        moved: dict[str, float] = defaultdict(float)
        remaining = amount
        while remaining > TINY and lots:
            lot = lots[-1] if lifo else lots[0]
            if lot.amount <= remaining + TINY:
                for v, x in lot.vec.items():
                    moved[v] += x
                remaining -= lot.amount
                if lifo:
                    lots.pop()
                else:
                    lots.popleft()
            else:
                frac = remaining / lot.amount
                for v, x in list(lot.vec.items()):
                    part = x * frac
                    moved[v] += part
                    if x - part > TINY:
                        lot.vec[v] = x - part
                    else:
                        del lot.vec[v]
                lot.amount -= remaining
                remaining = 0.0
        return dict(moved)

    # -- credit; returns the taint absorbed by an overdrawn balance
    def put(self, amount: float, vec: dict[str, float]) -> dict[str, float]:
        if self.mode == MODE_ABSORB:
            self.balance += amount
            for v, x in vec.items():
                self.taint[v] = self.taint.get(v, 0.0) + x
            return {}
        absorbed: dict[str, float] = {}
        keep = amount
        if self.balance < -TINY:
            cover = min(amount, -self.balance)
            keep = amount - cover
            if vec and cover > 0:
                f = cover / amount
                absorbed = {v: x * f for v, x in vec.items()}
                vec = {v: x - absorbed[v] for v, x in vec.items()}
        self.balance += amount
        vec = {v: x for v, x in vec.items() if x > TINY}
        if self.lots is None:
            for v, x in vec.items():
                self.taint[v] = self.taint.get(v, 0.0) + x
        elif keep > TINY:
            if not vec and self.lots and not self.lots[-1].vec:
                self.lots[-1].amount += keep
            else:
                self.lots.append(_Lot(keep, vec))
        return absorbed


@dataclass
class _RawEdge:
    txn: Transaction
    vec: dict[str, float]
    kind: str
    is_seed: bool
    src_layer_at: int


@dataclass
class SimulationResult:
    edges: list[TrailEdge]  # pruned by min_edge_amount (seeds, exits, merchant and off-trail edges are kept)
    accounts: dict[str, AccountState]
    totals: TraceTotals
    victim_seed: dict[str, float]
    warnings: list[str] = field(default_factory=list)
    edge_count_total: int = 0  # tainted edges before pruning
    # raw (unrounded) flows, for invariants and tests
    cashed_out_flow: float = 0.0
    outbound_flow: float = 0.0
    unlocated_flow: float = 0.0


def _add(acc: dict[str, float], vec: dict[str, float]) -> None:
    for v, x in vec.items():
        acc[v] += x


def _close_same_second_groups(
    ledger: Ledger, groups: dict[datetime, list[Transaction]], simulated: set[str], follow_consortium: bool
) -> None:
    """Add the same-second transactions linked to a same-second group through accounts that are not simulated.

    They move no taint (neither side is simulated), but they are part of the group's credit-before-debit order
    (``event_order``), which must not depend on which accounts happen to be candidates. Only groups of two or
    more transactions with a counterparty outside the simulated set cost a (one-second) ledger lookup.
    """
    for ts in sorted(ts for ts, group in groups.items() if len(group) > 1):
        group = groups[ts]
        known = {t.txn_id for t in group}
        seen = {a for t in group for a in (t.from_account, t.to_account) if a}
        frontier = sorted(a for a in seen if a not in simulated)
        while frontier:
            got = list(ledger.transactions_touching(frontier, ts, ts))
            if follow_consortium:
                got.extend(ledger.consortium_transactions(frontier, ts, ts))
            new: set[str] = set()
            for t in got:
                if t.txn_id in known or ensure_utc(t.ts) != ts:
                    continue
                known.add(t.txn_id)
                group.append(t)
                for a in (t.from_account, t.to_account):
                    if a and a not in seen:
                        seen.add(a)
                        new.add(a)
            frontier = sorted(a for a in new if a not in simulated)


def simulate(ledger: Ledger, exp: Expansion, cfg: TraceConfig) -> SimulationResult:
    home = exp.home_bank_id
    order = exp.ncrp_ids
    warnings: list[str] = []
    ws, we = exp.window_start, exp.window_end
    lifo = cfg.rule == ApportionmentRule.LIFO
    use_lots = cfg.rule in (ApportionmentRule.FIFO, ApportionmentRule.LIFO)

    cand_ids = sorted(exp.candidates)
    opening = ledger.balances_at(cand_ids, ws) if cand_ids else {}
    books: dict[str, _Book] = {}
    for a in cand_ids:
        if a not in opening:
            warnings.append(f"No opening balance for {a}; assumed ₹0.00.")
        info = exp.candidates[a]
        kind = exp.terminal.get(a)
        if kind is None:
            mode = MODE_RULE
        elif kind in PASS_THROUGH_TERMINALS and (info.bank_id == home or cfg.follow_consortium):
            mode = MODE_LIBR
        else:
            mode = MODE_ABSORB
        own = frozenset(exp.victim_of.get(a, ()))
        books[a] = _Book(info, float(opening.get(a, 0.0)), kind, mode, use_lots, own)

    # ---- the event stream
    txns: dict[str, Transaction] = {}
    if cand_ids:
        for t in ledger.transactions_touching(cand_ids, ws, we):
            txns[t.txn_id] = t
        if cfg.follow_consortium:
            others = [a for a in cand_ids if exp.candidates[a].bank_id != home]
            if others:
                for t in ledger.consortium_transactions(others, ws, we):
                    txns.setdefault(t.txn_id, t)
    for t in exp.seeds:
        txns[t.txn_id] = t
    groups = by_timestamp(txns.values())
    _close_same_second_groups(ledger, groups, set(books), cfg.follow_consortium)
    events = event_order(groups)

    victims = set(exp.victims) | {t.from_account for t in exp.seeds if t.from_account}
    raw_edges: list[_RawEdge] = []
    cashed: dict[str, float] = defaultdict(float)
    outbound_sent: dict[str, float] = defaultdict(float)
    unlocated_by_reason: dict[str, float] = defaultdict(float)
    seed_fresh: dict[str, float] = defaultdict(float)  # ncrp_id -> the reporting victim's own money
    # (victim account, ncrp_id) -> other complaints' taint its reported transfers carried, own money
    carried: dict[tuple[str, str], tuple[dict[str, float], list[float]]] = {}
    bad_amounts = 0

    for t in events:
        if t.amount <= 0:
            bad_amounts += 1
            continue
        src, dst = t.from_account, t.to_account
        seed_n = exp.seed_owner.get(t.txn_id)
        if seed_n is None and src is not None and src == dst:
            continue  # self-transfer: no economic movement
        ts = ensure_utc(t.ts)

        # debit side
        uncredited = False  # payouts of merchant / intermediary terminals leave the trail
        if seed_n is not None:
            moved = {}
            if src is not None and src in books:
                moved = {v: x for v, x in books[src].take(t.amount, lifo).items() if x > TINY}
            other = sum(moved.values())
            fresh = max(t.amount - other, 0.0)
            seed_fresh[seed_n] += fresh
            if other > 0 and src is not None:
                books[src].sent_total += other
                vec, own = carried.setdefault((src, seed_n), (defaultdict(float), [0.0]))
                _add(vec, moved)
                own[0] += fresh
            if fresh > TINY:
                moved[seed_n] = moved.get(seed_n, 0.0) + fresh
            src_layer = 0
        elif src is not None and src in books:
            sb = books[src]
            moved = sb.take(t.amount, lifo)
            uncredited = sb.mode == MODE_LIBR
            src_layer = sb.layer if sb.layer is not None else cfg.max_depth
        else:
            moved = {}
            src_layer = 0
        moved = {v: x for v, x in moved.items() if x > TINY}
        mt = sum(moved.values())
        if mt > 0 and seed_n is None:
            books[src].sent_total += mt  # type: ignore[index]

        # credit side
        if dst is not None and dst in books:
            db = books[dst]
            if uncredited:
                db.put(t.amount, {})
            else:
                credit = moved
                if db.own and any(v in db.own for v in moved):
                    unlocated_by_reason[_REASON_VICTIM] += sum(x for v, x in moved.items() if v in db.own)
                    credit = {v: x for v, x in moved.items() if v not in db.own}
                absorbed = db.put(t.amount, credit)
                ct = sum(credit.values())
                if ct > 0:
                    ab = sum(absorbed.values())
                    if ab > 0:
                        unlocated_by_reason["overdraft"] += ab
                    db.received_total += ct
                    db.layer = src_layer + 1 if db.layer is None else min(db.layer, src_layer + 1)
                    if db.first_tainted_at is None:
                        db.first_tainted_at = ts
                    if src is not None:
                        db.parents.add(src)
                if mt > 0:
                    kind = DEST_ACCOUNT if ct > 0 else DEST_UNLOCATED
                    raw_edges.append(_RawEdge(t, moved, kind, seed_n is not None, src_layer))
                continue
        if mt <= 0:
            continue
        if dst is None:
            _add(cashed, moved)
            raw_edges.append(_RawEdge(t, moved, DEST_EXIT, seed_n is not None, src_layer))
            continue
        if dst in victims:
            unlocated_by_reason[_REASON_VICTIM] += mt
            kind = DEST_UNLOCATED
        else:
            info = exp.known_accounts.get(dst)
            bank = info.bank_id if info is not None else t.to_bank_id
            if bank is not None and bank != home and not cfg.follow_consortium:
                _add(outbound_sent, moved)
                kind = DEST_OUTBOUND
            else:
                if uncredited:
                    reason = _REASON_TERMINAL
                else:
                    reason = exp.dropped.get(dst)
                    if reason is None:
                        reason = DROP_DEPTH if src in exp.depth_limited else _REASON_UNTRACED
                unlocated_by_reason[reason] += mt
                kind = DEST_UNLOCATED
        raw_edges.append(_RawEdge(t, moved, kind, seed_n is not None, src_layer))

    if bad_amounts:
        warnings.append(f"{bad_amounts} transaction(s) with a non-positive amount were ignored.")

    # ---- edges (depth uses final layers)
    layer_of = {a: b.layer for a, b in books.items() if b.layer is not None}
    merchant_ids = {a for a, b in books.items() if b.account.account_type == AccountType.MERCHANT}
    edges: list[TrailEdge] = []
    all_count = 0
    for re_ in raw_edges:
        t = re_.txn
        vec = round_vec(re_.vec, order)
        total = vec_total(vec)
        if total <= 0:
            continue
        all_count += 1
        dst = t.to_account
        if re_.kind == DEST_ACCOUNT and dst in layer_of:
            depth = layer_of[dst]
        else:
            src_layer = 0 if re_.is_seed else layer_of.get(t.from_account or "", re_.src_layer_at)
            depth = src_layer + 1
        keep = total >= cfg.min_edge_amount or re_.is_seed or re_.kind != DEST_ACCOUNT or dst in merchant_ids
        if not keep:
            continue
        from_info = exp.known_accounts.get(t.from_account) if t.from_account else None
        to_info = exp.known_accounts.get(dst) if dst else None
        from_bank = from_info.bank_id if from_info is not None else t.from_bank_id
        to_bank = to_info.bank_id if to_info is not None else t.to_bank_id
        edges.append(
            TrailEdge(
                txn_id=t.txn_id,
                ts=ensure_utc(t.ts),
                from_account=t.from_account,
                to_account=dst,
                from_bank_id=from_bank,
                to_bank_id=to_bank,
                channel=t.channel,
                utr=t.utr,
                amount=t.amount,
                depth=depth,
                tainted=vec,
                tainted_total=total,
                via_consortium=from_bank != home and to_bank != home,
            )
        )
    edges.sort(key=lambda e: (e.ts, e.txn_id))

    # ---- account states
    end_probe = we + timedelta(microseconds=1)
    victim_bal = ledger.balances_at(exp.victims, end_probe) if exp.victims else {}
    on_trail = {a for a, b in books.items() if b.layer is not None and r2(b.received_total) > 0}
    accounts: dict[str, AccountState] = {}
    for v in exp.victims:
        if v in on_trail:  # a victim account holding another complaint's money is listed at its layer
            continue
        info = exp.victim_accounts[v]
        accounts[v] = AccountState(
            account_id=v,
            bank_id=info.bank_id,
            account_type=info.account_type,
            is_home_bank=info.bank_id == home,
            layer=0,
            first_tainted_at=None,
            balance_now=r2(victim_bal.get(v, 0.0)),
        )

    # merchants: the part of their tainted money that paid for delivered goods is settled (SOP 9.2)
    terminal_sources = {a for a, b in books.items() if b.terminal in PASS_THROUGH_TERMINALS}
    now_by_account = {a: round_vec(b.taint_now(), order) for a, b in books.items()}
    fractions = delivery_fractions(ledger, merchant_inflows(edges, merchant_ids, terminal_sources))
    undelivered = merchant_undelivered(
        edges, fractions, {m: now_by_account[m] for m in merchant_ids}, terminal_sources, order
    )
    buckets: dict[str, float] = defaultdict(float)
    states: list[AccountState] = []
    overdrawn: list[str] = []
    for a, b in books.items():
        raw_total = sum(b.taint_now().values())
        now = now_by_account[a]
        now_total = vec_total(now)
        info = b.account
        is_home = info.bank_id == home
        if a in merchant_ids:
            und = min(vec_total(undelivered.get(a, {})), now_total)
            und_raw = raw_total if eq(und, now_total) else und
            buckets["merchant_settled"] += raw_total - und_raw
            buckets["holdable" if is_home else "outbound"] += und_raw
        elif info.account_type in INTERMEDIARY_ACCOUNT_TYPES:
            # other-bank nodal / pool / escrow money is requested from that bank (OUTBOUND_CFCFRMS_REQUEST)
            buckets["intermediary" if is_home else "outbound"] += raw_total
        elif is_home:
            buckets["holdable"] += raw_total
        else:
            buckets["outbound"] += raw_total
        if a not in on_trail:
            continue
        states.append(
            AccountState(
                account_id=a,
                bank_id=info.bank_id,
                account_type=info.account_type,
                is_home_bank=is_home,
                layer=b.layer,
                first_tainted_at=b.first_tainted_at,
                balance_now=r2(b.balance),
                tainted_now=now,
                tainted_now_total=now_total,
                tainted_received_total=r2(b.received_total),
                tainted_sent_total=r2(b.sent_total),
                terminal=b.terminal,
                parents=sorted(b.parents),
            )
        )
        if b.overdrafts:
            overdrawn.append(a)
    if overdrawn:
        overdrawn.sort()
        warnings.append(
            f"{len(overdrawn)} traced account(s) had debits larger than the available balance (overdraft): "
            + ", ".join(overdrawn[:5])
            + (" ..." if len(overdrawn) > 5 else "")
            + ". All tainted money in them was treated as moved by those debits."
        )
    for st in sorted(states, key=lambda s: (s.layer, s.account_id)):
        accounts[st.account_id] = st
    for (va, n), (vec, own) in sorted(carried.items()):
        others = ", ".join(k for k in order if vec.get(k, 0.0) > TINY)
        warnings.append(
            f"Victim account {va} (complaint {n}) also held money traced to complaint(s) {others}; "
            f"{fmt_inr(r2(sum(vec.values())))} of its reported transfer(s) carried that money "
            f"({cfg.rule.value} rule) and {fmt_inr(r2(own[0]))} is attributed to {n}."
        )

    # ---- totals
    # each victim's own money (a victim account's reported transfer can also carry other complaints' money)
    victim_seed = {n: r2(seed_fresh.get(n, 0.0)) + 0.0 for n in order}
    # buckets are rounded once from exact flows; ``unlocated`` is the remainder, so the parts always add up to
    # ``stolen`` exactly and a few paise of rounding residue can land in it
    stolen = r2(sum(victim_seed.values()))
    cashed_out = r2(sum(cashed.values()))
    outbound = r2(buckets["outbound"] + sum(outbound_sent.values()))
    holdable = r2(buckets["holdable"])
    intermediary = r2(buckets["intermediary"])
    merchant_settled = r2(buckets["merchant_settled"])
    unlocated = r2(stolen - (holdable + cashed_out + outbound + intermediary + merchant_settled)) + 0.0
    totals = TraceTotals(
        stolen=stolen,
        holdable=holdable,
        cashed_out=cashed_out,
        outbound=outbound,
        intermediary=intermediary,
        merchant_settled=merchant_settled,
        unlocated=unlocated,
    )

    # ---- warnings about untraced money
    messages = {
        DROP_DEPTH: f"Depth limit reached (max_depth={cfg.max_depth}): {{amt}} of tainted money moved to accounts "
        "beyond the traced layers; reported as unlocated.",
        DROP_LIMIT: f"Account limit reached (max_accounts={cfg.max_accounts}): {{amt}} of tainted money moved to "
        "accounts that were not traced; reported as unlocated.",
        DROP_UNKNOWN: "{amt} of tainted money moved to accounts not found in the ledger; reported as unlocated.",
        _REASON_VICTIM: "{amt} of tainted money flowed back to a victim account; reported as unlocated.",
        _REASON_TERMINAL: "{amt} of tainted money was paid out of merchant or nodal / pool / escrow accounts, which "
        "are not traced further; reported as unlocated.",
        "overdraft": "{amt} of tainted money repaid overdrawn balances; reported as unlocated.",
        _REASON_UNTRACED: "{amt} of tainted money moved to accounts outside the traced set; reported as unlocated.",
    }
    for reason, msg in messages.items():
        amt = r2(unlocated_by_reason.get(reason, 0.0))
        if amt > 0:
            warnings.append(msg.format(amt=fmt_inr(amt)))

    return SimulationResult(
        edges=edges,
        accounts=accounts,
        totals=totals,
        victim_seed=victim_seed,
        warnings=warnings,
        edge_count_total=all_count,
        cashed_out_flow=sum(cashed.values()),
        outbound_flow=buckets["outbound"] + sum(outbound_sent.values()),
        unlocated_flow=sum(unlocated_by_reason.values()),
    )
