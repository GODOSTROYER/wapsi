"""Hard-world typologies T11..T15 (``--size hard``): realistic confounders for mule / innocent-holder classification.

The demo world is separable by single features (fresh accounts, instant pass-through, shared devices, loud external
signals). The hard world keeps the exact ground truth (the same event simulation in ``wapsi.synth.reference``) but
adds the cases a nodal officer actually struggles with:

* ``T11_SLEEPER_MULE`` - a long-standing account rented to a ring: either a salaried person's account (commingled
  salary) or a small trading business with real client receipts (commingled turnover, so the victim money is not
  large next to it). A partial cash-out inside the first day, then usually delayed layering (26-42 h dwell) past
  the 24 h feature window.
* ``T12_VICTIM_CHAIN`` - task-scam chain: part of victim A's money is paid to victim B as a "task reward"; B is
  then persuaded to pay a fresh mule (B's own complaint). On A's trail B looks like a fast pass-through mule but is
  a victim (sometimes a third victim C follows).
* ``T13_SALARY_POOL`` - a genuine labour contractor is paid a victim's money as an "advance" by a fake client
  (triangulation); it pays its workers and withdraws cash for daily wages within hours (fan-out, fast pass-through,
  cash exits); workers cash out part of their wages. Every account on this trail is an innocent holder.
* ``T14_FESTIVAL_SELLER`` - a young home business at its festival peak: dozens of new customers a day, late-night
  orders, suppliers paid the same day, often a false external risk flag. The fraudster's bulk order is paid by the
  victim. The seller and its suppliers are innocent holders.
* ``T15_INSTANT_RELAY`` - a scripted mule relays the victim's transfer in the *same second* (written so each debit
  has a lower ``txn_id`` than the credit funding it: the AGENTS.md credit-before-debit rule is required to replay
  it), in tranches structured just under Rs 50,000; relays cash out only part of the money.

Global hard-mode confounders applied to T1..T10 (``RingContext.hard``) live in ``wapsi.synth.rings``. Every tracked
non-terminal account is ring-controlled and quiet after its episode, so the reference balances at ``world_end``
equal the product's balances at the end of the trace window (holds stay exact).
"""

from __future__ import annotations

from datetime import date

import numpy as np

from wapsi.synth import names as nm
from wapsi.synth.builder import CH, DAY, HOUR, MINUTE, upi_or_imps
from wapsi.synth.rings import Episode, RingContext, VictimRecord, round_down

Event = tuple[int, int, int | None, int | None, float, int, str, str | None]

WAGE_REMARKS = ["WAGES", "WEEKLY WAGES", "wages", "LABOUR WAGES"]
CONTRACTOR_TRADES = [
    "Manpower Services",
    "Labour Contractors",
    "Facility Services",
    "Housekeeping Services",
    "Construction Works",
]
FESTIVE_GOODS = [
    "rakhi gift hampers",
    "sweet boxes",
    "diyas and decor sets",
    "ethnic wear",
    "dry-fruit gift boxes",
    "puja item kits",
]
TASK_REWARD_REMARKS = ["TASK REWARD", "task commission", "cashback", "task payout", "review bonus"]


def _mins(ctx: RingContext, lo: float, hi: float) -> int:
    return int(ctx.rng.uniform(lo, hi) * MINUTE)


def _people(ctx: RingContext) -> np.ndarray:
    """Background individuals that already existed at world start (safe counterparties at any time)."""
    opened = np.array(ctx.b.acct_opened, dtype=np.int64)
    return ctx.pop.individuals[opened[ctx.pop.individuals] < ctx.b.start_s]


def _emit(ctx: RingContext, events: list[Event]) -> None:
    """Write events in time order (stable), skipping debits of ring accounts that the balance cannot cover."""
    led = ctx.led
    for ts, _o, frm, to, amt, chan, rem, lab in sorted(events, key=lambda e: (e[0], e[1])):
        if frm is not None and frm in led.bal and led.balance(frm) < amt:
            continue
        led.tx(ts, frm, to, amt, chan, rem, lab)


