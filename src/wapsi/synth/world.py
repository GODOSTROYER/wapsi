"""Synthetic Indian banking world: ``generate_world(WorldConfig) -> dict[str, DataFrame]``.

Tables follow docs/data-contract.md (product tables + ``meta`` + ground-truth ``labels_*``).
Deterministic: the same config gives byte-identical Parquet (fixed ordering, no hash-seed dependence).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from wapsi.money import fmt_inr
from wapsi.synth import names as nm
from wapsi.synth import narratives as nv
from wapsi.synth.background import generate_background
from wapsi.synth.builder import CHANNELS, DAY, HOUR, MINUTE, TxnChunk, WorldBuilder, epoch_s
from wapsi.synth.export import SCHEMAS, ledger_to_frames, meta_frame
from wapsi.synth.population import build_population
from wapsi.synth.rings import Episode
from wapsi.synth.typologies import generate_rings
from wapsi.timeutil import IST

GENERATOR_VERSION = "wapsi-synth/1.0"
IST_OFFSET_S = 19800

Size = Literal["showcase", "demo", "full"]

SIZES: dict[str, dict] = {
    "demo": {"n_accounts": 3000, "n_rings": 30, "episode_p": [0.62, 0.38], "n_pools": 6, "activity": 0.9},
    "full": {
        "n_accounts": 60000,
        "n_rings": 150,
        "episode_p": [0.3, 0.5, 0.2],
        "n_pools": 24,
        "activity": 1.6,
    },
}
ADVERSARIAL_RATE = 0.07


@dataclass
class WorldConfig:
    name: str = "demo"
    size: Size = "demo"
    seed: int = 7
    start: datetime = field(default_factory=lambda: datetime(2026, 8, 1, tzinfo=IST))
    days: int = 45
    home_bank_id: str = "HB01"
    overrides: dict = field(default_factory=dict)  # tweak SIZES entries (tests)


# --------------------------------------------------------------------------- helpers
def _ts_array(seconds: np.ndarray) -> pa.Array:
    return pa.array(seconds.astype(np.int64) * 1_000_000, type=pa.timestamp("us", tz="UTC"))


def _digits(nums: np.ndarray, width: int) -> pa.Array:
    return pc.utf8_lpad(pc.cast(pa.array(nums.astype(np.int64)), pa.string()), width=width, padding="0")


def _take(values: list[str], codes: np.ndarray) -> pa.Array:
    return pc.take(pa.array(values, pa.string()), pa.array(codes.astype(np.int64), mask=codes < 0))


def _group_rank(keys: np.ndarray) -> np.ndarray:
    """Rank of each element within its key group, in current order (keys need not be sorted)."""
    order = np.argsort(keys, kind="stable")
    sk = keys[order]
    starts = np.flatnonzero(np.r_[True, sk[1:] != sk[:-1]])
    lens = np.diff(np.r_[starts, sk.size])
    within = np.arange(sk.size) - np.repeat(starts, lens)
    out = np.empty_like(within)
    out[order] = within
    return out


def _utrs(ts: np.ndarray, chan: np.ndarray, from_bank: pa.Array) -> pa.Array:
    n = ts.size
    ist = ts + IST_OFFSET_S
    days = (ist // DAY).astype("datetime64[D]")
    years = days.astype("datetime64[Y]")
    doy = (days - years).astype(np.int64) + 1
    yy = years.astype(np.int64) + 1970
    hour = (ist % DAY) // HOUR
    c = {name: i for i, name in enumerate(CHANNELS)}
    fam_atm = chan == c["ATM"]
    fam_csh = chan == c["CASH_DEPOSIT"]
    fam_neft = (chan == c["NEFT"]) | (chan == c["RTGS"])
    fam_rrn = ~(fam_atm | fam_csh | fam_neft)
    pieces: list[pa.Array] = []
    index = np.empty(n, dtype=np.int64)
    offset = 0

    def put(mask: np.ndarray, arr: pa.Array) -> None:
        nonlocal offset
        idx = np.flatnonzero(mask)
        index[idx] = offset + np.arange(idx.size)
        offset += idx.size
        pieces.append(arr)

    m = fam_rrn
    key = (ist[m] // DAY) * 24 + hour[m]
    seq = (_group_rank(key) * 7919 + key * 131) % 1_000_000
    val = (yy[m] % 10) * 10**11 + doy[m] * 10**8 + hour[m] * 10**6 + seq
    put(m, _digits(val, 12))
    for mask, prefix, off in ((fam_atm, "ATM", 104_729), (fam_csh, "CSH", 7_368_787)):
        k = np.arange(int(mask.sum()), dtype=np.int64)
        put(mask, pc.binary_join_element_wise(prefix, _digits((k * 7919 + off) % 10**9, 9), ""))
    m = fam_neft
    day_key = ist[m] // DAY
    seq = (_group_rank(day_key) * 7919 + day_key * 17) % 1_000_000
    val = (yy[m] % 100) * 10**9 + doy[m] * 10**6 + seq
    letter = pa.array(np.where(chan[m] == c["RTGS"], "R", "N"), pa.string())
    put(m, pc.binary_join_element_wise(pc.filter(from_bank, pa.array(m)), letter, _digits(val, 11), ""))
    return pc.take(pa.concat_arrays(pieces), pa.array(index))


def _fix_openings(b: WorldBuilder, allt: TxnChunk, rank: np.ndarray, n_ring: int, base: np.ndarray) -> None:
    """Raise opening balances of background accounts so their running balance never goes negative.
    Ring credits into background accounts are ignored (they only add money), keeping holds robust."""
    ded = np.array(b.dedicated, dtype=bool)
    fr, to, amt = allt.frm, allt.to, allt.amt
    is_ring = np.arange(len(allt)) < n_ring
    m_from = (fr >= 0) & ~ded[np.maximum(fr, 0)]
    m_to = (to >= 0) & ~ded[np.maximum(to, 0)] & ~is_ring
    acct = np.concatenate([fr[m_from], to[m_to]]).astype(np.int64)
    rk = np.concatenate([rank[m_from], rank[m_to]])
    val = np.concatenate([-amt[m_from], amt[m_to]])
    o = np.lexsort((rk, acct))
    acct, val = acct[o], val[o]
    need = np.zeros(len(b.acct_ids))
    if acct.size:
        starts = np.flatnonzero(np.r_[True, acct[1:] != acct[:-1]])
        lens = np.diff(np.r_[starts, acct.size])
        cs = np.cumsum(val)
        before = np.r_[0.0, cs[starts[1:] - 1]]
        within = cs - np.repeat(before, lens)
        gmin = np.minimum.reduceat(within, starts)
        need[acct[starts]] = np.maximum(0.0, -gmin)
    for i in np.flatnonzero(~ded).tolist():
        b.acct_opening[i] = round(
            max(b.acct_opening[i], float(base[i])) + math.ceil(need[i] * 100) / 100 + 0.0, 2
        )


def _signals(b: WorldBuilder, rng: np.random.Generator) -> pd.DataFrame:
    n = len(b.acct_ids)
    u = rng.random(n)
    fri = np.where(u < 0.95, "NONE", np.where(u < 0.99, "MEDIUM", "HIGH")).astype(object)
    score = np.clip(rng.beta(1.2, 14.0, n), 0, 1)
    medium = rng.random(n) < 0.04
    score = np.where(medium, rng.uniform(0.35, 0.6, n), score)
    prior = np.where(rng.random(n) < 0.004, 1, 0)
    types = np.array(b.acct_type)
    inter = np.isin(types, ["NODAL", "POOL", "ESCROW"])
    fri[inter] = "NONE"
    score[inter] = 0.0
    prior[inter] = 0
    for i, (f, s, p) in b.signal_override.items():
        fri[i], score[i], prior[i] = f, s, p
    return pd.DataFrame(
        {
            "account_id": b.acct_ids,
            "fri_risk": fri.astype(str),
            "mulehunter_score": np.round(score, 3),
            "ncrp_prior_reports": prior.astype(np.int64),
        }
    )


def _background_devices(b: WorldBuilder, rng: np.random.Generator) -> None:
    ded = b.dedicated
    persons = []
    for i, t in enumerate(b.acct_type):
        if ded[i] or t in ("NODAL", "POOL", "ESCROW"):
            continue
        k = (
            1
            + int(rng.random() < (0.1 if t == "INDIVIDUAL" else 0.6))
            + int(t != "INDIVIDUAL" and rng.random() < 0.3)
        )
        for _ in range(k):
            first = max(b.acct_opened[i], b.start_s - int(rng.integers(0, 700)) * DAY)
            dev = b.add_device(i, min(first, b.end_s - DAY))
            if t == "INDIVIDUAL":
                persons.append((dev, i))
    # family devices: a few devices used by two unrelated-looking legitimate accounts
    for j in rng.choice(len(persons), size=max(1, len(persons) // 50), replace=False).tolist():
        dev, i = persons[j]
        other = persons[int(rng.integers(len(persons)))][1]
        if other != i:
            b.add_device(other, max(b.acct_opened[other], b.acct_opened[i]) + DAY, device=dev)


def _case_groups(complaints: list[dict]) -> dict[str, str]:
    """Union-find: complaints sharing a first-layer account form one case. Returns ncrp_id -> case_id."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_acct: dict[str, str] = {}
    for c in complaints:
        parent[c["ncrp_id"]] = c["ncrp_id"]
        for a in c["_first_layer"]:
            if a in by_acct:
                ra, rb = find(by_acct[a]), find(c["ncrp_id"])
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
            else:
                by_acct[a] = c["ncrp_id"]
    groups: dict[str, list[dict]] = {}
    for c in complaints:
        groups.setdefault(find(c["ncrp_id"]), []).append(c)
    out = {}
    for members in groups.values():
        first = min(members, key=lambda c: (c["reported_at"], c["ncrp_id"]))
        for c in members:
            out[c["ncrp_id"]] = f"CASE-{first['ncrp_id']}"
    return out


