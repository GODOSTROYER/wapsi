"""Candidate account discovery for a case: which accounts could have received the victims' money.

Seeds are the complaint transactions (victim -> first-layer). From there a time-respecting breadth-first search
follows outgoing transfers (``ts >= first possible tainted receipt`` and ``ts <= window_end``), bounded by
``TraceConfig.max_depth`` / ``max_accounts``. The search over-approximates: the exact simulation in
``wapsi.graph.taint`` decides how much tainted money actually moved.

- home-bank accounts are expanded with ``Ledger.transactions_touching``;
- other-bank accounts with ``Ledger.consortium_transactions`` when ``follow_consortium`` is on, otherwise they are
  terminals (``TerminalKind.OTHER_BANK``);
- nodal / pool / escrow and merchant accounts are terminals (kept as candidates, never expanded);
- debits without a destination account (ATM / crypto) are exits and need no expansion;
- a victim account of the case becomes a candidate only when money of *another* complaint of the case can reach
  it (a victim paying into another victim's account, or a traced account paying a victim of a multi-complaint
  case). Victim candidates are always expanded (never terminals), so their reported transfers can carry the other
  complaint's money on. Victims that only send stay out of the candidate set (layer 0).

Same-second transfers are included (``ts >= receipt time``): the simulation replays a same-second credit before
the debits it funds (``wapsi.graph.common.event_order``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from wapsi.config import TraceConfig
from wapsi.graph.common import terminal_kind
from wapsi.models import Account, TerminalKind, Transaction
from wapsi.ports import Ledger
from wapsi.timeutil import ensure_utc, utcnow

# why an account that received money was left out of the candidate set
DROP_DEPTH = "depth"
DROP_LIMIT = "limit"
DROP_UNKNOWN = "unknown"


@dataclass
class Expansion:
    home_bank_id: str
    ncrp_ids: list[str]
    window_start: datetime
    window_end: datetime
    seeds: list[Transaction]  # de-duplicated, sorted by (ts, txn_id)
    seed_owner: dict[str, str]  # txn_id -> ncrp_id
    victims: list[str]  # victim account ids known to the ledger, in complaint order
    victim_of: dict[str, set[str]]  # victim account -> ncrp_ids it is the victim of
    victim_accounts: dict[str, Account]
    candidates: dict[str, Account]  # non-victim accounts simulated by the taint engine
    bfs_layer: dict[str, int]
    arrival: dict[str, datetime]  # earliest possible tainted receipt found by the search
    terminal: dict[str, TerminalKind]  # candidates that are never expanded because of their type / bank
    depth_limited: set[str] = field(default_factory=set)  # expandable candidates at layer == max_depth
    dropped: dict[str, str] = field(default_factory=dict)  # account -> DROP_* reason
    known_accounts: dict[str, Account] = field(default_factory=dict)  # every account looked up
    warnings: list[str] = field(default_factory=list)

    def is_home(self, account_id: str | None) -> bool:
        acct = self.known_accounts.get(account_id) if account_id else None
        return acct is not None and acct.bank_id == self.home_bank_id


def _dedupe(ids: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(ids))


def expand_candidates(ledger: Ledger, ncrp_ids: Iterable[str], cfg: TraceConfig) -> Expansion:
    ncrp_ids = _dedupe(ncrp_ids)
    home = ledger.home_bank_id()
    warnings: list[str] = []

    # ---- seeds
    seed_owner: dict[str, str] = {}
    seeds: dict[str, Transaction] = {}
    victim_ids: list[str] = []
    victim_of: dict[str, set[str]] = {}
    for n in ncrp_ids:
        txns = ledger.complaint_transactions(n)
        if not txns:
            warnings.append(f"Complaint {n} has no linked victim transactions; nothing to trace for it.")
        for t in txns:
            if t.txn_id in seed_owner:
                if seed_owner[t.txn_id] != n:
                    warnings.append(
                        f"Transaction {t.txn_id} is reported in complaints {seed_owner[t.txn_id]} and {n}; "
                        f"its money is attributed to {seed_owner[t.txn_id]} only."
                    )
                continue
            if t.amount <= 0:
                warnings.append(f"Victim transaction {t.txn_id} has a non-positive amount and is ignored.")
                continue
            t = t.model_copy(update={"ts": ensure_utc(t.ts)})
            seed_owner[t.txn_id] = n
            seeds[t.txn_id] = t
            if t.from_account:
                victim_ids.append(t.from_account)
                victim_of.setdefault(t.from_account, set()).add(n)
        try:
            v = ledger.complaint(n).victim_account
        except (LookupError, ValueError):  # complaint unknown to this ledger
            continue
        victim_ids.append(v)
        victim_of.setdefault(v, set()).add(n)
    seed_list = sorted(seeds.values(), key=lambda t: (t.ts, t.txn_id))
    victim_ids = _dedupe(victim_ids)
    known: dict[str, Account] = dict(ledger.accounts(victim_ids))
    victims = [v for v in victim_ids if v in known]
    victim_set = set(victim_ids)

    if seed_list:
        window_start = seed_list[0].ts
        window_end = window_start + timedelta(hours=cfg.window_hours)
        if seed_list[-1].ts > window_end:
            window_end = seed_list[-1].ts + timedelta(hours=cfg.window_hours)
            warnings.append(
                "Victim transfers span more than the tracing window; the window was extended to "
                f"{cfg.window_hours:g} hours after the last victim transfer."
            )
    else:
        window_start = window_end = utcnow()

    exp = Expansion(
        home_bank_id=home,
        ncrp_ids=ncrp_ids,
        window_start=window_start,
        window_end=window_end,
        seeds=seed_list,
        seed_owner=seed_owner,
        victims=victims,
        victim_of=victim_of,
        victim_accounts={v: known[v] for v in victims},
        candidates={},
        bfs_layer={},
        arrival={},
        terminal={},
        known_accounts=known,
        warnings=warnings,
    )

    # ---- time-respecting BFS (label-correcting: an account is re-expanded if its arrival or layer improves)
    pending: dict[str, tuple[datetime, int]] = {}
    case_ids = set(ncrp_ids)

    def offer(acct: str, ts: datetime, layer: int, seed_of: str | None = None) -> None:
        if acct in victim_set:
            own = victim_of.get(acct, set())
            # only money of another complaint of this case can put a victim account on the trail
            foreign = seed_of not in own if seed_of is not None else bool(case_ids - own)
            if not foreign:
                return
        cur = pending.get(acct)
        pending[acct] = (ts, layer) if cur is None else (min(cur[0], ts), min(cur[1], layer))

    for t in seed_list:
        if t.to_account:
            offer(t.to_account, t.ts, 1, seed_owner[t.txn_id])

    fetched_from: dict[str, datetime] = {}  # account -> start of the cached debit list
    debits: dict[str, list[Transaction]] = {}
    limit_dropped: set[str] = set()

    while pending:
        new_ids = [a for a in pending if a not in known and a not in exp.dropped]
        if new_ids:
            known.update(ledger.accounts(new_ids))
        to_expand: set[str] = set()
        for acct in sorted(pending):
            ts, layer = pending[acct]
            info = known.get(acct)
            if info is None:
                exp.dropped[acct] = DROP_UNKNOWN
                continue
            if layer > cfg.max_depth:
                if acct not in exp.candidates:
                    exp.dropped.setdefault(acct, DROP_DEPTH)
                continue
            if acct not in exp.candidates:
                if len(exp.candidates) >= cfg.max_accounts:
                    exp.dropped[acct] = DROP_LIMIT
                    limit_dropped.add(acct)
                    continue
                exp.dropped.pop(acct, None)
                exp.candidates[acct] = info
                exp.arrival[acct] = ts
                exp.bfs_layer[acct] = layer
                kind = terminal_kind(info, home_bank_id=home, follow_consortium=cfg.follow_consortium)
                if kind is not None and acct not in victim_set:
                    exp.terminal[acct] = kind
                improved = True
            else:
                improved = ts < exp.arrival[acct] or layer < exp.bfs_layer[acct]
                exp.arrival[acct] = min(exp.arrival[acct], ts)
                exp.bfs_layer[acct] = min(exp.bfs_layer[acct], layer)
            if not improved or acct in exp.terminal:
                continue
            if exp.bfs_layer[acct] >= cfg.max_depth:
                exp.depth_limited.add(acct)
                continue
            exp.depth_limited.discard(acct)
            to_expand.add(acct)
        pending = {}
        if not to_expand:
            break

        # fetch (batched) the debits we have not cached yet
        need = [a for a in sorted(to_expand) if a not in fetched_from or exp.arrival[a] < fetched_from[a]]
        if need:
            start = min(exp.arrival[a] for a in need)
            # other-bank accounts are only expanded through the consortium when it is followed; an other-bank
            # victim candidate is otherwise expanded over what the home bank can see
            followed = {a for a in need if exp.candidates[a].bank_id != home and cfg.follow_consortium}
            home_ids = [a for a in need if a not in followed]
            other_ids = [a for a in need if a in followed]
            got: list[Transaction] = []
            if home_ids:
                got.extend(ledger.transactions_touching(home_ids, start, window_end))
            if other_ids:
                got.extend(ledger.consortium_transactions(other_ids, start, window_end))
            fresh: dict[str, dict[str, Transaction]] = {a: {} for a in need}
            for t in got:
                if t.from_account in fresh:
                    fresh[t.from_account][t.txn_id] = t
            for a in need:
                debits[a] = sorted(fresh[a].values(), key=lambda t: (ensure_utc(t.ts), t.txn_id))
                fetched_from[a] = start

        for acct in sorted(to_expand):
            since = exp.arrival[acct]
            child_layer = exp.bfs_layer[acct] + 1
            for t in debits.get(acct, ()):
                ts = ensure_utc(t.ts)
                if ts < since or ts > window_end or not t.to_account or t.to_account == acct:
                    continue
                offer(t.to_account, ts, child_layer)

    if limit_dropped:
        warnings.append(
            f"Account limit reached (max_accounts={cfg.max_accounts}): {len(limit_dropped)} further "
            "account(s) that received money from traced accounts were not traced."
        )
    unknown = sorted(a for a, why in exp.dropped.items() if why == DROP_UNKNOWN)
    if unknown:
        warnings.append(
            f"{len(unknown)} destination account(s) are not in the ledger and could not be traced: "
            + ", ".join(unknown[:5])
            + (" ..." if len(unknown) > 5 else "")
        )
    return exp