def _person(ctx: RingContext, bank: str, opened: int, *, role: str, opening: float, kyc: str = "FULL") -> int:
    b, r = ctx.b, ctx.rng
    name, city, _ = b.person_name()
    cid = b.new_customer(name, city, kyc, b.random_dob(1966, 2002))
    idx = b.new_account(
        bank,
        "INDIVIDUAL",
        opened,
        cid,
        name=name,
        city=city,
        opening=round(float(opening), 2),
        role=role,
        dedicated=True,
        ring=ctx.ring_id,
        typology=ctx.typology,
    )
    ctx.led.track(idx)
    b.add_device(idx, opened + int(r.integers(0, 30)) * DAY)
    return idx


def _business(
    ctx: RingContext, bank: str, opened: int, *, name: str, role: str, opening: float, gst_p: float
) -> int:
    b, r = ctx.b, ctx.rng
    city = b.person_name()[1]
    gstin = None
    if r.random() < gst_p:
        gstin = b.new_gstin(name, "ACTIVE", max(b.ist_date(opened), date(2017, 7, 1)))
    cid = b.new_customer(name, city, "FULL", None)
    idx = b.new_account(
        bank,
        "BUSINESS",
        opened,
        cid,
        name=name,
        city=city,
        gstin=gstin,
        opening=round(float(opening), 2),
        role=role,
        dedicated=True,
        ring=ctx.ring_id,
        typology=ctx.typology,
    )
    ctx.led.track(idx)
    b.add_device(idx, opened + int(r.integers(0, 30)) * DAY)
    return idx


def _business_events(ctx: RingContext, acct: int, t_hi: int, n_payers: int) -> list[Event]:
    """Ordinary trade before ``t_hi``: payments from other businesses / customers, stock bought from merchants."""
    b, r, pop = ctx.b, ctx.rng, ctx.pop
    lo = max(b.acct_opened[acct], b.start_s) + HOUR
    ev: list[Event] = []
    if t_hi - lo < DAY:
        return ev
    payers = [
        int(x) for x in r.choice(pop.businesses, size=min(n_payers, pop.businesses.size), replace=False)
    ]
    for p in payers:
        for _ in range(int(r.integers(1, 3))):
            amt = round(float(np.exp(r.normal(np.log(22_000), 0.7))), 2)
            ev.append((int(r.integers(lo, t_hi)), len(ev), p, acct, amt, CH["NEFT"], "INVOICE PAYMENT", None))
    for _ in range(int(r.integers(1, 4))):
        m = int(r.choice(pop.merchants))
        amt = round(float(np.exp(r.normal(np.log(6_000), 0.6))), 2)
        ev.append((int(r.integers(lo, t_hi)), len(ev), acct, m, amt, CH["IMPS"], "stock purchase", None))
    return ev


def _clean_signal(ctx: RingContext, idx: int, false_alarm_p: float) -> None:
    ctx.b.signal_override[idx] = ctx.noisy_clean_signal(false_alarm_p)
    ctx.maybe_missing_signal(idx)


# --------------------------------------------------------------------------- T11
def t11_sleeper_mule(ctx: RingContext, ep: Episode) -> int:
    b, r, led = ctx.b, ctx.rng, ctx.led
    opened = ep.t0 - int(r.uniform(2.0, 9.0) * 365 * DAY)
    business = r.random() < 0.5
    if business:  # a trading business rented out: real client receipts commingled with the ring's money
        s = _business(
            ctx,
            b.home,
            opened,
            name=b.business_name(),
            role="MULE",
            opening=r.uniform(20_000, 120_000),
            gst_p=0.6,
        )
        _emit(ctx, _business_events(ctx, s, ep.t0 - 2 * HOUR, int(r.integers(3, 8))))
    else:
        s = _person(ctx, b.home, opened, role="MULE", opening=r.uniform(4_000, 45_000))
        salary = float(round(np.exp(r.normal(np.log(28_000), 0.4)), -2)) if r.random() < 0.8 else None
        ctx.legit_history(s, ep.t0 - 2 * HOUR, salary=salary, n_payers=int(r.integers(2, 7)))
    _clean_signal(ctx, s, false_alarm_p=0.1)
    v = ctx.pick_victim()
    amount = ctx.victim_amount()
    t = ctx.seed(ep, v, s, ep.t0, amount)
    if r.random() < 0.55:  # partial cash-out inside the 24 h feature window
        t = ctx.cash_out(s, t + _mins(ctx, 30, 300), amount * r.uniform(0.08, 0.3))
    if business and r.random() < 0.4:  # some rented business accounts pass the money on within hours
        t_fwd = t + int(r.uniform(1.0, 5.0) * HOUR)
    else:  # delayed layering: the rest leaves a day or two later
        t_fwd = t + int(r.uniform(26.0, 42.0) * HOUR)
    subs = [ctx.new_mule(ctx.mule_bank(0.35), t_fwd) for _ in range(int(r.integers(1, 4)))]
    total = min(led.balance(s), amount) * r.uniform(0.5, 0.85)
    times = ctx.split_forward(s, subs, t_fwd, total, step=500)
    end = max([t_fwd, *times])
    for sub, ts in zip(subs, times, strict=True):
        if ts < 0:
            continue
        u = r.random()
        if u < 0.5:
            end = max(
                end, ctx.cash_out(sub, ts + _mins(ctx, 10, 60), led.balance(sub) * r.uniform(0.4, 0.95))
            )
        elif u < 0.75:
            end = max(
                end, ctx.crypto_out(sub, ts + _mins(ctx, 10, 60), led.balance(sub) * r.uniform(0.5, 0.95))
            )
    return end


