"""Small helpers shared by the trail expansion, the taint simulation and the hold plan.

Both the trace (``TraceTotals.merchant_settled`` / ``holdable``) and the hold plan (``NO_HOLD_UPDATE_ONLY`` vs
``HOLD_IN_POOL`` and the in-pool hold amount) decide how much of a merchant's tainted money paid for delivered
goods. They must decide identically, so the rule lives here and works on the (never-pruned) tainted edges into
and out of merchant accounts.

Same-second transactions are replayed in ``event_order``: credits into an account before the debits they fund.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime

from wapsi.models import (
    INTERMEDIARY_ACCOUNT_TYPES,
    Account,
    AccountType,
    Channel,
    OrderStatus,
    TerminalKind,
    TrailEdge,
    Transaction,
)
from wapsi.money import r2
from wapsi.ports import Ledger
from wapsi.timeutil import ensure_utc

TINY = 1e-7  # rupees; float residue below this is treated as zero inside the simulation

# terminals that are simulated (lowest-intermediate-balance) but never expanded
PASS_THROUGH_TERMINALS = frozenset({TerminalKind.MERCHANT, TerminalKind.NODAL_POOL_ESCROW})


def terminal_kind(account: Account, *, home_bank_id: str, follow_consortium: bool) -> TerminalKind | None:
    """Why an account is a terminal of the trail (not expanded), or None."""
    if account.account_type in INTERMEDIARY_ACCOUNT_TYPES:
        return TerminalKind.NODAL_POOL_ESCROW
    if account.account_type == AccountType.MERCHANT:
        return TerminalKind.MERCHANT
    if account.bank_id != home_bank_id and not follow_consortium:
        return TerminalKind.OTHER_BANK
    return None


def exit_kind(channel: Channel) -> TerminalKind:
    return TerminalKind.CRYPTO_EXIT if channel == Channel.CRYPTO else TerminalKind.ATM_CASH_OUT


def round_vec(vec: Mapping[str, float], order: Iterable[str] = ()) -> dict[str, float]:
    """r2 every entry, drop entries that round to zero; keys in ``order`` first, then the rest sorted."""
    rank = {k: i for i, k in enumerate(order)}
    out: dict[str, float] = {}
    for k in sorted(vec, key=lambda k: (rank.get(k, len(rank)), k)):
        v = r2(vec[k])
        if v != 0:
            out[k] = v
    return out


def vec_total(vec: Mapping[str, float]) -> float:
    return r2(sum(vec.values()))


# --------------------------------------------------------------------------- event order


def _same_second_order(group: list[Transaction]) -> list[Transaction]:
    """Order one same-timestamp group: a transaction that credits account X goes before the ones debiting X.

    Cycles (X -> Y and Y -> X in the same second) are strongly connected components; their members, and
    otherwise unrelated transactions, are ordered by ``txn_id``. Deterministic for any input order.
    """
    group = sorted(group, key=lambda t: t.txn_id)
    n = len(group)
    debits_of: dict[str, list[int]] = defaultdict(list)
    for j, t in enumerate(group):
        if t.from_account is not None:
            debits_of[t.from_account].append(j)
    adj: list[list[int]] = [
        [j for j in debits_of.get(t.to_account, ()) if j != i] if t.to_account is not None else []
        for i, t in enumerate(group)
    ]

    # Tarjan's SCC (iterative)
    index = [-1] * n
    low = [0] * n
    on_stack = [False] * n
    stack: list[int] = []
    comp = [-1] * n
    comps: list[list[int]] = []
    counter = 0
    for root in range(n):
        if index[root] != -1:
            continue
        work = [(root, 0)]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True
        while work:
            v, k = work[-1]
            if k < len(adj[v]):
                work[-1] = (v, k + 1)
                w = adj[v][k]
                if index[w] == -1:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack[w] = True
                    work.append((w, 0))
                elif on_stack[w]:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                u = work[-1][0]
                low[u] = min(low[u], low[v])
            if low[v] == index[v]:
                members = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    comp[w] = len(comps)
                    members.append(w)
                    if w == v:
                        break
                comps.append(sorted(members))

    # Kahn over the condensation; ready components are taken by their smallest txn_id (= smallest index)
    indeg = [0] * len(comps)
    succ: list[set[int]] = [set() for _ in comps]
    for i in range(n):
        for j in adj[i]:
            a, b = comp[i], comp[j]
            if a != b and b not in succ[a]:
                succ[a].add(b)
                indeg[b] += 1
    ready = [(comps[c][0], c) for c in range(len(comps)) if indeg[c] == 0]
    heapq.heapify(ready)
    out: list[Transaction] = []
    while ready:
        _, c = heapq.heappop(ready)
        out.extend(group[i] for i in comps[c])
        for d in succ[c]:
            indeg[d] -= 1
            if indeg[d] == 0:
                heapq.heappush(ready, (comps[d][0], d))
    return out


def by_timestamp(txns: Iterable[Transaction]) -> dict[datetime, list[Transaction]]:
    by_ts: dict[datetime, list[Transaction]] = defaultdict(list)
    for t in txns:
        by_ts[ensure_utc(t.ts)].append(t)
    return by_ts


def event_order(txns: Iterable[Transaction] | Mapping[datetime, list[Transaction]]) -> list[Transaction]:
    """Transactions in replay order: by timestamp; within one timestamp, credits before the debits they fund.

    Accepts transactions or their ``by_timestamp`` grouping.
    """
    by_ts = txns if isinstance(txns, Mapping) else by_timestamp(txns)
    out: list[Transaction] = []
    for ts in sorted(by_ts):
        group = by_ts[ts]
        out.extend(group if len(group) == 1 else _same_second_order(group))
    return out


# --------------------------------------------------------------------------- merchants


def merchant_inflows(
    edges: Iterable[TrailEdge], merchant_ids: Iterable[str], terminal_sources: Iterable[str] = ()
) -> dict[str, list[str]]:
    """txn ids of the tainted edges credited to each merchant account.

    Payouts of merchant / nodal / pool / escrow accounts (``terminal_sources``) leave the trail: their edges are
    shown but never taint the receiver, so they are not inflows.
    """
    wanted = set(merchant_ids)
    skip = set(terminal_sources)
    out: dict[str, list[str]] = {m: [] for m in wanted}
    for e in edges:
        if e.to_account in wanted and e.tainted_total > 0 and e.from_account not in skip:
            out[e.to_account].append(e.txn_id)
    return out


def delivery_fractions(ledger: Ledger, inflows: Mapping[str, Iterable[str]]) -> dict[str, dict[str, float]]:
    """merchant -> txn_id -> share of the payment that bought delivered goods (SOP 9.2).

    Only the merchant's own invoices for the payment count. No invoice, or only UNDELIVERED / CANCELLED ones
    -> 0. Only DELIVERED ones -> 1. A payment covering both -> split by invoice amounts.
    """
    inflows = {m: list(ids) for m, ids in inflows.items()}
    all_ids = sorted({t for ids in inflows.values() for t in ids})
    delivered: dict[tuple[str, str], float] = {}
    other: dict[tuple[str, str], float] = {}
    if all_ids:
        for inv in ledger.invoices_for_transactions(all_ids):
            if not inv.txn_id:
                continue
            key = (inv.merchant_account, inv.txn_id)
            bucket = delivered if inv.order_status == OrderStatus.DELIVERED else other
            bucket[key] = bucket.get(key, 0.0) + max(inv.amount, 0.0)
    out: dict[str, dict[str, float]] = {}
    for m, ids in inflows.items():
        fr: dict[str, float] = {}
        for t in ids:
            key = (m, t)
            if key not in delivered:
                fr[t] = 0.0
            elif key not in other:
                fr[t] = 1.0
            else:
                d, o = delivered[key], other[key]
                fr[t] = d / (d + o) if d + o > 0 else 0.0
        out[m] = fr
    return out


def merchant_undelivered(
    edges: Iterable[TrailEdge],
    fractions: Mapping[str, Mapping[str, float]],
    tainted_now: Mapping[str, Mapping[str, float]],
    terminal_sources: Iterable[str] = (),
    order: Iterable[str] = (),
) -> dict[str, dict[str, float]]:
    """merchant -> tainted money (per ncrp_id) still in the account that paid for goods not delivered.

    ``tainted_now`` is the (rounded) tainted money each merchant holds. A merchant none of whose tainted inflows
    was delivered keeps all of it undelivered; a fully delivered one none. For a mix, the merchant's credited
    inflows (split by ``fractions``) and its tainted payouts are replayed; a payout takes each victim's delivered
    and undelivered money proportionally (exact for the lowest-intermediate-balance rule of terminals, where a
    payout scales the whole taint vector).
    """
    order = list(order)
    skip = set(terminal_sources)
    mixed = {
        m
        for m, fr in fractions.items()
        if any(f > 0 for f in fr.values()) and any(f < 1 for f in fr.values())
    }
    events: dict[str, list[tuple[datetime, int, str, TrailEdge]]] = defaultdict(list)
    if mixed:
        for e in edges:
            if e.tainted_total <= 0:
                continue
            if e.to_account in mixed and e.from_account not in skip:
                events[e.to_account].append((e.ts, 0, e.txn_id, e))
            if e.from_account in mixed:
                events[e.from_account].append((e.ts, 1, e.txn_id, e))
    out: dict[str, dict[str, float]] = {}
    for m, now in tainted_now.items():
        fr = fractions.get(m, {})
        if m not in mixed:
            delivered = bool(fr) and all(f >= 1.0 for f in fr.values())
            out[m] = {} if delivered else dict(now)
            continue
        ud: dict[str, float] = defaultdict(float)
        held: dict[str, float] = defaultdict(float)
        for _, kind, txn_id, e in sorted(events.get(m, ()), key=lambda x: x[:3]):
            if kind == 0:
                f = fr.get(txn_id, 0.0)
                for v, x in e.tainted.items():
                    held[v] += x
                    ud[v] += x * (1.0 - f)
            else:
                for v, x in e.tainted.items():
                    if held[v] <= 0:
                        continue
                    keep = max(0.0, 1.0 - x / held[v])
                    held[v] *= keep
                    ud[v] *= keep
        out[m] = round_vec({v: min(x, now.get(v, 0.0)) for v, x in ud.items()}, order)
    return out


def fully_delivered(fractions: Mapping[str, Mapping[str, float]]) -> set[str]:
    """Merchants whose tainted inflows *all* paid for delivered goods (no inflow -> not delivered)."""
    return {m for m, fr in fractions.items() if fr and all(f >= 1.0 for f in fr.values())}


def delivered_merchants(ledger: Ledger, inflows: Mapping[str, Iterable[str]]) -> set[str]:
    """Merchants whose tainted inflows *all* carry only DELIVERED invoices (for that merchant)."""
    return fully_delivered(delivery_fractions(ledger, inflows))
