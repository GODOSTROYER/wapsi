"""Per-account behavioural features for mule / innocent-holder classification.

Only the ``wapsi.ports.Ledger`` protocol is used: home-bank accounts through ``transactions_touching`` and
other-bank accounts through ``consortium_transactions`` (simulated CFCFRMS responses).

Time windows (per account; timestamps are handled internally as UTC epoch seconds)
---------------------------------------------------------------------------------
* ``as_of`` - reference instant. *History* features only look at transactions strictly before it.
  ``wapsi.classify.classify_trace`` passes ``trace.window_start`` (the first reported victim transfer), so
  history describes the account *before* the fraud.
* burst window - ``[burst_start, burst_start + cfg.feature_window_hours]`` (inclusive, like the Ledger).
  ``classify_trace`` anchors it on each account's first tainted receipt, so the ``*_24h`` features describe what
  the account did with the money in the day after it arrived. With ``burst_start=None`` the burst window is the
  ``feature_window_hours`` immediately *before* ``as_of`` (plain snapshot scoring).
* ``obs_end = max(as_of, burst end)``. Behaviour windows run from ``as_of - N days`` to ``obs_end`` and therefore
  include the burst: 7 days for distinct payers/payees, 30 days for the night-time share, 90 days for recurring
  (rent-like) credits. ``salary_credits_90d`` uses the 90 days strictly before ``as_of``.
* ``history_days`` and the dormancy gap look back at most ``HISTORY_LOOKBACK_DAYS`` (180) before ``as_of``.

Counterparty-history features (what a genuine seller or landlord who is paid with victim money looks like; a mule
account is fresh, dormant or kept near empty and forwards to accounts it never dealt with before):

* ``hist_distinct_payers_90d`` - distinct counterparties that credited the account in the 90 days strictly
  before ``as_of`` (cash deposits have no counterparty and do not count): an established base of customers /
  tenants / employers.
* ``hist_credit_turnover_ratio`` - total credits in the 90 days before ``as_of`` divided by the burst inflow
  (capped at ``TURNOVER_RATIO_CAP``; 0 without burst inflow): whether the payment is in line with the account's
  usual turnover, the classic "sudden high-value credit in a low-activity account" check read the other way.
* ``known_payee_out_ratio_24h`` - burst debits to counterparties the account had already paid before ``as_of``
  (within the 180-day lookback), divided by the burst inflow (capped at 1): the part of the pass-through that is
  ordinary spending (rent to the same landlord, the usual supplier) rather than layering to fresh accounts.
  Always <= ``pass_through_ratio_24h``.

Ledger cost: one transaction call per visibility view (home / consortium) covering
``[min(as_of) - 180 days, max(obs_end)]``; every window is then a vectorised mask over that frame. Plus one call
each for accounts, customers, external signals, device links (x2), invoices and GST records.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from wapsi.config import ClassifyConfig
from wapsi.models import (
    INTERMEDIARY_ACCOUNT_TYPES,
    AccountType,
    Channel,
    OrderStatus,
    RiskLevel,
    Transaction,
)
from wapsi.ports import Ledger

FEATURE_COLUMNS: tuple[str, ...] = (
    "age_days",
    "kyc_min",
    "kyc_vkyc",
    "in_count_24h",
    "out_count_24h",
    "in_amount_24h",
    "out_amount_24h",
    "pass_through_ratio_24h",
    "median_forward_minutes",
    "n_distinct_payers_7d",
    "n_distinct_payees_7d",
    "night_share",
    "atm_share_24h",
    "crypto_share_24h",
    "device_shared_count",
    "shared_device_with_seed_accounts",
    "dormant_then_burst",
    "round_amount_share",
    "fri_risk",
    "mulehunter_score",
    "ncrp_prior_reports",
    "history_days",
    "salary_credits_90d",
    "recurring_rent_like_90d",
    "merchant_delivered_share",
    "gst_active",
    "is_business",
    "is_merchant",
    "is_intermediary",
    "is_other_bank",
    # counterparty-history features (added after the T6 triangulation review, see docs in account_features)
    "hist_distinct_payers_90d",
    "hist_credit_turnover_ratio",
    "known_payee_out_ratio_24h",
)

DAY_S = 86_400.0
HISTORY_LOOKBACK_DAYS = 180
NO_FORWARD_MINUTES = 7 * 24 * 60.0  # "never forwarded" default for median_forward_minutes (one week)
IST_OFFSET_S = 5.5 * 3600
NIGHT_START_HOUR, NIGHT_END_HOUR = 23, 5  # 23:00-05:00 IST
ROUND_UNIT = 1000.0  # an amount is "round" if it is a non-zero multiple of Rs 1,000
# dormant_then_burst: no activity for >= DORMANT_GAP_DAYS before as_of, account at least DORMANT_MIN_AGE_DAYS old,
# then >= BURST_MIN_TXNS transactions and >= BURST_MIN_INFLOW rupees received in the burst window.
DORMANT_GAP_DAYS = 30.0
DORMANT_MIN_AGE_DAYS = 90.0
BURST_MIN_TXNS = 3
BURST_MIN_INFLOW = 10_000.0
# recurring_rent_like_90d: credits of similar amount (+/-10%) 25-35 days apart, from the same payer or both with a
# rent-type remark, at least one of the pair before as_of. Heavy accounts are capped to their latest credits.
RECUR_MIN_AMOUNT = 500.0
RECUR_TOLERANCE = 0.10
RECUR_GAP_DAYS = (25.0, 35.0)
RECUR_MAX_CREDITS = 60
TURNOVER_RATIO_CAP = 10.0

FRI_LEVEL = {RiskLevel.NONE: 0.0, RiskLevel.MEDIUM: 1.0, RiskLevel.HIGH: 2.0, RiskLevel.VERY_HIGH: 3.0}
SALARY_RE = re.compile(r"\b(?:salary|sal|payroll|wages?|stipend|pension|vetan)\b", re.IGNORECASE)
RENT_RE = re.compile(r"\b(?:rent|rental|lease|kiraya|bhada|bhaada)\b", re.IGNORECASE)


def _epoch(ts: datetime) -> float:
    return (ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)).timestamp()


def _from_epoch(x: float) -> datetime:
    return datetime.fromtimestamp(x, tz=UTC)


def _per_account(value, ids: list[str], what: str) -> np.ndarray:
    """Expand a datetime or {account_id: datetime} mapping into epoch seconds aligned with ids."""
    if isinstance(value, Mapping):
        missing = [a for a in ids if a not in value]
        if missing:
            raise KeyError(f"{what} missing for accounts: {missing[:5]}")
        return np.array([_epoch(value[a]) for a in ids], dtype=float)
    return np.full(len(ids), _epoch(value), dtype=float)


def _txn_frame(txns: Iterable[Transaction]) -> pd.DataFrame:
    uniq: dict[str, Transaction] = {}
    for t in txns:
        uniq[t.txn_id] = t
    vals = list(uniq.values())
    return pd.DataFrame(
        {
            "txn_id": [t.txn_id for t in vals],
            "ts": np.array([_epoch(t.ts) for t in vals], dtype=float),
            "src": pd.Series([t.from_account for t in vals], dtype=object),
            "dst": pd.Series([t.to_account for t in vals], dtype=object),
            "amount": np.array([t.amount for t in vals], dtype=float),
            "channel": [str(t.channel) for t in vals],
            "remark": [t.remark or "" for t in vals],
        }
    )


def _explode(tx: pd.DataFrame, idset: set[str]) -> pd.DataFrame:
    """One row per (account, transaction side): direction +1 = credit, -1 = debit."""
    cols = ["account", "cp", "txn_id", "ts", "amount", "channel", "remark", "dir"]
    if tx.empty:
        return pd.DataFrame(
            {c: pd.Series(dtype=float if c in ("ts", "amount", "dir") else object) for c in cols}
        )
    not_self = (tx["src"] != tx["dst"]).to_numpy()
    deb = tx[tx["src"].isin(idset).to_numpy() & not_self]
    cre = tx[tx["dst"].isin(idset).to_numpy() & not_self]
    deb = deb.assign(account=deb["src"], cp=deb["dst"], dir=-1.0)
    cre = cre.assign(account=cre["dst"], cp=cre["src"], dir=1.0)
    return pd.concat([deb[cols], cre[cols]], ignore_index=True)


def _group(
    rows: pd.DataFrame, mask: np.ndarray, col: str, how: str, index: pd.Index, fill: float = 0.0
) -> pd.Series:
    sub = rows.loc[mask]
    if sub.empty:
        return pd.Series(fill, index=index, dtype=float)
    return sub.groupby("account")[col].agg(how).reindex(index).fillna(fill).astype(float)


def _safe_div(num: pd.Series, den: pd.Series) -> pd.Series:
    out = pd.Series(0.0, index=num.index)
    ok = den > 0
    out[ok] = num[ok] / den[ok]
    return out


def account_features(
    ledger: Ledger,
    account_ids: Iterable[str],
    as_of: datetime | Mapping[str, datetime],
    cfg: ClassifyConfig,
    *,
    seed_accounts: Iterable[str] | None = None,
    burst_start: datetime | Mapping[str, datetime] | None = None,
) -> pd.DataFrame:
    """Numeric feature frame (index ``account_id``, columns ``FEATURE_COLUMNS``, no NaN).

    ``as_of`` / ``burst_start`` may be one datetime for all accounts or a per-account mapping (used for training).
    ``seed_accounts``: accounts that received the same (tainted) money; drives ``shared_device_with_seed_accounts``
    (0 when not given).
    """
    ids = list(dict.fromkeys(account_ids))
    index = pd.Index(ids, name="account_id")
    if not ids:
        return pd.DataFrame(columns=list(FEATURE_COLUMNS), index=index, dtype=float)
    window_s = float(cfg.feature_window_hours) * 3600.0
    as_of_s = _per_account(as_of, ids, "as_of")
    if burst_start is None:
        b0 = as_of_s - window_s
        b1 = as_of_s - 1e-6  # strictly before as_of
    else:
        b0 = _per_account(burst_start, ids, "burst_start")
        b1 = b0 + window_s
    obs_end = np.maximum(as_of_s, b1)
    anchors = pd.DataFrame({"as_of": as_of_s, "b0": b0, "b1": b1, "obs_end": obs_end}, index=index)

    # ---- static attributes
    home = ledger.home_bank_id()
    accts = ledger.accounts(ids)
    bank = pd.Series({a: (accts[a].bank_id if a in accts else a.split("-", 1)[0]) for a in ids}, dtype=object)
    other_bank = (bank != home).reindex(index)
    atype = pd.Series(
        {a: (accts[a].account_type if a in accts else None) for a in ids}, dtype=object
    ).reindex(index)
    opened = pd.Series({a: (_epoch(accts[a].opened_at) if a in accts else np.nan) for a in ids}, dtype=float)
    age_days = ((anchors["as_of"] - opened.reindex(index)) / DAY_S).clip(lower=0.0).fillna(0.0)

    cust_ids = {a.customer_id for a in accts.values() if a.customer_id}
    customers = ledger.customers(cust_ids) if cust_ids else {}
    tier = pd.Series(
        {
            a: (
                customers[accts[a].customer_id].kyc_tier.upper()
                if a in accts and accts[a].customer_id in customers
                else ""
            )
            for a in ids
        },
        dtype=object,
    ).reindex(index)

    gstins = {a.gstin for a in accts.values() if a.gstin}
    gst = ledger.gst_records(gstins) if gstins else {}
    gst_active = pd.Series(
        {
            a: float(
                a in accts
                and bool(accts[a].gstin)
                and accts[a].gstin in gst
                and gst[accts[a].gstin].status.upper() == "ACTIVE"
            )
            for a in ids
        },
        dtype=float,
    ).reindex(index)

    signals = ledger.external_signals(ids)
    fri = pd.Series(
        {a: FRI_LEVEL.get(signals[a].fri_risk, 0.0) if a in signals else 0.0 for a in ids}, dtype=float
    )
    mhs = pd.Series(
        {a: float(signals[a].mulehunter_score) if a in signals else 0.0 for a in ids}, dtype=float
    )
    prior = pd.Series(
        {a: float(signals[a].ncrp_prior_reports) if a in signals else 0.0 for a in ids}, dtype=float
    )

    # ---- transactions: one call per visibility view
    start = _from_epoch(float(as_of_s.min()) - HISTORY_LOOKBACK_DAYS * DAY_S)
    end = _from_epoch(float(obs_end.max()))
    home_ids = [a for a in ids if not other_bank[a]]
    other_ids = [a for a in ids if other_bank[a]]
    txns: list[Transaction] = []
    if home_ids:
        txns.extend(ledger.transactions_touching(home_ids, start, end))
    if other_ids:
        txns.extend(ledger.consortium_transactions(other_ids, start, end))
    rows = _explode(_txn_frame(txns), set(ids))
    rows = rows.join(anchors, on="account")

    ts = rows["ts"].to_numpy(dtype=float)
    a_s = rows["as_of"].to_numpy(dtype=float)
    credit = rows["dir"].to_numpy() > 0
    debit = ~credit
    burst = (ts >= rows["b0"].to_numpy()) & (ts <= rows["b1"].to_numpy())
    until_obs = ts <= rows["obs_end"].to_numpy()
    before = (ts < a_s) & (ts >= a_s - HISTORY_LOOKBACK_DAYS * DAY_S)
    win7 = (ts >= a_s - 7 * DAY_S) & until_obs
    win30 = (ts >= a_s - 30 * DAY_S) & until_obs
    win90 = (ts >= a_s - 90 * DAY_S) & until_obs
    before90 = before & (ts >= a_s - 90 * DAY_S)
    channel = rows["channel"].to_numpy(dtype=object)

    in_count = _group(rows, burst & credit, "amount", "count", index)
    out_count = _group(rows, burst & debit, "amount", "count", index)
    in_amount = _group(rows, burst & credit, "amount", "sum", index)
    out_amount = _group(rows, burst & debit, "amount", "sum", index)
    pass_through = _safe_div(out_amount, in_amount).clip(upper=1.0)
    atm_amount = _group(rows, burst & debit & (channel == Channel.ATM.value), "amount", "sum", index)
    crypto_amount = _group(rows, burst & debit & (channel == Channel.CRYPTO.value), "amount", "sum", index)

    payers = _group(rows, win7 & credit, "cp", "nunique", index)
    payees = _group(rows, win7 & debit, "cp", "nunique", index)

    hour = ((ts + IST_OFFSET_S) % DAY_S) / 3600.0
    night = (hour >= NIGHT_START_HOUR) | (hour < NIGHT_END_HOUR)
    rows["_night"] = night.astype(float)
    night_share = _group(rows, win30, "_night", "mean", index)

    amt = rows["amount"].to_numpy(dtype=float)
    rows["_round"] = (
        (amt >= ROUND_UNIT) & (np.abs(amt - np.round(amt / ROUND_UNIT) * ROUND_UNIT) < 0.005)
    ).astype(float)
    round_share = _group(rows, burst, "_round", "mean", index)

    first_before = _group(rows, before, "ts", "min", index, fill=np.nan)
    last_before = _group(rows, before, "ts", "max", index, fill=np.nan)
    history_days = ((anchors["as_of"] - first_before) / DAY_S).fillna(0.0).clip(0.0, HISTORY_LOOKBACK_DAYS)
    gap_days = ((anchors["as_of"] - last_before) / DAY_S).fillna(age_days.clip(upper=HISTORY_LOOKBACK_DAYS))
    dormant = (
        (gap_days >= DORMANT_GAP_DAYS)
        & (age_days >= DORMANT_MIN_AGE_DAYS)
        & ((in_count + out_count) >= BURST_MIN_TXNS)
        & (in_amount >= BURST_MIN_INFLOW)
    ).astype(float)

    remarks = rows["remark"].astype(str)
    rows["_salary"] = remarks.str.contains(SALARY_RE).to_numpy(dtype=float)
    salary = _group(rows, before90 & credit, "_salary", "sum", index)

    hist_payers = _group(rows, before90 & credit, "cp", "nunique", index)
    hist_credits = _group(rows, before90 & credit, "amount", "sum", index)
    turnover = _safe_div(hist_credits, in_amount).clip(upper=TURNOVER_RATIO_CAP)
    known_out = _known_payee_amount(rows, before & debit, burst & debit, index)
    known_ratio = np.minimum(_safe_div(known_out, in_amount).clip(upper=1.0), pass_through)

    forward = _median_forward_minutes(rows[burst], index)
    recurring = _recurring_credits(rows[win90 & credit & (amt >= RECUR_MIN_AMOUNT)], remarks, index)
    delivered_share = _delivered_share(ledger, rows[burst & credit], index)
    dev_shared, dev_seed = _device_features(ledger, ids, anchors["obs_end"], seed_accounts)

    is_type = lambda types: atype.map(lambda t: float(t in types)).astype(float)  # noqa: E731
    out = pd.DataFrame(
        {
            "age_days": age_days,
            "kyc_min": (tier == "MIN").astype(float),
            "kyc_vkyc": (tier == "VKYC").astype(float),
            "in_count_24h": in_count,
            "out_count_24h": out_count,
            "in_amount_24h": in_amount.round(2),
            "out_amount_24h": out_amount.round(2),
            "pass_through_ratio_24h": pass_through,
            "median_forward_minutes": forward,
            "n_distinct_payers_7d": payers,
            "n_distinct_payees_7d": payees,
            "night_share": night_share,
            "atm_share_24h": _safe_div(atm_amount, out_amount),
            "crypto_share_24h": _safe_div(crypto_amount, out_amount),
            "device_shared_count": dev_shared,
            "shared_device_with_seed_accounts": dev_seed,
            "dormant_then_burst": dormant,
            "round_amount_share": round_share,
            "fri_risk": fri.reindex(index),
            "mulehunter_score": mhs.reindex(index),
            "ncrp_prior_reports": prior.reindex(index),
            "history_days": history_days,
            "salary_credits_90d": salary,
            "recurring_rent_like_90d": recurring,
            "merchant_delivered_share": delivered_share,
            "gst_active": gst_active,
            "is_business": is_type({AccountType.BUSINESS}),
            "is_merchant": is_type({AccountType.MERCHANT}),
            "is_intermediary": is_type(INTERMEDIARY_ACCOUNT_TYPES),
            "is_other_bank": other_bank.astype(float),
            "hist_distinct_payers_90d": hist_payers,
            "hist_credit_turnover_ratio": turnover,
            "known_payee_out_ratio_24h": known_ratio,
        },
        index=index,
    )
    return out[list(FEATURE_COLUMNS)].astype(float).fillna(0.0)


def _known_payee_amount(
    rows: pd.DataFrame, hist_debit: np.ndarray, burst_debit: np.ndarray, index: pd.Index
) -> pd.Series:
    """Burst debit amount per account that went to counterparties the account had paid in its history."""
    zero = pd.Series(0.0, index=index, dtype=float)
    hist = rows.loc[hist_debit, ["account", "cp"]].dropna()
    out = rows.loc[burst_debit, ["account", "cp", "amount"]].dropna(subset=["cp"])
    if hist.empty or out.empty:
        return zero
    known = pd.MultiIndex.from_frame(hist.drop_duplicates())
    is_known = pd.MultiIndex.from_frame(out[["account", "cp"]]).isin(known)
    if not is_known.any():
        return zero
    return out[is_known].groupby("account")["amount"].sum().reindex(index).fillna(0.0).astype(float)


def _median_forward_minutes(burst_rows: pd.DataFrame, index: pd.Index) -> pd.Series:
    """Median minutes from each burst credit to the account's next debit (same window); default if none."""
    default = pd.Series(NO_FORWARD_MINUTES, index=index, dtype=float)
    credits = burst_rows.loc[burst_rows["dir"] > 0, ["account", "ts"]].sort_values("ts")
    debits = burst_rows.loc[burst_rows["dir"] < 0, ["account", "ts"]].sort_values("ts")
    if credits.empty or debits.empty:
        return default
    debits = debits.assign(debit_ts=debits["ts"])
    matched = pd.merge_asof(
        credits, debits, on="ts", by="account", direction="forward", allow_exact_matches=True
    )
    matched["minutes"] = (matched["debit_ts"] - matched["ts"]) / 60.0
    med = matched.groupby("account")["minutes"].median()  # NaN (no later debit) is skipped
    return med.reindex(index).fillna(NO_FORWARD_MINUTES).clip(upper=NO_FORWARD_MINUTES).astype(float)