# --------------------------------------------------------------------------- T12
def _chain_victim(ctx: RingContext, t0: int) -> int:
    """A long-standing account of a person who will become the next victim of the chain (modest savings)."""
    b, r = ctx.b, ctx.rng
    bank = b.home if r.random() < 0.45 else ctx.other_bank()
    opened = t0 - int(r.uniform(1.5, 12.0) * 365 * DAY)
    kyc = str(r.choice(["FULL", "VKYC"], p=[0.85, 0.15]))
    v = _person(ctx, bank, opened, role="VICTIM", opening=r.uniform(2_000, 12_000), kyc=kyc)
    salary = float(round(r.uniform(9_000, 22_000), -2)) if r.random() < 0.5 else None
    ctx.legit_history(v, t0 - 2 * HOUR, salary=salary, n_payers=int(r.integers(1, 5)))
    _clean_signal(ctx, v, false_alarm_p=0.05)
    return v


def t12_victim_chain(ctx: RingContext, ep: Episode) -> int:
    b, r, led = ctx.b, ctx.rng, ctx.led
    m = ctx.new_mule(b.home, ep.t0)
    va = ctx.pick_victim()
    amount = max(ctx.victim_amount(), float(round(r.uniform(40_000, 90_000), -3)))
    t = ctx.seed(ep, va, m, ep.t0, amount, category="TASK_SCAM")
    n_links = 1 if r.random() < 0.7 else 2
    firsts = [m]
    src = m
    for _ in range(n_links):
        vb = _chain_victim(ctx, ep.t0)
        t += _mins(ctx, 20, 150)
        reward = min(60_000.0, led.balance(src) * r.uniform(0.3, 0.45))
        reward = round_down(reward, 10) - float(r.choice([0, 1, 10]))
        _, t = led.tx(t, src, vb, reward, upi_or_imps(reward, r, 0.7), str(r.choice(TASK_REWARD_REMARKS)))
        t += _mins(ctx, 15, 180)
        nxt = ctx.new_mule(b.home, t)
        pay = round_down(led.balance(vb) * r.uniform(0.7, 0.95), 100)
        t = ctx.seed(ep, vb, nxt, t, pay, category="TASK_SCAM", split_p=0.0)
        firsts.append(nxt)
        src = nxt
    end = t
    for i, mu in enumerate(firsts):
        tt = end + _mins(ctx, 10, 90)
        deepest = n_links == 2 and i == len(firsts) - 1  # layer 5 on the first victim's trail: exits only
        u = r.random()
        if not deepest and u < 0.35:
            x = ctx.new_mule(ctx.mule_bank(0.4), tt)
            tt = ctx.forward(mu, x, tt, led.balance(mu) * r.uniform(0.5, 0.9), 1000)
            tt = ctx.cash_out(x, tt + _mins(ctx, 10, 60), led.balance(x) * r.uniform(0.3, 0.9))
        elif u < 0.75:
            tt = ctx.cash_out(mu, tt, led.balance(mu) * r.uniform(0.4, 0.9))
        else:
            tt = ctx.crypto_out(mu, tt, led.balance(mu) * r.uniform(0.4, 0.9))
        end = max(end, tt)
    return end


