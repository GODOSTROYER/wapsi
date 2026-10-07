"""Ring plumbing: a balance-checked ledger for ring-controlled accounts plus mule / victim / exit helpers.

Ring-controlled ("dedicated") accounts never receive background traffic, so every rupee that moves through them is
written here and every debit is checked against the running balance at that moment.

Hard mode (``RingContext.hard``, world size ``hard``) adds classification confounders to every template: older
mules with a legitimate in-world history, fewer shared devices, noisy / missing external signals, structured
(just-under-threshold, non-round) transfers, partial cash-outs and one delayed-layering hop per episode. Every
hard-mode branch is gated so the non-hard worlds consume exactly the same random numbers as before.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from wapsi.synth import names as nm
from wapsi.synth.builder import CH, DAY, HOUR, MINUTE, TxnChunk, WorldBuilder, upi_or_imps
from wapsi.synth.population import Population

TYPOLOGIES = [
    "T1_FANOUT_CASHOUT",
    "T2_CHAIN_LAYERING",
    "T3_MERCHANT_WASH",
    "T4_CRYPTO_EXIT",
    "T5_CROSS_BANK_HOP",
    "T6_TRIANGULATION",
    "T7_MULTI_VICTIM",
    "T8_DORMANT_BURST",
    "T9_DEVICE_CLUSTER",
    "T10_NODAL_POOL",
]
# typologies that only appear in the hard world (wapsi.synth.hard)
HARD_TYPOLOGIES = [
    "T11_SLEEPER_MULE",
    "T12_VICTIM_CHAIN",
    "T13_SALARY_POOL",
    "T14_FESTIVAL_SELLER",
    "T15_INSTANT_RELAY",
]

# hard-mode knobs (synthetic difficulty settings, not estimates from real data)
HARD_OLDER_P = 0.5  # share of mules opened long before use (most get a legitimate in-world history)
HARD_HISTORY_P = 0.7  # older mules with a legitimate history (salary, friends, bills)
HARD_SHARE_DEVICE = 0.3  # cap on the probability that a mule shares the ring's device
HARD_SIGNAL_FLIP = 0.35  # "high" external-signal mules that look clean
HARD_SIGNAL_MISSING = 0.3  # ring / innocent accounts without any external-signal row
HARD_STRUCTURE_P = 0.5  # transfers structured just under a threshold or made non-round
HARD_PARTIAL_CASH_P = 0.4  # cash-outs that take only part of the money
HARD_DWELL_P = 0.55  # probability that an episode's first eligible hop dwells 26-44 h
HARD_DWELL_HOURS = (26.0, 44.0)
HARD_FALSE_ALARM_P = 0.12  # genuine (innocent / victim) accounts with a false external risk flag


def round_down(x: float, step: float) -> float:
    return float(math.floor(x / step + 1e-9) * step)


def _ev_key(e: tuple) -> tuple[int, int]:
    return e[0], e[1]


class RingLedger:
    """Collects ring transactions (always chunk 0 of the world) and checks balances of dedicated accounts."""

    def __init__(self, b: WorldBuilder) -> None:
        self.b = b
        self.ts: list[int] = []
        self.frm: list[int] = []
        self.to: list[int] = []
        self.amt: list[float] = []
        self.chan: list[int] = []
        self.remark: list[int] = []
        self.label: list[int] = []
        self.bal: dict[int, float] = {}
        self.last: dict[int, int] = {}

    def track(self, acct: int) -> None:
        self.bal[acct] = self.b.acct_opening[acct]
        self.last[acct] = self.b.acct_opened[acct]

    def balance(self, acct: int) -> float:
        return round(self.bal[acct], 2)

    def tx(
        self,
        ts: int,
        frm: int | None,
        to: int | None,
        amount: float,
        chan: int,
        remark: str = "",
        label: str | None = None,
    ) -> tuple[int, int]:
        """Append a transaction; returns (ring txn index, actual ts). Timestamps of dedicated accounts are
        strictly increasing (bumped by a minute when needed)."""
        amount = round(float(amount), 2)
        if amount <= 0:
            raise ValueError("non-positive amount")
        if frm is not None and frm == to:
            raise ValueError("self transfer")
        for a in (frm, to):
            if a is not None and a in self.last and ts <= self.last[a]:
                ts = self.last[a] + MINUTE + int(self.b.rng.integers(0, 50))
        if frm is not None and frm in self.bal:
            if self.bal[frm] + 1e-6 < amount:
                raise AssertionError(
                    f"ring debit exceeds balance: {self.b.acct_ids[frm]} {self.bal[frm]} < {amount}"
                )
            self.bal[frm] = round(self.bal[frm] - amount, 2)
            self.last[frm] = ts
        if to is not None and to in self.bal:
            self.bal[to] = round(self.bal[to] + amount, 2)
            self.last[to] = ts
        self.ts.append(int(ts))
        self.frm.append(-1 if frm is None else frm)
        self.to.append(-1 if to is None else to)
        self.amt.append(amount)
        self.chan.append(chan)
        self.remark.append(self.b.text.code(remark))
        self.label.append(-1 if label is None else self.b.text.code(label))
        return len(self.ts) - 1, int(ts)

    def relay(
        self, ts: int, legs: list[tuple[int | None, int | None, float, int, str]]
    ) -> tuple[list[int], int]:
        """Same-second relay (ordering stress): every leg ``(frm, to, amount, chan, remark)`` happens in the same
        second and each leg is funded by the one before it. Balances are checked in that logical order, but the legs
        are written in *reverse*, so each debit gets a lower ``txn_id`` than the credit that funds it. Replaying by
        ``(ts, txn_id)`` alone would overdraw; the AGENTS.md rule (a same-second credit is applied before the debits
        it funds) is required. Returns (ring txn indices in logical order, the common ts)."""
        accts = {a for f, t, *_ in legs for a in (f, t) if a is not None and a in self.last}
        for a in accts:
            if ts <= self.last[a]:
                ts = self.last[a] + MINUTE + int(self.b.rng.integers(0, 50))
        bal = dict(self.bal)
        for frm, to, amount, _chan, _rem in legs:
            amount = round(float(amount), 2)
            if amount <= 0 or (frm is not None and frm == to):
                raise ValueError("bad relay leg")
            if frm is not None and frm in bal:
                if bal[frm] + 1e-6 < amount:
                    raise AssertionError(f"relay debit exceeds balance: {self.b.acct_ids[frm]}")
                bal[frm] = round(bal[frm] - amount, 2)
            if to is not None and to in bal:
                bal[to] = round(bal[to] + amount, 2)
        self.bal = bal
        for a in accts:
            self.last[a] = ts
        idx = [0] * len(legs)
        for i in range(len(legs) - 1, -1, -1):
            frm, to, amount, chan, rem = legs[i]
            self.ts.append(int(ts))
            self.frm.append(-1 if frm is None else frm)
            self.to.append(-1 if to is None else to)
            self.amt.append(round(float(amount), 2))
            self.chan.append(chan)
            self.remark.append(self.b.text.code(rem))
            self.label.append(-1)
            idx[i] = len(self.ts) - 1
        return idx, int(ts)

    def chunk(self) -> TxnChunk:
        return TxnChunk(
            ts=np.array(self.ts, dtype=np.int64),
            frm=np.array(self.frm, dtype=np.int32),
            to=np.array(self.to, dtype=np.int32),
            amt=np.array(self.amt, dtype=np.float64),
            chan=np.array(self.chan, dtype=np.int8),
            remark=np.array(self.remark, dtype=np.int32),
            label=np.array(self.label, dtype=np.int32),
        )


@dataclass
class VictimRecord:
    victim: int
    seeds: list[int]  # ring txn indices
    first_layer: list[int]
    category: str = ""
    item: str = ""
    fir_p: float = 0.35


@dataclass
class Episode:
    ring_id: str
    typology: str
    t0: int
    victims: list[VictimRecord] = field(default_factory=list)
    t_end: int = 0


@dataclass
class RingContext:
    b: WorldBuilder
    led: RingLedger
    pop: Population
    rng: np.random.Generator
    ring_id: str
    typology: str
    devices: list[int]
    used_victims: set[int]
    used_merchants: dict[int, int] = field(default_factory=dict)
    hard: bool = False
    dwell_left: int = 0  # hard mode: delayed-layering hops still allowed in the current episode

    # ------------------------------------------------------------ accounts
    def other_bank(self) -> str:
        return "OB03" if self.rng.random() < 0.55 else "OB02"

    def mule_bank(self, p_other: float) -> str:
        return self.other_bank() if self.rng.random() < p_other else self.b.home

    def maybe_missing_signal(self, idx: int) -> None:
        """Hard mode: some accounts have no external-signal row at all (patchy FRI / MuleHunter coverage)."""
        if self.hard and self.rng.random() < HARD_SIGNAL_MISSING:
            self.b.signal_missing.add(idx)

    def mule_signal(self, level: str) -> tuple[str, float, int]:
        r = self.rng
        if level == "high" and r.random() < (HARD_SIGNAL_FLIP if self.hard else 0.15):
            level = "low"  # noise: some mules look clean
        if level == "high":
            fri = str(r.choice(["VERY_HIGH", "HIGH", "MEDIUM", "NONE"], p=[0.25, 0.37, 0.25, 0.13]))
            score = float(np.clip(r.beta(6.0, 2.2), 0.05, 0.99))
            prior = int(min(r.poisson(0.7), 4))
        else:
            fri = str(r.choice(["NONE", "MEDIUM", "HIGH"], p=[0.55, 0.35, 0.10]))
            score = float(np.clip(r.beta(2.5, 4.0), 0.02, 0.9))
            prior = int(min(r.poisson(0.2), 2))
        return fri, round(score, 3), prior

    def new_mule(
        self,
        bank: str,
        use_s: int,
        *,
        level: str = "high",
        older_p: float = 0.15,
        share_device: float = 0.75,
        kyc: str | None = None,
        clean: bool = False,
    ) -> int:
        """Fresh mule opened 3-25 days before ``use_s`` (15% older). ``clean`` keeps the balance a multiple of
        100 (opening 0, round cash deposits only) so ATM sweeps can empty it exactly."""
        b, r = self.b, self.rng
        if self.hard:
            older_p = max(older_p, HARD_OLDER_P)
            share_device = min(share_device, HARD_SHARE_DEVICE)
        older = r.random() < older_p
        if older:
            opened = use_s - int(r.integers(60, 1500)) * DAY - int(r.integers(0, 8 * HOUR))
        else:
            opened = use_s - int(r.uniform(3, 25) * DAY)
        if kyc is None:
            p_new = [0.3, 0.35, 0.35] if self.hard else [0.45, 0.4, 0.15]
            kyc = str(r.choice(["MIN", "VKYC", "FULL"], p=[0.2, 0.2, 0.6] if older else p_new))
        name, city, _ = b.person_name()
        cid = b.new_customer(name, city, kyc, b.random_dob(1975, 2006))
        opening = 0.0
        if opened < b.start_s:
            opening = float(np.round(r.uniform(500, 15000) if older else r.uniform(0, 3000), 2))
            if r.random() < 0.35 or clean:
                opening = 0.0
        idx = b.new_account(
            bank,
            "INDIVIDUAL",
            opened,
            cid,
            name=name,
            city=city,
            opening=opening,
            role="MULE",
            dedicated=True,
            ring=self.ring_id,
            typology=self.typology,
        )
        self.led.track(idx)
        # pre-history
        lo = max(opened, b.start_s) + HOUR
        hi = use_s - 2 * HOUR
        if self.hard and older and not clean and r.random() < HARD_HISTORY_P:
            # hard mode: a rented / sleeper account with an ordinary in-world life before the fraud
            self.legit_history(
                idx,
                hi,
                salary=float(round(np.exp(r.normal(np.log(24_000), 0.45)), -2)) if r.random() < 0.6 else None,
                n_payers=int(r.integers(1, 6)),
            )
        elif hi > lo and r.random() < 0.6:
            t = int(r.integers(lo, hi))
            self.led.tx(
                t, None, idx, round_down(r.uniform(500, 10000), 100), CH["CASH_DEPOSIT"], "cash deposit"
            )
            if not clean and r.random() < 0.4 and self.led.balance(idx) > 400 and hi > t + HOUR:
                biller = int(r.choice(self.pop.billers))
                amt = round(float(r.uniform(149, min(399, self.led.balance(idx) - 1))), 0)
                self.led.tx(int(r.integers(t + 60, hi)), idx, biller, amt, CH["UPI"], "MOBILE RECHARGE")
        b.signal_override[idx] = self.mule_signal(level)
        self.maybe_missing_signal(idx)
        b.add_device(idx, opened + int(r.integers(0, 2 * HOUR)))
        if self.devices and r.random() < share_device:
            for d in self.devices[: 1 + int(r.random() < 0.3)]:
                b.add_device(idx, max(opened, use_s - int(r.uniform(0.5, 3) * DAY)), device=d)
        return idx

    def innocent_signal(self, idx: int, medium_p: float = 0.3) -> None:
        r = self.rng
        if self.hard:
            self.b.signal_override[idx] = self.noisy_clean_signal(HARD_FALSE_ALARM_P)
            self.maybe_missing_signal(idx)
            return
        if r.random() < medium_p:
            sig = (str(r.choice(["NONE", "MEDIUM"], p=[0.6, 0.4])), round(float(r.uniform(0.3, 0.55)), 3), 0)
        else:
            sig = ("NONE", round(float(np.clip(r.beta(1.5, 12.0), 0.0, 0.3)), 3), 0)
        self.b.signal_override[idx] = sig

    def noisy_clean_signal(self, false_alarm_p: float = HARD_FALSE_ALARM_P) -> tuple[str, float, int]:
        """External signals of a genuine account in the hard world: usually quiet, sometimes a false alarm (recycled
        mobile number on the FRI list, velocity-driven MuleHunter score, a customer dispute filed on NCRP)."""
        r = self.rng
        u = r.random()
        if u < false_alarm_p:
            fri = str(r.choice(["MEDIUM", "HIGH", "VERY_HIGH"], p=[0.55, 0.35, 0.10]))
            prior = int(r.choice([0, 1, 2], p=[0.6, 0.28, 0.12]))
            return fri, round(float(r.uniform(0.4, 0.8)), 3), prior
        if u < false_alarm_p + 0.3:
            fri = str(r.choice(["NONE", "MEDIUM"], p=[0.6, 0.4]))
            return fri, round(float(r.uniform(0.2, 0.5)), 3), int(r.random() < 0.08)
        return "NONE", round(float(np.clip(r.beta(1.5, 12.0), 0.0, 0.3)), 3), 0

    # ------------------------------------------------------------ hard-mode helpers
    def hop_delay(self, lo_min: float, hi_min: float) -> int:
        """Seconds before an account forwards what it received. In hard mode one hop per episode may dwell
        26-44 h (delayed layering past the 24 h feature window, still inside the 72 h trace window)."""
        d = int(self.rng.uniform(lo_min, hi_min) * MINUTE)
        if self.hard and self.dwell_left > 0 and self.rng.random() < HARD_DWELL_P:
            self.dwell_left -= 1
            d = int(self.rng.uniform(*HARD_DWELL_HOURS) * HOUR)
        return d

    def structure(self, amount: float) -> float:
        """Hard mode: an amount at most ``amount`` that sits just under a reporting / channel threshold
        (Rs 50,000 PAN cash limit, Rs 1 lakh UPI limit) or is simply non-round."""
        r = self.rng
        if 50_000 <= amount < 62_000:
            return float(49_000 + int(r.integers(500, 1000)))
        if 100_000 <= amount < 125_000:
            return float(99_000 + int(r.integers(500, 1000)))
        return float(round(amount - int(r.integers(11, 500)), 2))

    def legit_history(self, acct: int, t_hi: int, **kw) -> None:
        """Write ``history_events`` in time order, balance-checked (debits the balance cannot cover are skipped)."""
        led = self.led
        for ts, _o, frm, to, amt, chan, rem, lab in sorted(
            self.history_events(acct, t_hi, **kw), key=_ev_key
        ):
            if frm == acct and led.balance(acct) < amt:
                continue
            led.tx(ts, frm, to, amt, chan, rem, lab)

    def history_events(
        self,
        acct: int,
        t_hi: int,
        *,
        salary: float | None = None,
        employer: int | None = None,
        n_payers: int = 3,
        spend: bool = True,
        salary_remark: str = "SALARY",
    ) -> list[tuple[int, int, int | None, int | None, float, int, str, str | None]]:
        """Ordinary in-world life of a dedicated account before ``t_hi``, as events
        ``(ts, order, frm, to, amount, channel, remark, label)``: monthly salary (1st / 7th / month end), credits
        from ``n_payers`` friends / family, bills, small UPI / card spends and ATM cash. Counterparties are
        background accounts that existed at world start."""
        b, r, pop = self.b, self.rng, self.pop
        ev: list[tuple[int, int, int | None, int | None, float, int, str, str | None]] = []
        lo = max(b.acct_opened[acct], b.start_s) + HOUR
        if t_hi - lo < DAY:
            return ev
        opened = np.array(b.acct_opened, dtype=np.int64)
        people = pop.individuals[opened[pop.individuals] < b.start_s]
        shops = pop.merchants[opened[pop.merchants] < b.start_s]

        def add(ts: int, frm: int | None, to: int | None, amt: float, chan: int, rem: str, lab=None) -> None:
            if lo < ts < t_hi and amt > 0:
                ev.append((int(ts), len(ev), frm, to, round(float(amt), 2), chan, rem, lab))

        if salary:
            emp = (
                employer
                if employer is not None
                else int(pop.businesses[int(r.integers(pop.businesses.size))])
            )
            rule = int(r.integers(3))
            short = " ".join(b.acct_name[emp].upper().split()[:2])
            for d in range(b.days):
                day = b.day_start(d)
                date, nxt = b.ist_date(day), b.ist_date(day + DAY)
                if not (
                    (rule == 0 and date.day == 1)
                    or (rule == 1 and date.day == 7)
                    or (rule == 2 and nxt.month != date.month)
                ):
                    continue
                paid_for = (date if rule == 2 else b.ist_date(day - 10 * DAY)).strftime("%b").upper()
                chan = CH["RTGS"] if salary >= 200_000 else CH["NEFT"]
                add(
                    day + int(r.uniform(9.5, 13) * HOUR),
                    emp,
                    acct,
                    salary,
                    chan,
                    f"{salary_remark} {short} {paid_for}{date.strftime('%y')}",
                )
        friends = [int(x) for x in r.choice(people, size=min(n_payers, people.size), replace=False)]
        for f in friends:
            for _ in range(int(r.integers(1, 4))):
                amt = round_down(float(np.clip(np.exp(r.normal(np.log(1500), 0.9)), 100, 20_000)), 10)
                add(int(r.integers(lo, t_hi)), f, acct, amt, CH["UPI"], str(r.choice(nm.P2P_REMARKS)))
        if spend:
            n_days = max(1, (t_hi - lo) // DAY)
            for _ in range(int(r.integers(1, 3)) * max(1, int(n_days // 30) + 1)):
                biller = int(r.choice(pop.billers))
                add(
                    int(r.integers(lo, t_hi)),
                    acct,
                    biller,
                    round(float(r.uniform(149, 2500)), 2),
                    CH["UPI"],
                    str(r.choice(nm.BILL_REMARKS)),
                )
            for _ in range(int(r.integers(2, 6 + n_days // 5))):
                m = int(r.choice(shops))
                add(
                    int(r.integers(lo, t_hi)),
                    acct,
                    m,
                    round(float(np.exp(r.normal(np.log(600), 0.8))), 2),
                    CH["UPI"] if r.random() < 0.7 else CH["POS"],
                    str(r.choice(nm.MERCHANT_REMARKS)),
                )
            for _ in range(int(r.integers(0, 2 + n_days // 10))):
                add(
                    int(r.integers(lo, t_hi)),
                    acct,
                    None,
                    float(r.choice([500, 1000, 2000, 3000, 5000])),
                    CH["ATM"],
                    "ATM WDL",
                    self.atm_label(acct),
                )
        return ev

    # ------------------------------------------------------------ victims
    def pick_victim(self) -> int:
        r, pop = self.rng, self.pop
        for _ in range(1000):
            v = int(pop.individuals[int(r.integers(pop.individuals.size))])
            if v in self.used_victims or self.b.acct_opened[v] > self.b.start_s:
                continue
            home = self.b.acct_bank[v] == self.b.home
            if home and r.random() < 0.55:  # victims are mostly customers of other banks
                continue
            self.used_victims.add(v)
            self.b.role[v] = "VICTIM"
            self.b.ring[v] = self.ring_id
            self.b.typology[v] = self.typology
            return v
        raise RuntimeError("no victim available")

    def victim_amount(self) -> float:
        r = self.rng
        v = float(np.clip(np.exp(r.normal(np.log(125_000), 0.85)), 12_000, 2_500_000))
        u = r.random()
        if u < 0.55:
            return max(1000.0, round(v, -3))
        if u < 0.8:
            return round(v, -2)
        if u < 0.92:
            return max(999.0, round(v, -3) - 1)
        return float(round(v))

    def seed(
        self,
        ep: Episode,
        victim: int,
        to: int,
        t: int,
        amount: float,
        *,
        category: str = "",
        item: str = "",
        fir_p: float = 0.35,
        split_p: float = 0.15,
        remark: str = "",
    ) -> int:
        """Victim transfer(s) into a first-layer account; returns the ts of the last transfer."""
        r = self.rng
        rec = VictimRecord(
            victim=victim, seeds=[], first_layer=[to], category=category, item=item, fir_p=fir_p
        )
        parts = [amount]
        if split_p and r.random() < split_p and amount >= 20_000:
            first = round_down(amount * r.uniform(0.4, 0.7), 1000)
            parts = [first, round(amount - first, 2)]
        for i, part in enumerate(parts):
            if i:
                t += int(r.uniform(4, 30) * MINUTE)
            chan = upi_or_imps(part, r, p_upi=0.45)
            rem = remark or str(
                r.choice(["", "", "urgent", "verification", "refund", "processing fee", "deposit"])
            )
            idx, t = self.led.tx(t, victim, to, part, chan, rem)
            rec.seeds.append(idx)
        ep.victims.append(rec)
        return t

    # ------------------------------------------------------------ exits
    def atm_label(self, acct: int) -> str:
        r = self.rng
        city = (self.b.acct_city[acct] or "MUMBAI").upper().replace(" ", "-")
        return f"ATM {city}-{nm.CITY_AREAS[int(r.integers(len(nm.CITY_AREAS)))]}-{int(r.integers(1, 40)):02d}"

    def cash_out(
        self, acct: int, t: int, target: float, *, max_gap_min: float = 12.0, sweep: bool = False
    ) -> int:
        """ATM withdrawals of round amounts. ``sweep`` withdraws until less than Rs 100 is left. Hard mode: some
        cash-outs take only part of the target (partial cash-out; the rest stays and is holdable)."""
        r = self.rng
        if self.hard and not sweep and r.random() < HARD_PARTIAL_CASH_P:
            target *= r.uniform(0.25, 0.6)
        label = self.atm_label(acct)
        remaining = self.led.balance(acct) if sweep else min(target, self.led.balance(acct))
        step = 100.0 if sweep else 500.0
        n = 0
        while n < (14 if sweep else 8):
            bal = self.led.balance(acct)
            if (sweep and bal < 100) or (not sweep and remaining < 2000):
                break
            chunk = float(r.choice([10_000, 15_000, 20_000, 25_000, 40_000, 49_500, 50_000]))
            chunk = round_down(min(chunk, remaining, bal), step)
            if chunk < step:
                break
            t += int(r.uniform(2, max_gap_min) * MINUTE)
            if r.random() < 0.3:
                label = self.atm_label(acct)
            _, t = self.led.tx(t, acct, None, chunk, CH["ATM"], "ATM WDL", label)
            remaining -= chunk
            n += 1
        return t

    def crypto_out(self, acct: int, t: int, target: float, *, sweep: bool = False) -> int:
        """1-3 transfers to a VASP. ``sweep`` sends the exact remaining balance last."""
        r = self.rng
        vasp = nm.VASPS[int(r.integers(len(nm.VASPS)))]
        n = int(r.integers(1, 4))
        remaining = self.led.balance(acct) if sweep else min(target, self.led.balance(acct))
        for i in range(n):
            last = i == n - 1
            amt = remaining if last else remaining * r.uniform(0.3, 0.6)
            amt = min(amt, self.led.balance(acct))
            if not (sweep and last):
                amt = round_down(amt, 1000)
            if amt < 1000:
                break
            t += int(r.uniform(3, 25) * MINUTE)
            _, t = self.led.tx(t, acct, None, amt, CH["CRYPTO"], "VDA purchase", vasp)
            remaining -= amt
        return t

    def forward(
        self,
        frm: int,
        to: int,
        t: int,
        amount: float,
        step: float = 500.0,
        remark: str = "",
        exact: bool = False,
    ) -> int:
        """Round-amount transfer (``exact``: the whole current balance, paise included). Hard mode: some transfers
        are structured just under a threshold or made non-round (``structure``)."""
        amount = self.led.balance(frm) if exact else round_down(min(amount, self.led.balance(frm)), step)
        if amount < (1.0 if exact else step):
            return t
        if self.hard and not exact and amount >= 5000 and self.rng.random() < HARD_STRUCTURE_P:
            amount = self.structure(amount)
        _, t = self.led.tx(t, frm, to, amount, upi_or_imps(amount, self.rng), remark)
        return t

    def split_forward(
        self,
        frm: int,
        targets: list[int],
        t: int,
        total: float,
        step: float = 500.0,
        gap_min: tuple[float, float] = (1, 6),
        sweep: bool = False,
    ) -> list[int]:
        """Split ``total`` over targets (Dirichlet weights, round amounts); ``sweep`` sends the exact remaining
        balance to the last target. Returns receive times per target (-1 when skipped)."""
        r = self.rng
        w = r.dirichlet(np.full(len(targets), 2.5))
        times = []
        for i, (tgt, wi) in enumerate(zip(targets, w, strict=True)):
            t += int(r.uniform(*gap_min) * MINUTE)
            before = len(self.led.ts)
            t = self.forward(frm, tgt, t, total * wi, step, exact=sweep and i == len(targets) - 1)
            times.append(t if len(self.led.ts) > before else -1)
        return times

    def home_merchant(self, categories: list[str]) -> int:
        r, b = self.rng, self.b
        cands = [
            int(m)
            for c in categories
            for m in self.pop.merchant_cat.get(c, [])
            if b.acct_bank[int(m)] == b.home and b.acct_opened[int(m)] < b.start_s
        ]
        gst_ok = {g["gstin"] for g in b.gst if g["status"] == "ACTIVE"}
        cands = [m for m in cands if b.acct_gstin[m] in gst_ok] or cands
        return cands[int(r.integers(len(cands)))]

    def home_pool(self) -> int:
        cands = [int(p) for p in self.pop.pools if self.b.acct_bank[int(p)] == self.b.home]
        return cands[int(self.rng.integers(len(cands)))]