def _complaints(
    b: WorldBuilder,
    rng: np.random.Generator,
    episodes: list[Episode],
    ring_rank: np.ndarray,
    txn_ids: pa.Array,
    utrs: pa.Array,
    ring_chunk: TxnChunk,
) -> list[dict]:
    cust = {c["customer_id"]: c for c in b.customers}
    drafts = []
    for ep in episodes:
        for rec in ep.victims:
            last = max(int(ring_chunk.ts[s]) for s in rec.seeds)
            reported = min(last + int(rng.uniform(0.5, 6.0) * HOUR), b.end_s - MINUTE)
            drafts.append((reported, ep, rec))
    drafts.sort(key=lambda d: (d[0], d[1].ring_id, d[2].seeds[0]))
    base = int(rng.integers(1000, 90000))
    out = []
    for i, (reported, ep, rec) in enumerate(drafts):
        victim = cust[b.acct_cust[rec.victim]]
        d = b.ist_date(reported)
        ncrp = f"{int(rng.integers(1, 4))}{d.strftime('%d%m%y')}{base + i:07d}"
        first = rec.seeds[0]
        amounts = [float(ring_chunk.amt[s]) for s in rec.seeds]
        total = round(sum(amounts), 2)
        fmt = int(rng.integers(3))
        money = (
            (lambda x: f"Rs {fmt_inr(x, symbol=False, paise=False)}")
            if fmt < 2
            else (lambda x: fmt_inr(x, paise=False))
        )
        category = rec.category or nv.GENERIC_CATEGORIES[int(rng.integers(len(nv.GENERIC_CATEGORIES)))]
        lang = "hi-en" if rng.random() < 0.35 else "en"
        seed_ts = datetime.fromtimestamp(int(ring_chunk.ts[first]), tz=UTC).astimezone(IST)
        utr = utrs[int(ring_rank[first])].as_py() if rng.random() < 0.4 else None
        text, lang = nv.render_narrative(
            rng,
            category,
            lang,
            amount=money(amounts[0] if len(amounts) == 1 else total),
            acct=b.acct_ids[rec.first_layer[0]],
            time=seed_ts.strftime("%H:%M"),
            date=seed_ts.strftime("%d-%m-%Y"),
            channel=CHANNELS[int(ring_chunk.chan[first])],
            utr=utr,
            item=rec.item,
        )
        if len(amounts) > 1:
            text += nv.MULTI_TXN_SUFFIX[lang].format(
                total=money(total), n=len(amounts), parts=", ".join(money(a) for a in amounts)
            )
        fir = None
        if rng.random() < rec.fir_p:
            fir = f"FIR {int(rng.integers(20, 990))}/2026, Cyber PS {victim['city'] or 'Mumbai'}"
        out.append(
            {
                "ncrp_id": ncrp,
                "reported_at": reported,
                "victim_account": b.acct_ids[rec.victim],
                "victim_name": victim["name"],
                "victim_mobile": victim["mobile"],
                "amount_claimed": total,
                "narrative_text": text,
                "language": lang,
                "category": category,
                "fir_number": fir,
                "source": "1930" if rng.random() < 0.5 else "NCRP",
                "_txns": [txn_ids[int(ring_rank[s])].as_py() for s in rec.seeds],
                "_first_layer": [b.acct_ids[a] for a in rec.first_layer],
                "_ring": ep.ring_id,
                "_typology": ep.typology,
                "_adv": False,
            }
        )
    n_adv = max(1, round(ADVERSARIAL_RATE * len(out))) if len(out) >= 5 else 0
    for j in sorted(rng.choice(len(out), size=n_adv, replace=False).tolist()):
        c = out[j]
        c["narrative_text"] = nv.inject(rng, c["narrative_text"], c["language"], c["_first_layer"][0])
        c["_adv"] = True
    return out