# --------------------------------------------------------------------------- T13
def t13_salary_pool(ctx: RingContext, ep: Episode) -> int:
    b, r, led, pop = ctx.b, ctx.rng, ctx.led, ctx.pop
    t0 = ep.t0
    cname = (
        f"{nm.BIZ_STEMS[int(r.integers(len(nm.BIZ_STEMS)))]} "
        f"{CONTRACTOR_TRADES[int(r.integers(len(CONTRACTOR_TRADES)))]}"
    )
    c = _business(
        ctx,
        b.home,
        t0 - int(r.uniform(1.5, 9.0) * 365 * DAY),
        name=cname,
        role="INNOCENT_HOLDER",
        opening=r.uniform(25_000, 90_000),
        gst_p=0.5,
    )
    ctx.innocent_signal(c)

    def worker(opened: int) -> int:
        bank = b.home if r.random() < 0.75 else ctx.other_bank()
        kyc = str(r.choice(["FULL", "VKYC", "MIN"], p=[0.7, 0.2, 0.1]))
        w = _person(ctx, bank, opened, role="INNOCENT_HOLDER", opening=r.uniform(300, 6_000), kyc=kyc)
        ctx.innocent_signal(w)
        return w

    workers = [worker(t0 - int(r.uniform(1.0, 10.0) * 365 * DAY)) for _ in range(int(r.integers(3, 7)))]
    wage = {w: float(round(r.uniform(2_800, 6_500), -2)) for w in workers}
    weekly = sum(wage.values())
    clients = [int(x) for x in r.choice(pop.businesses, size=int(r.integers(2, 6)), replace=False)]
    people = _people(ctx)
    landlord = int(people[int(r.integers(people.size))])
    office_rent = float(round(r.uniform(8_000, 25_000), -2))
    hi = t0 - 2 * HOUR
    ev: list[Event] = []

    def add(ts: int, frm: int | None, to: int | None, amt: float, chan: int, rem: str, lab=None) -> None:
        if b.start_s < ts < hi:
            ev.append((int(ts), len(ev), frm, to, round(float(amt), 2), chan, rem, lab))

    for d in range(b.days):
        day = b.day_start(d)
        if day >= hi:
            break
        if d % 7 == 0:  # Saturdays (the world starts on a Saturday): weekly wages
            ts = day + int(r.uniform(17, 20) * HOUR)
            for j, w in enumerate(workers):
                add(ts + 60 * j, c, w, wage[w], CH["UPI"], f"{r.choice(WAGE_REMARKS)} WK{d // 7 + 1}")
        if d % 7 in (2, 4) and r.random() < 0.7:  # client bills paid on weekdays
            cl = clients[int(r.integers(len(clients)))]
            add(
                day + int(r.uniform(11, 16) * HOUR),
                cl,
                c,
                weekly * r.uniform(0.9, 1.7),
                CH["NEFT"],
                str(r.choice(["MANPOWER BILL", "INVOICE PAYMENT", "LABOUR CHARGES"])),
            )
        if b.ist_date(day).day == 5:
            add(day + int(r.uniform(10, 18) * HOUR), c, landlord, office_rent, CH["NEFT"], "OFFICE RENT")
    for w in workers:  # the workers' own lives (small spends, cash)
        own = ctx.history_events(w, hi, n_payers=int(r.integers(0, 3)))
        base = len(ev)
        ev.extend((e[0], base + k, *e[2:]) for k, e in enumerate(own))
    _emit(ctx, ev)

    # the fraud: a fake client has the victim pay the contractor a labour "advance"
    v = ctx.pick_victim()
    advance = float(round(r.uniform(120_000, 400_000), -3)) - float(r.choice([0, 0, 1]))
    t = ctx.seed(ep, v, c, t0, advance, split_p=0.0, remark=str(r.choice(["ADVANCE", "LABOUR ADVANCE", ""])))
    t += _mins(ctx, 40, 240)
    new_workers = [worker(t0 - int(r.uniform(1.0, 8.0) * 365 * DAY)) for _ in range(int(r.integers(0, 3)))]
    for w in new_workers:
        ctx.legit_history(w, t - 2 * HOUR, n_payers=int(r.integers(1, 3)))
        wage[w] = float(round(r.uniform(2_800, 6_500), -2))
    for w in workers + new_workers:  # advance wages: one to two weeks
        amt = wage[w] * float(r.choice([1.0, 1.5, 2.0]))
        if led.balance(c) < amt:
            break
        t += int(r.uniform(0.5, 4) * MINUTE)
        _, t = led.tx(t, c, w, amt, CH["UPI"] if amt <= 100_000 else CH["IMPS"], "ADVANCE WAGES")
    cash = min(led.balance(c), advance) * r.uniform(0.05, 0.3)
    end = ctx.cash_out(c, t + _mins(ctx, 10, 60), cash) if cash >= 2_000 else t
    base = end
    for w in workers + new_workers:  # daily-wage workers often withdraw most of it in cash
        if r.random() < 0.25 and led.balance(w) >= 1_000:
            amt = round_down(led.balance(w) * r.uniform(0.25, 0.8), 500)
            if amt >= 500:
                _, tt = led.tx(
                    base + _mins(ctx, 20, 600), w, None, amt, CH["ATM"], "ATM WDL", ctx.atm_label(w)
                )
                end = max(end, tt)
    return end


