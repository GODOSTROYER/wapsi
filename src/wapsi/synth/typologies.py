"""Fraud-ring typology templates T1..T10 and the ring orchestrator (hard-only T11..T15: ``wapsi.synth.hard``).

Each template writes one *episode* (one or more victims plus fresh ring accounts). A ring may run several
episodes (same gang: shared devices, fresh mules), each of which becomes its own golden case unless the
victims share first-layer accounts (T7).
"""

from __future__ import annotations

import numpy as np

from wapsi.synth import names as nm
from wapsi.synth.builder import CH, DAY, HOUR, MINUTE, WorldBuilder, upi_or_imps
from wapsi.synth.population import Population
from wapsi.synth.rings import HARD_TYPOLOGIES, TYPOLOGIES, Episode, RingContext, RingLedger, round_down


def _mins(ctx: RingContext, lo: float, hi: float) -> int:
    return int(ctx.rng.uniform(lo, hi) * MINUTE)


def _invoice(
    ctx: RingContext, merchant: int, payer: int, txn: int, amount: float, paid_s: int, delivered: bool
) -> None:
    r, b = ctx.rng, ctx.b
    b.invoices.append(
        {
            "merchant": merchant,
            "payer": payer,
            "txn": txn,
            "amount": amount,
            "ordered_at": paid_s - _mins(ctx, 2, 25),
            "delivered_at": paid_s + int(r.uniform(1, 8) * HOUR) if delivered else None,
            "order_status": "DELIVERED" if delivered else "UNDELIVERED",
        }
    )