def _recurring_credits(credits: pd.DataFrame, remarks: pd.Series, index: pd.Index) -> pd.Series:
    """Number of credits that belong to a monthly, similar-amount pair (rent / salary-like)."""
    zero = pd.Series(0.0, index=index, dtype=float)
    if len(credits) < 2:
        return zero
    c = credits[["account", "cp", "ts", "amount", "as_of"]].copy()
    c["rentish"] = remarks.loc[credits.index].str.contains(RENT_RE).to_numpy()
    c["cp"] = c["cp"].fillna("").astype(str)
    c = c.sort_values(["account", "ts"])
    c = c[c.groupby("account").cumcount(ascending=False) < RECUR_MAX_CREDITS]
    c["rid"] = np.arange(len(c))
    pairs = c.merge(c, on="account", suffixes=("", "_o"))
    gap = (pairs["ts_o"] - pairs["ts"]) / DAY_S
    hi = np.maximum(pairs["amount"], pairs["amount_o"])
    same_payer = (pairs["cp"] == pairs["cp_o"]) & (pairs["cp"] != "")
    ok = (
        (gap >= RECUR_GAP_DAYS[0])
        & (gap <= RECUR_GAP_DAYS[1])
        & ((pairs["amount"] - pairs["amount_o"]).abs() <= RECUR_TOLERANCE * hi)
        & (same_payer | (pairs["rentish"] & pairs["rentish_o"]))
        & ((pairs["ts"] < pairs["as_of"]) | (pairs["ts_o"] < pairs["as_of"]))
    )
    hit = pairs.loc[ok]
    if hit.empty:
        return zero
    members = pd.concat(
        [hit[["account", "rid"]], hit[["account", "rid_o"]].rename(columns={"rid_o": "rid"})],
        ignore_index=True,
    )
    return (
        members.drop_duplicates().groupby("account")["rid"].count().reindex(index).fillna(0.0).astype(float)
    )