# --------------------------------------------------------------------------- T14
def t14_festival_seller(ctx: RingContext, ep: Episode) -> int:
    b, r, led = ctx.b, ctx.rng, ctx.led
    t0 = ep.t0
    goods = FESTIVE_GOODS[int(r.integers(len(FESTIVE_GOODS)))]
    opened = t0 - int(r.uniform(45, 400) * DAY)
    if r.random() < 0.6:  # a home business run from a personal account
        kyc = str(r.choice(["FULL", "VKYC", "MIN"], p=[0.5, 0.35, 0.15]))
        f = _person(ctx, b.home, opened, role="INNOCENT_HOLDER", opening=r.uniform(1_500, 20_000), kyc=kyc)
    else:
        f = _business(
            ctx,
            b.home,
            opened,
            name=b.business_name(),
            role="INNOCENT_HOLDER",
            opening=r.uniform(3_000, 30_000),
            gst_p=0.3,
        )
    _clean_signal(ctx, f, false_alarm_p=0.35)

    def supplier(bank: str) -> int:
        s = _business(
            ctx,
            bank,
            t0 - int(r.uniform(2, 15) * 365 * DAY),
            name=b.business_name(),
            role="INNOCENT_HOLDER",
            opening=r.uniform(50_000, 300_000),
            gst_p=0.8,
        )
        ctx.innocent_signal(s)
        return s

    known = [supplier(b.home if r.random() < 0.6 else ctx.other_bank()) for _ in range(int(r.integers(1, 3)))]
    fresh = supplier(b.home if r.random() < 0.5 else ctx.other_bank())
    people = _people(ctx)
    hi = t0 - 2 * HOUR
    ev: list[Event] = []
    for s in [*known, fresh]:
        ev.extend(_business_events(ctx, s, hi, int(r.integers(3, 8))))

    def orders(day_lo: int, day_hi: int, lam: float, night_p: float) -> None:
        for d in range(day_lo, day_hi):
            day = b.day_start(d)
            for _ in range(int(r.poisson(lam))):
                hour = r.uniform(21.0, 24.5) if r.random() < night_p else r.uniform(10.0, 21.0)
                ts = day + int(hour * HOUR)
                amt = round_down(float(np.clip(np.exp(r.normal(np.log(700), 0.7)), 120, 6_000)), 10)
                who = int(people[int(r.integers(people.size))])
                ev.append(
                    (
                        ts,
                        len(ev),
                        who,
                        f,
                        amt,
                        CH["UPI"],
                        str(r.choice(["order", goods[:18], "", "payment"])),
                        None,
                    )
                )

    t0_day = (t0 - b.start_s) // DAY
    orders(0, max(0, t0_day - 3), 1.2, 0.1)
    orders(max(0, t0_day - 3), t0_day + 1, 11.0, 0.3)
    ev = [e for e in ev if e[0] < hi]
    for d in range(1, t0_day):  # weekly stock bills to the usual suppliers (Mondays)
        if d % 7 == 2:
            for s in known:
                ev.append(
                    (
                        b.day_start(d) + int(r.uniform(11, 15) * HOUR),
                        len(ev),
                        f,
                        s,
                        float(round(r.uniform(2_000, 9_000), -1)),
                        CH["UPI"],
                        "stock",
                        None,
                    )
                )
    _emit(ctx, ev)

    v = ctx.pick_victim()
    price = float(round(r.uniform(40_000, 250_000), -2)) - float(r.choice([0, 0, 1]))
    item = f"{goods} (bulk)"
    t = ctx.seed(
        ep,
        v,
        f,
        t0,
        price,
        category="ONLINE_SHOPPING",
        item=item,
        split_p=0.0,
        remark=f"BULK ORDER {goods[:14].upper()}",
    )
    # the festival rush continues through the day after the payment (fan-in, night orders)
    ev = []
    orders(t0_day, t0_day + 2, 12.0, 0.3)
    ev = [e for e in ev if t < e[0] < t + 22 * HOUR]
    t_pay = t + _mins(ctx, 60, 480)
    share = r.uniform(0.55, 0.95)
    cut = r.dirichlet(np.full(len(known) + 1, 2.0))
    for s, w in zip([*known, fresh], cut, strict=True):
        ev.append(
            (
                t_pay,
                len(ev),
                f,
                s,
                round_down(price * share * float(w), 10),
                CH["IMPS"],
                "STOCK PAYMENT",
                None,
            )
        )
        t_pay += _mins(ctx, 2, 30)
    if r.random() < 0.5:
        ev.append(
            (
                t_pay + _mins(ctx, 10, 120),
                len(ev),
                f,
                None,
                float(r.choice([5_000, 10_000, 20_000])),
                CH["ATM"],
                "ATM WDL",
                ctx.atm_label(f),
            )
        )
    _emit(ctx, ev)
    return max([t, *(led.last.get(a, t) for a in (f, *known, fresh))])