# --------------------------------------------------------------------------- T1
def t1_fanout(ctx: RingContext, ep: Episode) -> int:
    r, led = ctx.rng, ctx.led
    a = ctx.new_mule(ctx.b.home, ep.t0)
    v = ctx.pick_victim()
    t = ctx.seed(ep, v, a, ep.t0, ctx.victim_amount())
    k = int(r.integers(3, 7))
    sweep_a = r.random() < 0.35
    if sweep_a and r.random() < 0.5:  # first layer withdraws some cash, then empties itself
        t = ctx.cash_out(a, t + _mins(ctx, 3, 10), min(50_000.0, led.balance(a) * r.uniform(0.1, 0.3)))
    total = led.balance(a) * (1.0 if sweep_a else r.uniform(0.85, 1.0))
    k = max(1, min(k, int(total // 6000)))
    t += ctx.hop_delay(3, 20)
    clean = [bool(r.random() < 0.4) for _ in range(k)]
    subs = [ctx.new_mule(ctx.mule_bank(0.25), t, clean=c) for c in clean]
    times = ctx.split_forward(a, subs, t, total, step=500, sweep=sweep_a)
    end = max([t, *times])
    if not sweep_a and r.random() < 0.3 and led.balance(a) >= 5000:
        end = max(end, ctx.cash_out(a, end, led.balance(a) * r.uniform(0.5, 1.0)))
    for s, ts, c in zip(subs, times, clean, strict=True):
        if ts < 0:
            continue
        frac = r.uniform(0.9, 1.0) if r.random() < 0.6 else r.uniform(0.4, 0.8)
        end = max(
            end, ctx.cash_out(s, ts + _mins(ctx, 10, 60), led.balance(s) * frac, max_gap_min=8, sweep=c)
        )
    return end


# --------------------------------------------------------------------------- T2
def t2_chain(ctx: RingContext, ep: Episode) -> int:
    r, led = ctx.rng, ctx.led
    depth = int(r.integers(3, 6))
    chain = [ctx.new_mule(ctx.b.home, ep.t0)]
    v = ctx.pick_victim()
    t = ctx.seed(ep, v, chain[0], ep.t0, ctx.victim_amount())
    for _i in range(1, depth):
        t += ctx.hop_delay(5, 70)
        nxt = ctx.new_mule(ctx.mule_bank(0.2), t)
        chain.append(nxt)
        src = chain[-2]
        if r.random() < 0.3:
            side = ctx.new_mule(ctx.b.home, t)
            t = ctx.forward(src, side, t, led.balance(src) * r.uniform(0.05, 0.15), step=1000)
        total = led.balance(src) * r.uniform(0.8, 1.0)
        step = 5000.0 if total > 100_000 else 1000.0
        if r.random() < 0.3:  # pass-through: forwards its whole balance
            t = ctx.forward(src, nxt, t, 0.0, exact=True)
        elif r.random() < 0.6 and total > 2 * step:
            first = round_down(total * r.uniform(0.45, 0.65), step)
            t = ctx.forward(src, nxt, t, first, step)
            t = ctx.forward(src, nxt, t + _mins(ctx, 1, 6), total - first, step)
        else:
            t = ctx.forward(src, nxt, t, total, step)
    last = chain[-1]
    t += _mins(ctx, 5, 60)
    u = r.random()
    if depth <= 4 and u < 0.25:
        x = ctx.new_mule(ctx.other_bank(), t)
        t = ctx.forward(last, x, t, led.balance(last) * r.uniform(0.85, 1.0), 1000)
        if r.random() < 0.6:
            t = ctx.cash_out(x, t + _mins(ctx, 10, 40), led.balance(x) * r.uniform(0.3, 0.7))
    elif u < 0.65:
        t = ctx.cash_out(last, t, led.balance(last) * r.uniform(0.6, 0.95))
    else:
        t = ctx.crypto_out(last, t, led.balance(last) * r.uniform(0.7, 0.95))
    return t


# --------------------------------------------------------------------------- T3
def t3_merchant_wash(ctx: RingContext, ep: Episode) -> int:
    r, led = ctx.rng, ctx.led
    a = ctx.new_mule(ctx.b.home, ep.t0)
    v = ctx.pick_victim()
    t = ctx.seed(ep, v, a, ep.t0, ctx.victim_amount())
    purchases = [(nm.WASH_CATEGORIES, r.uniform(0.35, 0.7), r.random() < 0.75)]
    if r.random() < 0.4:
        purchases.append((nm.WASH_CATEGORIES, r.uniform(0.2, 0.5), True))
    for cats, frac, delivered in purchases:
        k = ctx.home_merchant(cats)
        amt = round_down(led.balance(a) * frac, 10)
        if amt < 1000:
            continue
        amt = amt - 10 if r.random() < 0.5 else amt  # ticket-like 64,990
        t += _mins(ctx, 10, 60)
        chan = CH["POS"] if r.random() < 0.3 else upi_or_imps(amt, r, 0.7)
        idx, t = led.tx(t, a, k, amt, chan, f"order {int(r.integers(10000, 99999))}")
        _invoice(ctx, k, a, idx, amt, t, delivered)
    if r.random() < 0.5 and led.balance(a) > 5000:
        t = ctx.cash_out(a, t + _mins(ctx, 5, 30), led.balance(a) * r.uniform(0.5, 0.9))
    return t


# --------------------------------------------------------------------------- T4
def t4_crypto(ctx: RingContext, ep: Episode) -> int:
    r, led = ctx.rng, ctx.led
    a = ctx.new_mule(ctx.b.home, ep.t0)
    v = ctx.pick_victim()
    t = ctx.seed(ep, v, a, ep.t0, ctx.victim_amount())
    src = a
    if r.random() < 0.5:
        t += ctx.hop_delay(5, 30)
        bm = ctx.new_mule(ctx.mule_bank(0.3), t)
        t = ctx.forward(a, bm, t, led.balance(a) * r.uniform(0.85, 1.0), 1000)
        src = bm
    return ctx.crypto_out(
        src, t + _mins(ctx, 5, 40), led.balance(src) * r.uniform(0.7, 0.97), sweep=bool(r.random() < 0.4)
    )


# --------------------------------------------------------------------------- T5
def t5_cross_bank(ctx: RingContext, ep: Episode) -> int:
    r, led = ctx.rng, ctx.led
    a = ctx.new_mule(ctx.b.home, ep.t0)
    v = ctx.pick_victim()
    t = ctx.seed(ep, v, a, ep.t0, ctx.victim_amount())
    t += ctx.hop_delay(5, 40)
    x = ctx.new_mule(ctx.other_bank(), t)
    t = ctx.forward(a, x, t, led.balance(a) * r.uniform(0.8, 1.0), 1000)
    t += _mins(ctx, 10, 50)
    y = ctx.new_mule(ctx.b.home, t)
    keeps = r.random() < 0.5
    t = ctx.forward(x, y, t, led.balance(x) * (r.uniform(0.6, 0.9) if keeps else 1.0), 1000, exact=not keeps)
    if keeps and r.random() < 0.3:
        t = ctx.cash_out(x, t + _mins(ctx, 5, 30), led.balance(x) * r.uniform(0.3, 0.7))
    u = r.random()
    if u < 0.5:
        t = ctx.cash_out(y, t + _mins(ctx, 10, 40), led.balance(y) * r.uniform(0.4, 0.8))
    elif u < 0.8:
        t = ctx.crypto_out(y, t + _mins(ctx, 10, 40), led.balance(y) * r.uniform(0.5, 0.9))
    return t


# --------------------------------------------------------------------------- T6
def t6_triangulation(ctx: RingContext, ep: Episode) -> int:
    """Victim pays a genuine seller (innocent holder, layer 1) who later pays rent to an innocent landlord."""
    b, r, led, pop = ctx.b, ctx.rng, ctx.led, ctx.pop
    t0 = ep.t0
    business = r.random() < 0.6
    if business:
        sname = b.merchant_name(str(r.choice(["electronics", "apparel", "online"]))).replace(
            "Stores", "Traders"
        )
        city = b.person_name()[1]
        opened = t0 - int(r.uniform(2, 12) * 365 * DAY)
        gstin = b.new_gstin(sname, "ACTIVE", b.ist_date(opened))
        cid = b.new_customer(sname, city, "FULL", None)
        atype = "BUSINESS"
    else:
        sname, city, _ = b.person_name()
        opened = t0 - int(r.uniform(2, 12) * 365 * DAY)
        gstin = None
        cid = b.new_customer(sname, city, "FULL", b.random_dob(1965, 1998))
        atype = "INDIVIDUAL"
    seller = b.new_account(
        b.home,
        atype,
        opened,
        cid,
        name=sname,
        city=city,
        gstin=gstin,
        opening=float(np.round(r.uniform(40_000, 250_000), 2)),
        role="INNOCENT_HOLDER",
        dedicated=True,
        ring=ctx.ring_id,
        typology=ctx.typology,
    )
    lname, lcity, _ = b.person_name()
    lcid = b.new_customer(lname, lcity, "FULL", b.random_dob(1950, 1985))
    landlord = b.new_account(
        b.home,
        "INDIVIDUAL",
        t0 - int(r.uniform(3, 15) * 365 * DAY),
        lcid,
        name=lname,
        city=lcity,
        opening=float(np.round(r.uniform(50_000, 300_000), 2)),
        role="INNOCENT_HOLDER",
        dedicated=True,
        ring=ctx.ring_id,
        typology=ctx.typology,
    )
    for acct in (seller, landlord):
        led.track(acct)
        ctx.innocent_signal(acct, medium_p=0.35 if acct == seller else 0.15)
        b.add_device(acct, b.acct_opened[acct] + int(r.integers(0, 30)) * DAY)
    rent = round_down(r.uniform(12_000, 40_000), 500)
    # history inside the world, before the fraud (emitted in time order)
    pre_lo = b.start_s + HOUR
    opened_at = np.array(b.acct_opened, dtype=np.int64)
    buyers = pop.individuals[opened_at[pop.individuals] < b.start_s]  # pre-world customers only
    events: list[tuple[int, int, int, int, float, str]] = []  # (ts, order, frm, to, amount, remark)
    for _ in range(int(r.integers(6, 16))):
        events.append(
            (
                int(r.integers(pre_lo, t0 - 2 * HOUR)),
                len(events),
                int(buyers[int(r.integers(buyers.size))]),
                seller,
                round_down(r.uniform(800, 25_000), 10),
                "order",
            )
        )
    for d in range(0, b.days):
        day = b.day_start(d)
        if day + 5 * DAY >= t0:
            break
        date = b.ist_date(day)
        if date.day == 3:
            events.append(
                (
                    day + int(r.uniform(9, 20) * HOUR),
                    len(events),
                    seller,
                    landlord,
                    rent,
                    f"RENT {date.strftime('%b').upper()}",
                )
            )
        if date.day == 5:
            events.append(
                (
                    day + int(r.uniform(9, 20) * HOUR),
                    len(events),
                    int(buyers[int(r.integers(buyers.size))]),
                    landlord,
                    round_down(r.uniform(8_000, 30_000), 500),
                    "house rent",
                )
            )
    supplier_at = int(r.integers(pre_lo, t0 - 2 * HOUR)) if r.random() < 0.7 else None
    supplier = int(pop.businesses[int(r.integers(pop.businesses.size))])
    for ts, _o, frm, to, amt, rem in sorted(events):
        if supplier_at is not None and supplier_at < ts:
            led.tx(
                supplier_at,
                seller,
                supplier,
                round_down(min(led.balance(seller) * 0.3, 60_000), 100),
                CH["NEFT"],
                "stock purchase",
            )
            supplier_at = None
        if frm == seller and led.balance(seller) < amt:
            continue
        led.tx(ts, frm, to, amt, CH["UPI"], rem)
    item = nm.SELL_ITEMS[int(r.integers(len(nm.SELL_ITEMS)))]
    v = ctx.pick_victim()
    price = round_down(r.uniform(15_000, 160_000), 500) - float(r.choice([0, 1, 10]))
    t = ctx.seed(
        ep,
        v,
        seller,
        t0,
        price,
        category="ONLINE_SHOPPING",
        item=item,
        split_p=0.0,
        remark=f"ORDER {item[:18]}",
    )
    # ordinary sales keep arriving (commingling)
    t_rent = t + int(r.uniform(2, 40) * HOUR)
    for ts in sorted(int(x) for x in r.integers(t + MINUTE, t_rent - MINUTE, size=int(r.integers(0, 5)))):
        led.tx(
            ts,
            int(buyers[int(r.integers(buyers.size))]),
            seller,
            round_down(r.uniform(800, 20_000), 10),
            CH["UPI"],
            "order",
        )
    rent_now = min(rent, round_down(led.balance(seller) * 0.8, 500))
    _, t = led.tx(
        max(t_rent, led.last[seller] + MINUTE),
        seller,
        landlord,
        rent_now,
        CH["UPI"],
        f"RENT {b.ist_date(t_rent).strftime('%b').upper()}",
    )
    if r.random() < 0.5:
        k = ctx.home_merchant(nm.WHOLESALE_CATEGORIES)
        amt = round_down(led.balance(seller) * r.uniform(0.1, 0.35), 10)
        if amt >= 1000:
            t += int(r.uniform(1, 6) * HOUR)
            idx, t = led.tx(t, seller, k, amt, upi_or_imps(amt, r, 0.5), "stock purchase")
            _invoice(ctx, k, seller, idx, amt, t, True)
    return t


# --------------------------------------------------------------------------- T7
def t7_multi_victim(ctx: RingContext, ep: Episode) -> int:
    r, led, b = ctx.rng, ctx.led, ctx.b
    m = ctx.new_mule(b.home, ep.t0)
    if r.random() < 0.5:
        fri, score, _ = b.signal_override[m]
        b.signal_override[m] = (fri, score, int(r.integers(2, 5)))
    nv = int(r.integers(2, 5))
    t = ep.t0
    fwd_target: int | None = None
    for j in range(nv):
        v = ctx.pick_victim()
        t = ctx.seed(ep, v, m, t, ctx.victim_amount(), fir_p=0.45)
        if j < nv - 1:
            gap = int(r.uniform(15, 240) * MINUTE)
            if r.random() < 0.5:
                mid = t + gap // 3
                if r.random() < 0.5:
                    mid = ctx.cash_out(m, mid, led.balance(m) * r.uniform(0.2, 0.5))
                else:
                    fwd_target = (
                        fwd_target if fwd_target is not None else ctx.new_mule(ctx.mule_bank(0.3), mid)
                    )
                    mid = ctx.forward(m, fwd_target, mid, led.balance(m) * r.uniform(0.3, 0.6), 1000)
                t = max(t + gap, mid + 10 * MINUTE)
            else:
                t += gap
    t += _mins(ctx, 10, 60)
    u = r.random()
    if u < 0.4:
        t = ctx.cash_out(m, t, led.balance(m) * r.uniform(0.2, 0.6))
    elif u < 0.7:
        fwd_target = fwd_target if fwd_target is not None else ctx.new_mule(ctx.mule_bank(0.3), t)
        t = ctx.forward(m, fwd_target, t, led.balance(m) * r.uniform(0.3, 0.7), 1000)
    if fwd_target is not None and r.random() < 0.5:
        t = ctx.cash_out(fwd_target, t + _mins(ctx, 10, 40), led.balance(fwd_target) * r.uniform(0.3, 0.9))
    return t


# --------------------------------------------------------------------------- T8
def t8_dormant(ctx: RingContext, ep: Episode) -> int:
    b, r, led = ctx.b, ctx.rng, ctx.led
    name, city, _ = b.person_name()
    cid = b.new_customer(name, city, "FULL", b.random_dob(1960, 1995))
    opened = b.start_s - int(r.uniform(7, 15) * 365 * DAY)
    d = b.new_account(
        b.home,
        "INDIVIDUAL",
        opened,
        cid,
        name=name,
        city=city,
        opening=float(np.round(r.uniform(150, 9000), 2)),
        role="MULE",
        dedicated=True,
        ring=ctx.ring_id,
        typology=ctx.typology,
    )
    led.track(d)
    b.signal_override[d] = ctx.mule_signal("low")
    b.add_device(d, opened + DAY)
    b.add_device(d, ep.t0 - int(r.uniform(1, 5) * DAY), device=ctx.devices[0])
    v = ctx.pick_victim()
    t = ctx.seed(ep, v, d, ep.t0, ctx.victim_amount())
    t += ctx.hop_delay(5, 45)
    subs = [ctx.new_mule(ctx.mule_bank(0.3), t) for _ in range(int(r.integers(2, 4)))]
    times = ctx.split_forward(d, subs, t, led.balance(d) * r.uniform(0.7, 0.95), step=1000)
    end = max([t, *times])
    if r.random() < 0.5 and led.balance(d) > 5000:
        end = max(end, ctx.cash_out(d, end, min(20_000, led.balance(d) * 0.5)))
    for s, ts in zip(subs, times, strict=True):
        if ts > 0 and r.random() < 0.7:
            end = max(end, ctx.cash_out(s, ts + _mins(ctx, 10, 50), led.balance(s) * r.uniform(0.4, 1.0)))
    return end


# --------------------------------------------------------------------------- T9
def t9_device_cluster(ctx: RingContext, ep: Episode) -> int:
    r, led, b = ctx.rng, ctx.led, ctx.b

    def cluster_mule(bank: str, when: int) -> int:
        return ctx.new_mule(
            bank,
            when,
            level="low",
            older_p=0.35,
            share_device=1.0,
            kyc=str(r.choice(["FULL", "VKYC", "MIN"], p=[0.5, 0.3, 0.2])),
        )

    a = cluster_mule(b.home, ep.t0)
    for _ in range(int(r.integers(1, 3))):  # members that never receive money in this episode
        cluster_mule(ctx.mule_bank(0.3), ep.t0)
    v = ctx.pick_victim()
    t = ctx.seed(ep, v, a, ep.t0, ctx.victim_amount())
    t += ctx.hop_delay(10, 40)
    bm = cluster_mule(ctx.mule_bank(0.2), t)
    t = ctx.forward(a, bm, t, led.balance(a) * r.uniform(0.85, 1.0), 1000)
    t += _mins(ctx, 10, 40)
    c = cluster_mule(ctx.mule_bank(0.3), t)
    t = ctx.forward(bm, c, t, led.balance(bm) * r.uniform(0.4, 0.7), 1000)
    t = ctx.cash_out(bm, t + _mins(ctx, 5, 20), led.balance(bm) * r.uniform(0.5, 0.9))
    if r.random() < 0.5:
        t = ctx.crypto_out(c, t + _mins(ctx, 5, 30), led.balance(c) * r.uniform(0.5, 0.95))
    else:
        t = ctx.cash_out(c, t + _mins(ctx, 5, 30), led.balance(c) * r.uniform(0.3, 0.9))
    return t


# --------------------------------------------------------------------------- T10
def t10_nodal_pool(ctx: RingContext, ep: Episode) -> int:
    r, led, b = ctx.rng, ctx.led, ctx.b
    a = ctx.new_mule(b.home, ep.t0)
    v = ctx.pick_victim()
    t = ctx.seed(ep, v, a, ep.t0, ctx.victim_amount())
    src = a
    if r.random() < 0.3:
        t += _mins(ctx, 5, 30)
        bm = ctx.new_mule(b.home, t)
        t = ctx.forward(a, bm, t, led.balance(a) * r.uniform(0.9, 1.0), 1000)
        src = bm
    pool = ctx.home_pool()
    t += ctx.hop_delay(5, 40)
    kind = b.acct_type[pool]
    remark = {"POOL": "PA WALLET LOAD", "NODAL": "NODAL COLLECT", "ESCROW": "ESCROW DEPOSIT"}[kind]
    t = ctx.forward(
        src,
        pool,
        t,
        led.balance(src) * r.uniform(0.3, 0.9),
        1000,
        remark=f"{remark} {int(r.integers(10**7, 10**8))}",
    )
    if r.random() < 0.6 and led.balance(src) > 5000:
        t = ctx.cash_out(src, t + _mins(ctx, 5, 30), led.balance(src) * r.uniform(0.5, 0.9))
    return t


TEMPLATE_FNS = {
    "T1_FANOUT_CASHOUT": t1_fanout,
    "T2_CHAIN_LAYERING": t2_chain,
    "T3_MERCHANT_WASH": t3_merchant_wash,
    "T4_CRYPTO_EXIT": t4_crypto,
    "T5_CROSS_BANK_HOP": t5_cross_bank,
    "T6_TRIANGULATION": t6_triangulation,
    "T7_MULTI_VICTIM": t7_multi_victim,
    "T8_DORMANT_BURST": t8_dormant,
    "T9_DEVICE_CLUSTER": t9_device_cluster,
    "T10_NODAL_POOL": t10_nodal_pool,
}


def generate_rings(
    b: WorldBuilder,
    pop: Population,
    rng: np.random.Generator,
    n_rings: int,
    episode_p: list[float],
    hard: bool = False,
) -> tuple[RingLedger, list[Episode]]:
    """Ring orchestrator. ``hard``: add the hard-only typologies (``wapsi.synth.hard``) to the rotation and switch
    every template to hard mode (``RingContext.hard``)."""
    from wapsi.synth.hard import HARD_TEMPLATE_FNS

    led = RingLedger(b)
    episodes: list[Episode] = []
    used_victims: set[int] = set()
    typs = TYPOLOGIES + HARD_TYPOLOGIES if hard else TYPOLOGIES
    fns = {**TEMPLATE_FNS, **HARD_TEMPLATE_FNS} if hard else TEMPLATE_FNS
    typ_order = [typs[i % len(typs)] for i in range(n_rings)]
    rng.shuffle(typ_order)
    for i, typ in enumerate(typ_order):
        ring_id = f"RING-{i + 1:04d}"
        n_dev = 2 if typ == "T9_DEVICE_CLUSTER" else 1
        devices = [b.new_device_no() for _ in range(n_dev)]
        ctx = RingContext(
            b=b,
            led=led,
            pop=pop,
            rng=rng,
            ring_id=ring_id,
            typology=typ,
            devices=devices,
            used_victims=used_victims,
            hard=hard,
        )
        single = typ in ("T7_MULTI_VICTIM", "T12_VICTIM_CHAIN")
        n_ep = 1 if single else 1 + int(rng.choice(len(episode_p), p=episode_p))
        for _ in range(n_ep):
            day = int(rng.integers(3, b.days - 4))
            hour = float(rng.choice([9.5, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5, 18.5, 19.5, 20.5]))
            t0 = b.day_start(day) + int((hour + rng.uniform(-0.5, 0.5)) * HOUR)
            ep = Episode(ring_id=ring_id, typology=typ, t0=t0)
            ctx.dwell_left = 1 if hard else 0
            ep.t_end = fns[typ](ctx, ep)
            if ep.t_end - t0 > 60 * HOUR:
                raise AssertionError(f"{ring_id} trail longer than the trace window")
            episodes.append(ep)
    return led, episodes
