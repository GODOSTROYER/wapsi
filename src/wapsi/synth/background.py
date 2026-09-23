"""Vectorised background traffic: salaries, rent, bills, P2P, merchant payments (+ invoices), cash, ATM, payment
aggregator pools, B2B and a little legitimate crypto. Never touches ring-controlled accounts."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from wapsi.synth import names as nm
from wapsi.synth.builder import CH, DAY, HOUR, TxnChunk, WorldBuilder
from wapsi.synth.population import Population

_P = {
    "retail": [
        0.2,
        0.1,
        0.05,
        0.05,
        0.05,
        0.2,
        0.5,
        1.5,
        3,
        4,
        5,
        6,
        6,
        5,
        4.5,
        4.5,
        5,
        6,
        7.5,
        8,
        7,
        5,
        2.5,
        1,
    ],
    "p2p": [
        0.5,
        0.3,
        0.1,
        0.1,
        0.1,
        0.2,
        0.8,
        2,
        4,
        5,
        5,
        4.5,
        4.5,
        4,
        3.5,
        3.5,
        4,
        5,
        6,
        7,
        7.5,
        6,
        3.5,
        1.5,
    ],
    "atm": [0.3, 0.2, 0.1, 0.1, 0.1, 0.3, 1, 3, 5, 6, 6, 5.5, 5, 5, 4.5, 4.5, 5, 6, 6.5, 6, 5, 3, 1.5, 0.6],
    "branch": [0, 0, 0, 0, 0, 0, 0, 0.3, 1, 2, 7, 8, 8, 7, 7, 6, 4, 1.5, 1, 0.8, 0.5, 0.2, 0, 0],
    "office": [0, 0, 0, 0, 0, 0, 0, 0.2, 1, 4, 7, 8, 8, 6, 7, 7, 6, 4, 1.5, 0.5, 0.2, 0, 0, 0],
    "batch": [0, 0, 0, 0, 0, 0.5, 4, 6, 5, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "salary": [0, 0, 0, 0, 0, 0, 0, 0.5, 2, 6, 7, 5, 3, 1, 0.5, 0.5, 0.3, 0, 0, 0, 0, 0, 0, 0],
}
HOUR_P = {k: np.array(v) / np.sum(v) for k, v in _P.items()}


@dataclass
class BackgroundResult:
    chunk: TxnChunk
    invoices: dict[
        str, np.ndarray
    ]  # merchant, payer, txn (index into chunk), amount, ordered, delivered, status


class _Collector:
    def __init__(self, b: WorldBuilder) -> None:
        self.b = b
        self.parts: list[TxnChunk] = []
        self.n = 0

    def add(self, ts, frm, to, amt, chan, remark, label=-1) -> int:
        k = int(np.asarray(ts).shape[0])
        base = self.n

        def full(x, dtype):
            x = np.asarray(x)
            return np.full(k, x, dtype=dtype) if x.ndim == 0 else x.astype(dtype, copy=False)

        self.parts.append(
            TxnChunk(
                ts=full(ts, np.int64),
                frm=full(frm, np.int32),
                to=full(to, np.int32),
                amt=np.round(full(amt, np.float64), 2),
                chan=full(chan, np.int8),
                remark=full(remark, np.int32),
                label=full(label, np.int32),
            )
        )
        self.n += k
        return base


def _times(
    b: WorldBuilder,
    rng: np.random.Generator,
    n: int,
    profile: str,
    days: np.ndarray | None = None,
    weekday_only: bool = False,
) -> np.ndarray:
    if days is None:
        days = rng.integers(0, b.days, n)
        if weekday_only:  # push Sundays to Monday (world starts on a Saturday: day 1 is Sunday)
            days = np.where(days % 7 == 1, np.minimum(days + 1, b.days - 1), days)
    hours = rng.choice(24, size=n, p=HOUR_P[profile])
    return b.start_s + days.astype(np.int64) * DAY + hours.astype(np.int64) * HOUR + rng.integers(0, HOUR, n)


def _round_mix(
    rng: np.random.Generator, x: np.ndarray, p=(0.35, 0.3, 0.2, 0.15), steps=(100.0, 10.0, 1.0, 0.01)
) -> np.ndarray:
    st = np.asarray(steps)[rng.choice(len(steps), size=x.size, p=np.asarray(p) / np.sum(p))]
    return np.round(np.maximum(np.round(x / st) * st, st), 2)


def _pick(
    rng: np.random.Generator, pool: np.ndarray, n: int, weights: np.ndarray | None = None
) -> np.ndarray:
    if weights is None:
        return pool[rng.integers(0, pool.size, n)]
    return pool[rng.choice(pool.size, size=n, p=weights / weights.sum())]


def _upi_imps(rng: np.random.Generator, amt: np.ndarray, p_imps: float = 0.1) -> np.ndarray:
    ch = np.where(rng.random(amt.size) < p_imps, CH["IMPS"], CH["UPI"])
    return np.where(amt > 100_000, CH["IMPS"], ch).astype(np.int8)


def _labels_atm(b: WorldBuilder, rng: np.random.Generator, accts: np.ndarray) -> np.ndarray:
    cities = np.array([(c or "MUMBAI").upper().replace(" ", "-") for c in b.acct_city], dtype=object)
    city_codes, city_inv = np.unique(cities[accts], return_inverse=True)
    area = rng.integers(0, len(nm.CITY_AREAS), accts.size)
    nn = rng.integers(1, 40, accts.size)
    key = (city_inv.astype(np.int64) * len(nm.CITY_AREAS) + area) * 100 + nn
    uniq, inv = np.unique(key, return_inverse=True)
    codes = np.array(
        [
            b.text.code(
                f"ATM {city_codes[k // 100 // len(nm.CITY_AREAS)]}-{nm.CITY_AREAS[k // 100 % len(nm.CITY_AREAS)]}"
                f"-{k % 100:02d}"
            )
            for k in uniq.tolist()
        ],
        dtype=np.int32,
    )
    return codes[inv]


def generate_background(
    b: WorldBuilder, pop: Population, rng: np.random.Generator, activity: float
) -> BackgroundResult:
    c = _Collector(b)
    ind = pop.individuals
    n_ind = ind.size
    T = b.text
    propensity = np.exp(rng.normal(0, 0.8, n_ind))

    # ---- P2P (contact graph + some random)
    n = int(n_ind * 6.0 * activity)
    s_pos = rng.choice(n_ind, size=n, p=propensity / propensity.sum())
    contacts = ind[rng.integers(0, n_ind, (n_ind, 6))]
    to = np.where(rng.random(n) < 0.8, contacts[s_pos, rng.integers(0, 6, n)], ind[rng.integers(0, n_ind, n)])
    amt = _round_mix(rng, np.clip(np.exp(rng.normal(np.log(700), 1.1, n)), 1, 100_000))
    rem = T.codes(nm.P2P_REMARKS)[rng.integers(0, len(nm.P2P_REMARKS), n)]
    c.add(_times(b, rng, n, "p2p"), ind[s_pos], to, amt, _upi_imps(rng, amt, 0.12), rem)

    # ---- merchant retail (+ invoices)
    merch = pop.merchants
    cats = [b.acct_category[m] for m in merch.tolist()]
    pop_w = np.exp(rng.normal(0, 1.0, merch.size))
    n = int(n_ind * 8.0 * activity)
    m_pos = rng.choice(merch.size, size=n, p=pop_w / pop_w.sum())
    med = np.array([nm.MERCHANT_CATEGORIES[k][1] for k in cats])[m_pos]
    inv_p = np.array([nm.MERCHANT_CATEGORIES[k][2] for k in cats])[m_pos]
    amt = _round_mix(rng, np.clip(med * np.exp(rng.normal(0, 0.8, n)), 10, 450_000), p=(0.2, 0.3, 0.3, 0.2))
    payer = _pick(rng, ind, n, propensity)
    ch = np.where(rng.random(n) < 0.3, CH["POS"], _upi_imps(rng, amt, 0.02)).astype(np.int8)
    ts = _times(b, rng, n, "retail")
    rem = T.codes(nm.MERCHANT_REMARKS)[rng.integers(0, len(nm.MERCHANT_REMARKS), n)]
    base = c.add(ts, payer, merch[m_pos], amt, ch, rem)
    has_inv = rng.random(n) < inv_p
    k = int(has_inv.sum())
    ordered = ts[has_inv] - rng.integers(60, 2 * DAY, k)
    delivered = ts[has_inv] + rng.integers(2 * HOUR, 5 * DAY, k)
    u = rng.random(k)
    status = np.where(u < 0.94, 0, np.where(u < 0.98, 1, 2))  # DELIVERED / UNDELIVERED / CANCELLED
    status = np.where((status == 0) & (delivered > b.end_s), 1, status)
    delivered = np.where(status == 0, delivered, -1)
    invoices = {
        "merchant": merch[m_pos][has_inv],
        "payer": payer[has_inv],
        "txn": base + np.flatnonzero(has_inv),
        "amount": amt[has_inv],
        "ordered": ordered,
        "delivered": delivered,
        "status": status,
    }

    # ---- ATM withdrawals
    n = int(n_ind * 2.0 * activity)
    acc = _pick(rng, ind, n, propensity)
    amt = rng.choice(
        [500.0, 1000, 2000, 3000, 5000, 10000, 20000], size=n, p=[0.12, 0.2, 0.25, 0.1, 0.18, 0.12, 0.03]
    )
    c.add(_times(b, rng, n, "atm"), acc, -1, amt, CH["ATM"], T.code("ATM WDL"), _labels_atm(b, rng, acc))

    # ---- cash deposits (individuals + merchants)
    n = int(n_ind * 0.6 * activity)
    acc = _pick(rng, ind, n)
    amt = np.maximum(100.0, np.round(np.exp(rng.normal(np.log(8000), 1.0, n)), -2))
    c.add(_times(b, rng, n, "branch"), -1, acc, amt, CH["CASH_DEPOSIT"], T.code("cash deposit"))
    n = int(merch.size * b.days * 0.4)
    acc = _pick(rng, merch, n)
    amt = np.maximum(100.0, np.round(pop.wealth[acc] * 0.05 * np.exp(rng.normal(0, 0.6, n)), -2))
    c.add(_times(b, rng, n, "branch"), -1, acc, amt, CH["CASH_DEPOSIT"], T.code("CASH DEP COUNTER"))

    # ---- utility bills
    n = int(n_ind * 1.5 * activity)
    bpos = rng.integers(0, pop.billers.size, n)
    amt = np.round(np.clip(np.exp(rng.normal(np.log(900), 0.7, n)), 49, 40_000), 2)
    rem = T.codes(nm.BILL_REMARKS)[bpos % len(nm.BILL_REMARKS)]
    c.add(_times(b, rng, n, "p2p"), _pick(rng, ind, n), pop.billers[bpos], amt, CH["UPI"], rem)

    # ---- rent (monthly, fixed tenant -> landlord)
    n_ten = int(n_ind * 0.12)
    tenants = rng.choice(ind, size=n_ten, replace=False)
    landlords = rng.choice(ind, size=max(1, int(n_ind * 0.04)), replace=False)
    lord = landlords[rng.integers(0, landlords.size, n_ten)]
    ok = lord != tenants
    tenants, lord = tenants[ok], lord[ok]
    rent = np.round(rng.uniform(6000, 45000, tenants.size), -2)
    pay_dom = rng.integers(1, 8, tenants.size)
    rent_ch = rng.choice([CH["UPI"], CH["IMPS"], CH["NEFT"]], size=tenants.size, p=[0.7, 0.15, 0.15])
    for d in range(b.days):
        date = b.ist_date(b.day_start(d))
        sel = pay_dom == date.day
        if not sel.any():
            continue
        k = int(sel.sum())
        mon = date.strftime("%b")
        rem = T.codes([f"rent {mon}", f"RENT {mon.upper()}{date.strftime('%y')}", "house rent"])
        c.add(
            _times(b, rng, k, "p2p", days=np.full(k, d)),
            tenants[sel],
            lord[sel],
            rent[sel],
            rent_ch[sel],
            rem[rng.integers(0, 3, k)],
        )

    # ---- salaries (NEFT, monthly)
    biz = pop.businesses
    emp_w = pop.wealth[biz]
    salaried = rng.random(n_ind) < 0.55
    emp_of = biz[rng.choice(biz.size, size=n_ind, p=emp_w / emp_w.sum())]
    pay_rule = {int(e): int(rng.choice(3, p=[0.5, 0.3, 0.2])) for e in biz.tolist()}  # 1st / 7th / month end
    salary = np.round(np.clip(np.exp(rng.normal(np.log(38_000), 0.55, n_ind)), 9_000, 900_000), 0)
    rule = np.array([pay_rule[int(e)] for e in emp_of.tolist()])
    short = {int(e): " ".join(b.acct_name[int(e)].upper().split()[:2]) for e in biz.tolist()}
    for d in range(b.days):
        date = b.ist_date(b.day_start(d))
        nxt = b.ist_date(b.day_start(d) + DAY)
        day_rule = 0 if date.day == 1 else 1 if date.day == 7 else 2 if nxt.month != date.month else -1
        if day_rule < 0:
            continue
        sel = salaried & (rule == day_rule)
        if not sel.any():
            continue
        k = int(sel.sum())
        # month-end payers pay for the current month; 1st / 7th payers pay for the previous month
        paid_for = (date if day_rule == 2 else b.ist_date(b.day_start(d) - 10 * DAY)).strftime("%b").upper()
        emp = emp_of[sel]
        uniq, inv = np.unique(emp, return_inverse=True)
        rcodes = np.array(
            [T.code(f"SALARY {short[int(e)]} {paid_for}{date.strftime('%y')}") for e in uniq.tolist()],
            dtype=np.int32,
        )
        amt = salary[sel]
        ch = np.where(amt >= 200_000, CH["RTGS"], CH["NEFT"]).astype(np.int8)
        c.add(_times(b, rng, k, "salary", days=np.full(k, d)), emp, ind[sel], amt, ch, rcodes[inv])

    # ---- payment aggregator / nodal / escrow
    pools = pop.pools
    kinds = [b.acct_type[p] for p in pools.tolist()]
    coll_rem = {
        "POOL": T.code("PA COLLECT"),
        "NODAL": T.code("NODAL COLLECT"),
        "ESCROW": T.code("ESCROW DEPOSIT"),
    }
    n = int(n_ind * 1.5 * activity)
    ppos = rng.integers(0, pools.size, n)
    amt = _round_mix(rng, np.clip(np.exp(rng.normal(np.log(600), 1.0, n)), 10, 100_000))
    rem = np.array([coll_rem[k] for k in kinds], dtype=np.int32)[ppos]
    c.add(_times(b, rng, n, "retail"), _pick(rng, ind, n, propensity), pools[ppos], amt, CH["UPI"], rem)
    n = int(n_ind * 0.15 * activity)
    amt = _round_mix(rng, np.clip(np.exp(rng.normal(np.log(500), 0.8, n)), 10, 50_000))
    c.add(
        _times(b, rng, n, "retail"),
        _pick(rng, pools, n),
        _pick(rng, ind, n),
        amt,
        CH["UPI"],
        T.code("REFUND"),
    )
    linked_pool = pools[rng.integers(0, pools.size, merch.size)]
    for d in range(b.days):
        sel = rng.random(merch.size) < 0.5
        k = int(sel.sum())
        date = b.ist_date(b.day_start(d))
        amt = np.round(pop.wealth[merch[sel]] * 0.04 * np.exp(rng.normal(0, 0.7, k)), 2)
        ch = np.where(amt >= 200_000, CH["RTGS"], CH["NEFT"]).astype(np.int8)
        c.add(
            _times(b, rng, k, "batch", days=np.full(k, d)),
            linked_pool[sel],
            merch[sel],
            amt,
            ch,
            T.code(f"SETTLEMENT {date.strftime('%d-%m')}"),
        )

    # ---- B2B, supplier payments, consumer payments to businesses
    b2b_rem = T.codes(["INVOICE PAYMENT", "SUPPLIER PAYMENT", "PO PAYMENT", "SERVICE CHARGES", "OFFICE RENT"])
    n = int(biz.size * b.days * 0.6)
    frm = _pick(rng, biz, n)
    to = _pick(rng, biz, n)
    amt = np.round(np.clip(np.exp(rng.normal(np.log(150_000), 1.1, n)), 5_000, 50_000_000), 2)
    ch = np.where(amt >= 200_000, np.where(rng.random(n) < 0.7, CH["RTGS"], CH["NEFT"]), CH["NEFT"]).astype(
        np.int8
    )
    c.add(_times(b, rng, n, "office", weekday_only=True), frm, to, amt, ch, b2b_rem[rng.integers(0, 5, n)])
    n = int(merch.size * b.days * 0.15)
    amt = np.round(np.clip(np.exp(rng.normal(np.log(40_000), 0.9, n)), 1_000, 5_000_000), 2)
    ch = np.where(amt >= 200_000, CH["RTGS"], np.where(amt > 100_000, CH["NEFT"], CH["IMPS"])).astype(np.int8)
    c.add(
        _times(b, rng, n, "office", weekday_only=True),
        _pick(rng, merch, n),
        _pick(rng, biz, n),
        amt,
        ch,
        T.code("SUPPLIER PAYMENT"),
    )
    n = int(n_ind * 0.5 * activity)
    amt = _round_mix(rng, np.clip(np.exp(rng.normal(np.log(3500), 1.0, n)), 100, 200_000))
    c.add(
        _times(b, rng, n, "office"),
        _pick(rng, ind, n),
        _pick(rng, biz, n),
        amt,
        _upi_imps(rng, amt, 0.3),
        T.codes(["FEES", "SERVICE PAYMENT", "SUBSCRIPTION"])[rng.integers(0, 3, n)],
    )

    # ---- legitimate crypto purchases (so crypto alone is not a mule oracle)
    n = max(1, int(n_ind * 0.05 * activity))
    amt = np.maximum(500.0, np.round(np.exp(rng.normal(np.log(5000), 1.0, n)), -2))
    labels = T.codes(nm.VASPS)[rng.integers(0, len(nm.VASPS), n)]
    c.add(_times(b, rng, n, "p2p"), _pick(rng, ind, n), -1, amt, CH["CRYPTO"], T.code("VDA purchase"), labels)

    chunk = TxnChunk.concat(c.parts)
    # drop self transfers and transactions before an account was opened
    opened = np.array(b.acct_opened, dtype=np.int64)
    keep = chunk.frm != chunk.to
    keep &= (chunk.frm < 0) | (chunk.ts > opened[np.maximum(chunk.frm, 0)])
    keep &= (chunk.to < 0) | (chunk.ts > opened[np.maximum(chunk.to, 0)])
    newpos = np.cumsum(keep) - 1
    inv_keep = keep[invoices["txn"]]
    invoices = {k: v[inv_keep] for k, v in invoices.items()}
    invoices["txn"] = newpos[invoices["txn"]]
    chunk = TxnChunk(
        *(getattr(chunk, f)[keep] for f in ("ts", "frm", "to", "amt", "chan", "remark", "label"))
    )
    return BackgroundResult(chunk=chunk, invoices=invoices)