def _delivered_share(ledger: Ledger, credits: pd.DataFrame, index: pd.Index) -> pd.Series:
    """Share of burst-window credit amount backed by a DELIVERED invoice of the receiving account."""
    zero = pd.Series(0.0, index=index, dtype=float)
    if credits.empty:
        return zero
    invoices = ledger.invoices_for_transactions(credits["txn_id"].unique().tolist())
    backed = {
        (inv.merchant_account, inv.txn_id)
        for inv in invoices
        if inv.order_status == OrderStatus.DELIVERED and inv.delivered_at is not None
    }
    if not backed:
        return zero
    keys = list(zip(credits["account"], credits["txn_id"], strict=True))
    is_backed = np.array([k in backed for k in keys], dtype=bool)
    total = credits.groupby("account")["amount"].sum()
    good = credits[is_backed].groupby("account")["amount"].sum()
    return _safe_div(good.reindex(index).fillna(0.0), total.reindex(index).fillna(0.0))


def _device_features(
    ledger: Ledger, ids: list[str], obs_end: pd.Series, seed_accounts: Iterable[str] | None
) -> tuple[pd.Series, pd.Series]:
    """Distinct other accounts on the same devices (links first seen by obs_end), overall and within the seeds."""
    zero = pd.Series(0.0, index=obs_end.index, dtype=float)
    own = [(lk.account_id, lk.device_id, _epoch(lk.first_seen)) for lk in ledger.device_links(ids)]
    if not own:
        return zero, zero.copy()
    own_df = pd.DataFrame(own, columns=["account", "device", "seen"])
    own_df = own_df[own_df["seen"] <= obs_end.reindex(own_df["account"]).to_numpy()]
    if own_df.empty:
        return zero, zero.copy()
    peers = [
        (lk.device_id, lk.account_id, _epoch(lk.first_seen))
        for lk in ledger.device_links_for_devices(own_df["device"].unique().tolist())
    ]
    peer_df = pd.DataFrame(peers, columns=["device", "other", "other_seen"])
    pairs = own_df.merge(peer_df, on="device")
    pairs = pairs[(pairs["other"] != pairs["account"])]
    pairs = pairs[pairs["other_seen"] <= obs_end.reindex(pairs["account"]).to_numpy()]
    shared = pairs.groupby("account")["other"].nunique().reindex(obs_end.index).fillna(0.0).astype(float)
    seeds = set(seed_accounts or ())
    if not seeds:
        return shared, zero.copy()
    seeded = pairs[pairs["other"].isin(seeds)]
    seed_shared = (
        seeded.groupby("account")["other"].nunique().reindex(obs_end.index).fillna(0.0).astype(float)
    )
    return shared, seed_shared