# --------------------------------------------------------------------------- main entry points
def generate_world(cfg: WorldConfig) -> dict[str, pd.DataFrame]:
    return build_world(cfg)[0]


def build_world(cfg: WorldConfig) -> tuple[dict[str, pd.DataFrame], list]:
    """Returns (frames, reference cases). Reference cases are ``wapsi.synth.reference.RefCase`` objects."""
    if cfg.size == "showcase":
        return _showcase_world(cfg)
    from wapsi.synth.golden import compute_cases

    params = {**SIZES[cfg.size], **cfg.overrides}
    ss = np.random.SeedSequence(cfg.seed)
    r_pop, r_ring, r_bg, r_fin = (np.random.default_rng(s) for s in ss.spawn(4))
    start_s = epoch_s(cfg.start)
    b = WorldBuilder(
        name=cfg.name, seed=cfg.seed, start_s=start_s, days=cfg.days, home=cfg.home_bank_id, rng=r_pop
    )
    n_acc = int(params["n_accounts"])
    mean_ep = sum((i + 1) * p for i, p in enumerate(params["episode_p"]))
    n_ring_accts = int(params["n_rings"] * mean_ep * 2.8)
    shares = {cfg.home_bank_id: 0.55, "OB02": 0.3, "OB03": 0.25}
    b.reserve_account_numbers({k: int(n_acc * v) + 2000 for k, v in shares.items()})
    pop = build_population(b, max(200, n_acc - n_ring_accts), int(params["n_pools"]))

    b.rng = r_ring
    led, episodes = generate_rings(b, pop, r_ring, int(params["n_rings"]), list(params["episode_p"]))
    ring_chunk = led.chunk()
    ring_invoices = list(b.invoices)

    b.rng = r_bg
    bg = generate_background(b, pop, r_bg, float(params["activity"]))
    n_ring = len(ring_chunk)
    allt = TxnChunk.concat([ring_chunk, bg.chunk])
    order = np.lexsort((np.arange(len(allt)), allt.ts))
    rank = np.empty(len(allt), dtype=np.int64)
    rank[order] = np.arange(len(allt))

    b.rng = r_fin
    base = np.maximum(100.0, np.round(pop.wealth * r_fin.uniform(0.15, 1.2, pop.wealth.size), 2))
    base = np.concatenate([base, np.zeros(len(b.acct_ids) - base.size)])
    _fix_openings(b, allt, rank, n_ring, base)
    _background_devices(b, r_fin)

    # ---- transactions table (final order = (ts, txn_id))
    st = TxnChunk(*(getattr(allt, f)[order] for f in ("ts", "frm", "to", "amt", "chan", "remark", "label")))
    n = len(st)
    acct_arr = pa.array(b.acct_ids, pa.string())
    bank_arr = pa.array(b.acct_bank, pa.string())
    frm_mask = pa.array(st.frm.astype(np.int64), mask=st.frm < 0)
    to_mask = pa.array(st.to.astype(np.int64), mask=st.to < 0)
    from_bank = pc.take(bank_arr, frm_mask)
    txn_ids = pc.binary_join_element_wise("TX", _digits(np.arange(1, n + 1), 10), "")
    utrs = _utrs(st.ts, st.chan, pc.fill_null(from_bank, "HB01"))
    tx_table = pa.table(
        {
            "txn_id": txn_ids,
            "ts": _ts_array(st.ts),
            "from_account": pc.take(acct_arr, frm_mask),
            "to_account": pc.take(acct_arr, to_mask),
            "from_bank_id": from_bank,
            "to_bank_id": pc.take(bank_arr, to_mask),
            "amount": pa.array(st.amt),
            "channel": _take(CHANNELS, st.chan.astype(np.int64)),
            "utr": utrs,
            "remark": _take(b.text.values, st.remark),
            "counterparty_label": _take(b.text.values, st.label),
        },
        schema=SCHEMAS["transactions"],
    )

    # ---- invoices
    inv_m = np.concatenate(
        [np.array([i["merchant"] for i in ring_invoices], dtype=np.int64), bg.invoices["merchant"]]
    )
    inv_p = np.concatenate(
        [np.array([i["payer"] for i in ring_invoices], dtype=np.int64), bg.invoices["payer"]]
    )
    inv_t = np.concatenate(
        [
            np.array([i["txn"] for i in ring_invoices], dtype=np.int64),
            bg.invoices["txn"].astype(np.int64) + n_ring,
        ]
    )
    inv_a = np.concatenate([np.array([i["amount"] for i in ring_invoices]), bg.invoices["amount"]])
    inv_o = np.concatenate(
        [np.array([i["ordered_at"] for i in ring_invoices], dtype=np.int64), bg.invoices["ordered"]]
    )
    inv_d = np.concatenate(
        [
            np.array(
                [-1 if i["delivered_at"] is None else i["delivered_at"] for i in ring_invoices],
                dtype=np.int64,
            ),
            bg.invoices["delivered"],
        ]
    )
    status_names = ["DELIVERED", "UNDELIVERED", "CANCELLED"]
    inv_s = np.concatenate(
        [
            np.array([status_names.index(i["order_status"]) for i in ring_invoices], dtype=np.int64),
            bg.invoices["status"],
        ]
    )
    inv_rank = rank[inv_t]
    io = np.argsort(inv_rank, kind="stable")
    inv_table = pa.table(
        {
            "invoice_id": pc.binary_join_element_wise("INV-", _digits(np.arange(1, io.size + 1), 8), ""),
            "merchant_account": pc.take(acct_arr, pa.array(inv_m[io])),
            "payer_account": pc.take(acct_arr, pa.array(inv_p[io])),
            "txn_id": pc.take(txn_ids, pa.array(inv_rank[io])),
            "amount": pa.array(inv_a[io]),
            "ordered_at": _ts_array(inv_o[io]),
            "delivered_at": pc.if_else(
                pa.array(inv_d[io] < 0),
                pa.scalar(None, pa.timestamp("us", tz="UTC")),
                _ts_array(np.maximum(inv_d[io], 0)),
            ),
            "order_status": _take(status_names, inv_s[io]),
        },
        schema=SCHEMAS["invoices"],
    )

    # ---- complaints
    complaints = _complaints(b, r_fin, episodes, rank[:n_ring], txn_ids, utrs, ring_chunk)
    case_of = _case_groups(complaints)
    comp_df = pd.DataFrame([{k: v for k, v in c.items() if not k.startswith("_")} for c in complaints])
    comp_df["reported_at"] = pd.to_datetime(comp_df["reported_at"] * 1_000_000, unit="us", utc=True)
    ct_df = pd.DataFrame(
        [(c["ncrp_id"], t) for c in complaints for t in c["_txns"]], columns=["ncrp_id", "txn_id"]
    )
    labels_complaints = pd.DataFrame(
        {
            "ncrp_id": [c["ncrp_id"] for c in complaints],
            "case_id": [case_of[c["ncrp_id"]] for c in complaints],
            "ring_id": [c["_ring"] for c in complaints],
            "typology": [c["_typology"] for c in complaints],
            "is_adversarial": [c["_adv"] for c in complaints],
        }
    )

    frames: dict[str, pd.DataFrame] = {
        "banks": pd.DataFrame(
            {
                "bank_id": [cfg.home_bank_id, "OB02", "OB03"],
                "name": [nm.BANK_NAMES["HB01"], nm.BANK_NAMES["OB02"], nm.BANK_NAMES["OB03"]],
                "is_home": [True, False, False],
            }
        ),
        "customers": pa.Table.from_pylist(b.customers, schema=SCHEMAS["customers"]).to_pandas(),
        "accounts": pa.table(
            {
                "account_id": acct_arr,
                "bank_id": bank_arr,
                "customer_id": pa.array(b.acct_cust, pa.string()),
                "account_type": pa.array(b.acct_type, pa.string()),
                "opened_at": _ts_array(np.array(b.acct_opened, dtype=np.int64)),
                "gstin": pa.array(b.acct_gstin, pa.string()),
                "status": pa.array(b.acct_status, pa.string()),
                "opening_balance": pa.array(np.round(np.array(b.acct_opening), 2)),
            },
            schema=SCHEMAS["accounts"],
        ).to_pandas(),
        "transactions": tx_table.to_pandas(),
        "invoices": inv_table.to_pandas(),
        "gst_registry": pa.Table.from_pylist(b.gst, schema=SCHEMAS["gst_registry"]).to_pandas(),
        "external_signals": _signals(b, r_fin),
        "device_links": pa.table(
            {
                "device_id": pc.binary_join_element_wise(
                    "DEV-",
                    _digits(
                        (np.array([d[0] for d in b.devices]) * 7919 + 1_000_003) % 9_000_000 + 1_000_000, 7
                    ),
                    "",
                ),
                "account_id": pc.take(acct_arr, pa.array([d[1] for d in b.devices], pa.int64())),
                "first_seen": _ts_array(np.array([d[2] for d in b.devices], dtype=np.int64)),
            },
            schema=SCHEMAS["device_links"],
        )
        .to_pandas()
        .sort_values(["account_id", "device_id", "first_seen"], kind="mergesort")
        .reset_index(drop=True),
        "complaints": comp_df,
        "complaint_txns": ct_df,
        "labels_accounts": pd.DataFrame(
            {"account_id": b.acct_ids, "role_truth": b.role, "ring_id": b.ring, "typology": b.typology}
        ),
        "labels_complaints": labels_complaints,
    }
    world_end = (cfg.start + timedelta(days=cfg.days)).astimezone(UTC)
    typ_counts = pd.Series([e.typology for e in episodes]).value_counts().sort_index()
    frames["meta"] = meta_frame(
        {
            "world": cfg.name,
            "size": cfg.size,
            "seed": cfg.seed,
            "generator_version": GENERATOR_VERSION,
            "world_start": cfg.start.astimezone(UTC).isoformat(),
            "world_end": world_end.isoformat(),
            "home_bank_id": cfg.home_bank_id,
            "days": cfg.days,
            "n_accounts": len(b.acct_ids),
            "n_transactions": n,
            "n_rings": int(params["n_rings"]),
            "n_episodes": len(episodes),
            "n_complaints": len(complaints),
            "n_cases": len(set(case_of.values())),
            "n_adversarial": int(labels_complaints["is_adversarial"].sum()),
            "episodes_by_typology": ";".join(f"{k}={v}" for k, v in typ_counts.items()),
        }
    )
    cases = compute_cases(frames, cfg.name)
    frames["labels_flows"] = flows_frame(cases)
    warnings = sum(len(c.warnings) for c in cases)
    frames["meta"] = pd.concat(
        [frames["meta"], meta_frame({"reference_warnings": warnings})], ignore_index=True
    )
    return frames, cases