# --------------------------------------------------------------------------- T15
def t15_instant_relay(ctx: RingContext, ep: Episode) -> int:
    b, r, led = ctx.b, ctx.rng, ctx.led
    r0 = ctx.new_mule(b.home, ep.t0)
    v = ctx.pick_victim()
    amount = max(ctx.victim_amount(), float(round(r.uniform(90_000, 220_000), -3)))

    def tranche() -> float:
        return float(49_000 + int(r.integers(1, 1000)))  # just under the Rs 50,000 threshold

    r1 = ctx.new_mule(ctx.mule_bank(0.3), ep.t0)
    t1 = tranche()
    legs: list[tuple[int | None, int | None, float, int, str]] = [
        (
            v,
            r0,
            amount,
            upi_or_imps(amount, r, 0.3),
            str(r.choice(["", "urgent", "verification", "deposit"])),
        ),
        (r0, r1, t1, CH["IMPS"], ""),
    ]
    tail = [r1]
    if r.random() < 0.5:  # the relay's relay fires in the same second too
        r1b = ctx.new_mule(ctx.mule_bank(0.3), ep.t0)
        legs.append((r1, r1b, float(round(t1 * r.uniform(0.5, 0.9), 2)), CH["IMPS"], ""))
        tail.append(r1b)
    idx, t = led.relay(ep.t0, legs)
    ep.victims.append(VictimRecord(victim=v, seeds=[idx[0]], first_layer=[r0], fir_p=0.35))
    relays = list(tail)
    keep = r.uniform(0.1, 0.4)
    while led.balance(r0) - 49_999 >= amount * keep and len(relays) < 5:
        t += _mins(ctx, 1, 5)
        x = ctx.new_mule(ctx.mule_bank(0.35), t)
        _, t = led.tx(t, r0, x, tranche(), CH["IMPS"], "")
        relays.append(x)
    end = t
    for x in relays:
        u = r.random()
        if u < 0.6:
            end = max(end, ctx.cash_out(x, t + _mins(ctx, 5, 90), led.balance(x) * r.uniform(0.3, 0.8)))
        elif u < 0.8:
            end = max(end, ctx.crypto_out(x, t + _mins(ctx, 5, 90), led.balance(x) * r.uniform(0.4, 0.9)))
    return end


HARD_TEMPLATE_FNS = {
    "T11_SLEEPER_MULE": t11_sleeper_mule,
    "T12_VICTIM_CHAIN": t12_victim_chain,
    "T13_SALARY_POOL": t13_salary_pool,
    "T14_FESTIVAL_SELLER": t14_festival_seller,
    "T15_INSTANT_RELAY": t15_instant_relay,
}