def flows_frame(cases: list) -> pd.DataFrame:
    rows = [(e.txn_id, n, v) for c in cases for e in c.edges for n, v in e.tainted.items()]
    df = pd.DataFrame(rows, columns=["txn_id", "ncrp_id", "tainted_amount_truth"])
    return df.sort_values(["txn_id", "ncrp_id"], kind="mergesort").reset_index(drop=True)


# --------------------------------------------------------------------------- showcase
def _showcase_world(cfg: WorldConfig) -> tuple[dict[str, pd.DataFrame], list]:
    from wapsi.fixtures import showcase as sc
    from wapsi.synth.golden import compute_cases

    frames = ledger_to_frames(sc.showcase_ledger())
    ring1 = {sc.A, sc.B, sc.D, sc.L, sc.C, sc.E, sc.V1}
    roles = {a: "LEGIT" for a in frames["accounts"]["account_id"]}
    roles.update({v: "VICTIM" for v in (sc.V1, sc.V2, sc.V3)})
    roles.update({a: r.value for a, r in {**sc.SHOWCASE_1["roles"], **sc.SHOWCASE_2["roles"]}.items()})
    ring_of = {a: ("RING-SC1" if a in ring1 else "RING-SC2") for a in [*ring1, sc.M, sc.V2, sc.V3]}
    typ = {"RING-SC1": "T2_CHAIN_LAYERING", "RING-SC2": "T7_MULTI_VICTIM"}
    ids = frames["accounts"]["account_id"].tolist()
    frames["labels_accounts"] = pd.DataFrame(
        {
            "account_id": ids,
            "role_truth": [roles[a] for a in ids],
            "ring_id": [ring_of.get(a) for a in ids],
            "typology": [typ.get(ring_of.get(a, "")) for a in ids],
        }
    )
    frames["labels_complaints"] = pd.DataFrame(
        {
            "ncrp_id": [sc.N1, sc.N2, sc.N3],
            "case_id": [f"CASE-{sc.N1}", f"CASE-{sc.N2}", f"CASE-{sc.N2}"],
            "ring_id": ["RING-SC1", "RING-SC2", "RING-SC2"],
            "typology": ["T2_CHAIN_LAYERING", "T7_MULTI_VICTIM", "T7_MULTI_VICTIM"],
            "is_adversarial": [False] * 3,
        }
    )
    start = sc.WORLD_START.astimezone(UTC)
    end = datetime(2026, 9, 30, tzinfo=IST).astimezone(UTC)  # the fixture's "now"
    frames["meta"] = meta_frame(
        {
            "world": cfg.name,
            "size": "showcase",
            "seed": 0,
            "generator_version": GENERATOR_VERSION,
            "world_start": start.isoformat(),
            "world_end": end.isoformat(),
            "home_bank_id": sc.HOME,
            "days": (end - start).days,
            "n_accounts": len(ids),
            "n_transactions": len(frames["transactions"]),
            "n_rings": 2,
            "n_episodes": 2,
            "n_complaints": 3,
            "n_cases": 2,
            "n_adversarial": 0,
            "episodes_by_typology": "T2_CHAIN_LAYERING=1;T7_MULTI_VICTIM=1",
            "source": "wapsi.fixtures.showcase.showcase_ledger",
        }
    )
    cases = compute_cases(frames, cfg.name)
    frames["labels_flows"] = flows_frame(cases)
    frames["meta"] = pd.concat(
        [frames["meta"], meta_frame({"reference_warnings": sum(len(c.warnings) for c in cases)})],
        ignore_index=True,
    )
    return frames, cases
