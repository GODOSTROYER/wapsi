"""Plain-English reasons from score contributions.

Reasons state observable *indicators*, never guilt. Amounts use ``wapsi.money.fmt_inr``; durations are rendered in
minutes / hours / days. Reasons contain no account numbers, UTRs or NCRP ids.

Selection: contributions are ranked by size in the direction that matters for the role (positive = mule
indicators, negative = legitimate-profile indicators). A first pass picks at most one strong reason per feature
group, then the list is filled by rank. Ranking multiplies each contribution by ``RANK_FACTOR`` of its group so
reasons prefer what the bank itself observed (tenure, flows, devices) over third-party scores (FRI, MuleHunter).

Rupee amounts in reasons (e.g. the burst inflow) are feature values, not hold-plan slots: drafts should quote
reasons as context, not as verified figures.

Graph models may add a ``graph_neighbourhood`` contribution (``wapsi.classify.scoring.GRAPH_NEIGHBOURHOOD``): what
the account's position in the transaction graph adds to its score. It gets one sentence naming what the graph model
looked at (who paid the account, whom it paid, the devices it shares); it is ranked below what the bank observed
directly, since it is a model's summary rather than a single observable fact.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import pandas as pd

from wapsi.classify.features import NO_FORWARD_MINUTES, TURNOVER_RATIO_CAP
from wapsi.classify.scoring import BIAS, GRAPH_NEIGHBOURHOOD
from wapsi.models import Role
from wapsi.money import fmt_inr

MIN_CONTRIBUTION = 0.25  # log-odds; smaller effects are not worth a sentence
STRONG_CONTRIBUTION = 0.6  # the one-per-group diversity pass only considers effects at least this large
# presentation priority per feature group: facts the bank observed itself (tenure, flows) before third-party scores
RANK_FACTOR = {"tenure": 1.5, "profile": 1.2, "kyc": 0.8, "graph": 0.8, "external": 0.6}
MERGE_MIN_PASS_THROUGH = 0.5  # below this, forwarding speed is described on its own
SOP_5XII = "may have been operated without the holder's knowledge (SOP Para 5(xii))"
# a couple of past payers (family, employer) is not a customer base: never describe fewer as "established",
# whatever a trained scorer attributes to the feature
ESTABLISHED_MIN_PAYERS = 3

GROUP = {
    "age_days": "tenure",
    "history_days": "tenure",
    "dormant_then_burst": "tenure",
    "pass_through_ratio_24h": "flow",
    "median_forward_minutes": "flow",
    "in_count_24h": "flow",
    "out_count_24h": "flow",
    "in_amount_24h": "flow",
    "out_amount_24h": "flow",
    "round_amount_share": "flow",
    "n_distinct_payers_7d": "network",
    "n_distinct_payees_7d": "network",
    "device_shared_count": "network",
    "shared_device_with_seed_accounts": "network",
    "atm_share_24h": "exit",
    "crypto_share_24h": "exit",
    "night_share": "exit",
    "fri_risk": "external",
    "mulehunter_score": "external",
    "ncrp_prior_reports": "external",
    "kyc_min": "kyc",
    "kyc_vkyc": "kyc",
    "is_other_bank": "kyc",
    "salary_credits_90d": "legit",
    "recurring_rent_like_90d": "legit",
    "merchant_delivered_share": "legit",
    "gst_active": "legit",
    "is_business": "type",
    "is_merchant": "type",
    "is_intermediary": "type",
    "hist_distinct_payers_90d": "profile",
    "hist_credit_turnover_ratio": "profile",
    "known_payee_out_ratio_24h": "flow",
    GRAPH_NEIGHBOURHOOD: "graph",
}
# picking the key suppresses the listed features (their fact is already in the sentence)
SUPPRESSES = {"shared_device_with_seed_accounts": ("device_shared_count",)}
# pass-through and forwarding speed are merged into one sentence when both are strong mule indicators
FLOW_PAIR = frozenset({"pass_through_ratio_24h", "median_forward_minutes"})
FRI_NAMES = {1: "medium", 2: "high", 3: "very high"}


# --------------------------------------------------------------------------- formatting helpers


def pct(x: float) -> str:
    n = int(round(100 * x))
    if x < 1 and n >= 100:
        n = 99
    if x > 0 and n <= 0:
        n = 1
    return f"{n}%"


def fmt_minutes(m: float) -> str:
    if m < 1:
        return "less than a minute"
    if m < 120:
        n = int(round(m))
        return f"{n} minute{'s' if n != 1 else ''}"
    if m < 48 * 60:
        return f"about {int(round(m / 60))} hours"
    return f"about {int(round(m / 1440))} days"


def fmt_days(d: float) -> str:
    n = int(round(d))
    return f"{n} day{'s' if n != 1 else ''}"


def _hours(h: float) -> str:
    return f"{int(h)} hours" if float(h).is_integer() else f"{h:g} hours"


def _n(row: pd.Series, key: str) -> float:
    v = row.get(key, 0.0)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _tenure_phrase(age_days: float) -> str:
    years = age_days / 365.25
    if years >= 2:
        return f"open about {int(years)} years"
    if years >= 1:
        return "open for over a year"
    return f"open about {max(1, int(age_days / 30))} months"


# --------------------------------------------------------------------------- per-feature sentences

Describer = Callable[[pd.Series, float, float], "str | None"]  # (row, contribution, window_hours)


def _age(r, c, h):
    d = _n(r, "age_days")
    if c > 0:
        n = int(round(d))
        return (
            "Account opened on the day of the fraud"
            if n <= 0
            else f"Account opened {fmt_days(d)} before the fraud"
        )
    return f"Long-standing account, {_tenure_phrase(d)}" if d >= 180 else None


def _history(r, c, h):
    d = _n(r, "history_days")
    if c < 0:
        return f"Transaction history going back {fmt_days(d)} before the fraud"
    return (
        "No transaction history before the fraud" if d < 1 else f"Only {fmt_days(d)} of transaction history"
    )


def _dormant(r, c, h):
    return "Dormant account that suddenly became active around the time of the fraud" if c > 0 else None


def _flow(r, c, h):
    ratio, amt = _n(r, "pass_through_ratio_24h"), _n(r, "in_amount_24h")
    if c > 0 and amt > 0:
        return f"{pct(ratio)} of the {fmt_inr(amt)} received was sent on within {_hours(h)}"
    if c < 0 and amt > 0 and ratio < 0.2:
        return f"Kept most of the {fmt_inr(amt)} received; {pct(ratio) if ratio else 'none'} was sent on within {_hours(h)}"
    return None


def _forward(r, c, h):
    m = _n(r, "median_forward_minutes")
    if c > 0 and m < NO_FORWARD_MINUTES:
        return f"Money was typically sent on {fmt_minutes(m)} after it arrived"
    if c < 0:
        if m >= NO_FORWARD_MINUTES:
            return "Money received was not sent on"
        return f"Money typically stayed {fmt_minutes(m)} before being sent on"
    return None


def _flow_and_forward(r, h):
    ratio, amt, m = _n(r, "pass_through_ratio_24h"), _n(r, "in_amount_24h"), _n(r, "median_forward_minutes")
    return (
        f"{pct(ratio)} of the {fmt_inr(amt)} received was sent on within {_hours(h)}, "
        f"typically {fmt_minutes(m)} after arriving"
    )


def _count(key: str, word: str):
    def f(r, c, h):
        n = int(_n(r, key))
        return f"{_plural(n, word)} within {_hours(h)}" if c > 0 and n > 0 else None

    return f


def _amount(key: str, verb: str):
    def f(r, c, h):
        v = _n(r, key)
        return f"{verb} {fmt_inr(v)} within {_hours(h)}" if c > 0 and v > 0 else None

    return f


def _round(r, c, h):
    return f"{pct(_n(r, 'round_amount_share'))} of transfers were in round amounts" if c > 0 else None


def _payers(r, c, h):
    n = int(_n(r, "n_distinct_payers_7d"))
    return f"Received money from {_plural(n, 'different sender')} within a week" if c > 0 and n > 1 else None


def _payees(r, c, h):
    n = int(_n(r, "n_distinct_payees_7d"))
    return f"Sent money to {_plural(n, 'different account')} within a week" if c > 0 and n > 1 else None


def _device(r, c, h):
    n = int(_n(r, "device_shared_count"))
    if c > 0 and n > 0:
        return f"Device shared with {_plural(n, 'other account')}"
    return "Device not shared with any other account" if c < 0 and n == 0 else None


def _device_seed(r, c, h):
    n = int(_n(r, "shared_device_with_seed_accounts"))
    if c > 0 and n > 0:
        return f"Device shared with {_plural(n, 'other account')} that received the same money"
    return None


def _exit(key: str, where: str):
    def f(r, c, h):
        share, out = _n(r, key), _n(r, "out_amount_24h")
        if c <= 0 or share <= 0:
            return None
        part = "all of the money sent out" if share >= 0.995 else f"{pct(share)} of the money sent out"
        return f"{where.format(amt=fmt_inr(share * out))} within {_hours(h)} ({part})"

    return f


def _night(r, c, h):
    s = _n(r, "night_share")
    if c > 0 and s > 0:
        return f"{pct(s)} of transactions took place between 11 pm and 5 am IST"
    return None


def _fri(r, c, h):
    lvl = int(round(_n(r, "fri_risk")))
    if c > 0 and lvl > 0:
        return f"DoT Financial Fraud Risk Indicator: {FRI_NAMES.get(lvl, 'flagged')}"
    return "No DoT Financial Fraud Risk Indicator on the linked mobile" if c < 0 and lvl == 0 else None


def _mulehunter(r, c, h):
    s = _n(r, "mulehunter_score")
    if c > 0:
        return f"MuleHunter-style risk score of {s:.2f}"
    return f"Low MuleHunter-style risk score ({s:.2f})"


def _prior(r, c, h):
    n = int(_n(r, "ncrp_prior_reports"))
    if c > 0 and n > 0:
        return f"{_plural(n, 'earlier NCRP report')} on this account"
    return "No earlier NCRP reports on this account" if c < 0 and n == 0 else None


def _kyc_min(r, c, h):
    return "Minimum-KYC account" if c > 0 and _n(r, "kyc_min") else None


def _kyc_vkyc(r, c, h):
    return "Account opened remotely through video KYC" if c > 0 and _n(r, "kyc_vkyc") else None


def _other_bank(r, c, h):
    return (
        "Account at another bank, seen through CFCFRMS responses"
        if c > 0 and _n(r, "is_other_bank")
        else None
    )


def _salary(r, c, h):
    n = int(_n(r, "salary_credits_90d"))
    return f"{_plural(n, 'salary credit')} in the 90 days before the fraud" if c < 0 and n > 0 else None


def _rent(r, c, h):
    return (
        "Regular monthly credits of similar amounts, such as rent"
        if c < 0 and _n(r, "recurring_rent_like_90d")
        else None
    )


def _delivered(r, c, h):
    s = _n(r, "merchant_delivered_share")
    return f"{pct(s)} of payments received are backed by delivered invoices" if c < 0 and s > 0 else None


def _gst(r, c, h):
    return "Registered business with an active GSTIN" if c < 0 and _n(r, "gst_active") else None


def _hist_payers(r, c, h):
    n = int(_n(r, "hist_distinct_payers_90d"))
    return (
        f"Received payments from {_plural(n, 'different sender')} in the 90 days before the fraud"
        if c < 0 and n >= ESTABLISHED_MIN_PAYERS
        else None
    )


def _turnover(r, c, h):
    x = _n(r, "hist_credit_turnover_ratio")
    if c >= 0 or x <= 0:
        return None
    times = f"more than {TURNOVER_RATIO_CAP:g}" if x >= TURNOVER_RATIO_CAP else f"about {x:.1f}"
    return f"Credits in the 90 days before the fraud were {times} times the amount received"


def _known_payees(r, c, h):
    x = _n(r, "known_payee_out_ratio_24h")
    if c >= 0 or x <= 0:
        return None
    return f"{pct(x)} of the money received was paid within {_hours(h)} to accounts it had already paid before the fraud"


def _graph(r, c, h):
    # a model contribution, not a feature value: the row carries nothing to quote, so name what the model looked at
    direction = "raise" if c > 0 else "lower"
    return (
        "The accounts it received money from and sent money to, and the devices it shares, "
        f"{direction} the graph model's mule score"
    )


def _flag(key: str, text: str, sign: int):
    def f(r, c, h):
        return text if c * sign > 0 and _n(r, key) else None

    return f


DESCRIBERS: dict[str, Describer] = {
    "age_days": _age,
    "history_days": _history,
    "dormant_then_burst": _dormant,
    "pass_through_ratio_24h": _flow,
    "median_forward_minutes": _forward,
    "in_count_24h": _count("in_count_24h", "incoming transfer"),
    "out_count_24h": _count("out_count_24h", "outgoing transfer"),
    "in_amount_24h": _amount("in_amount_24h", "Received"),
    "out_amount_24h": _amount("out_amount_24h", "Sent out"),
    "round_amount_share": _round,
    "n_distinct_payers_7d": _payers,
    "n_distinct_payees_7d": _payees,
    "device_shared_count": _device,
    "shared_device_with_seed_accounts": _device_seed,
    "atm_share_24h": _exit("atm_share_24h", "Withdrew {amt} in cash at ATMs"),
    "crypto_share_24h": _exit("crypto_share_24h", "Sent {amt} to crypto exchanges"),
    "night_share": _night,
    "fri_risk": _fri,
    "mulehunter_score": _mulehunter,
    "ncrp_prior_reports": _prior,
    "kyc_min": _kyc_min,
    "kyc_vkyc": _kyc_vkyc,
    "is_other_bank": _other_bank,
    "salary_credits_90d": _salary,
    "recurring_rent_like_90d": _rent,
    "merchant_delivered_share": _delivered,
    "gst_active": _gst,
    "is_business": _flag("is_business", "Business account", -1),
    "is_merchant": _flag("is_merchant", "Merchant account", -1),
    "is_intermediary": _flag("is_intermediary", "Nodal / pool / escrow account", -1),
    "hist_distinct_payers_90d": _hist_payers,
    "hist_credit_turnover_ratio": _turnover,
    "known_payee_out_ratio_24h": _known_payees,
    GRAPH_NEIGHBOURHOOD: _graph,
}


def describe(feature: str, row: pd.Series, contribution: float, window_hours: float = 24.0) -> str | None:
    fn = DESCRIBERS.get(feature)
    return fn(row, contribution, window_hours) if fn else None


# --------------------------------------------------------------------------- selection


def _ranked(contrib: pd.Series, sign: int) -> list[tuple[str, float, float]]:
    """[(feature, contribution, ranking key)] in the requested direction, strongest first."""
    items = []
    for feat, val in contrib.items():
        if feat == BIAS or feat not in DESCRIBERS:
            continue
        v = float(val) if val == val else 0.0  # NaN-safe
        if v * sign < MIN_CONTRIBUTION:
            continue
        key = abs(v) * RANK_FACTOR.get(GROUP.get(feat, ""), 1.0)
        items.append((str(feat), v, key))
    items.sort(key=lambda t: (-t[2], t[0]))
    return items


def top_indicators(
    row: pd.Series,
    contrib: pd.Series,
    *,
    sign: int,
    k: int = 3,
    window_hours: float = 24.0,
    exclude: tuple[str, ...] = (),
) -> list[str]:
    """Up to k sentences for the strongest contributions in direction ``sign`` (+1 mule, -1 legitimate)."""
    ranked = [t for t in _ranked(contrib, sign) if t[0] not in exclude]
    ranked_names = {t[0] for t in ranked}
    out: list[str] = []
    used: set[str] = set()  # features already expressed (or suppressed)
    groups: set[str] = set()
    for one_per_group in (True, False):
        for feat, val, key in ranked:
            if len(out) >= k:
                return out
            group = GROUP.get(feat, feat)
            if feat in used or (one_per_group and (group in groups or key < STRONG_CONTRIBUTION)):
                continue
            text = None
            if (
                sign > 0
                and feat in FLOW_PAIR
                and FLOW_PAIR <= ranked_names
                and _n(row, "pass_through_ratio_24h") >= MERGE_MIN_PASS_THROUGH
                and _n(row, "in_amount_24h") > 0
                and _n(row, "median_forward_minutes") < NO_FORWARD_MINUTES
            ):
                text = _flow_and_forward(row, window_hours)
                used.update(FLOW_PAIR)
            text = text or describe(feat, row, val, window_hours)
            if not text:
                continue
            used.add(feat)
            used.update(SUPPRESSES.get(feat, ()))
            groups.add(group)
            if text not in out:
                out.append(text)
    return out


def _fallback_legit_facts(row: pd.Series, window_hours: float) -> list[str]:
    facts = []
    if _n(row, "in_amount_24h") > 0 and _n(row, "out_amount_24h") == 0:
        facts.append(f"None of the money received was sent on within {_hours(window_hours)}")
    if _n(row, "mulehunter_score") < 0.2:
        facts.append(f"Low MuleHunter-style risk score ({_n(row, 'mulehunter_score'):.2f})")
    if _n(row, "ncrp_prior_reports") == 0:
        facts.append("No earlier NCRP reports on this account")
    if _n(row, "fri_risk") == 0:
        facts.append("No DoT Financial Fraud Risk Indicator on the linked mobile")
    return facts


def _innocent_summary(row: pd.Series, contrib: pd.Series) -> tuple[str, tuple[str, ...]]:
    folded: list[str] = []
    c = lambda f: float(contrib.get(f, 0.0))  # noqa: E731
    if c("age_days") < 0 and _n(row, "age_days") >= 180:
        profile = f"Long-standing account ({_tenure_phrase(_n(row, 'age_days'))})"
        folded.append("age_days")
    else:
        profile = "Account"
    if c("recurring_rent_like_90d") < 0:
        detail = "with regular rent credits"
        folded.append("recurring_rent_like_90d")
    elif c("salary_credits_90d") < 0:
        detail = "with regular salary credits"
        folded.append("salary_credits_90d")
    elif c("merchant_delivered_share") < 0:
        detail = "with invoices showing delivered goods"
        folded.append("merchant_delivered_share")
    elif c("hist_distinct_payers_90d") < 0 and _n(row, "hist_distinct_payers_90d") >= ESTABLISHED_MIN_PAYERS:
        detail = "with an established base of payers"
    else:
        detail = "with few mule indicators"
    return f"{profile} {detail} — {SOP_5XII}", tuple(folded)


def explain(
    role: Role,
    row: pd.Series,
    contrib: pd.Series,
    *,
    score: float,
    window_hours: float = 24.0,
    k: int = 3,
) -> list[str]:
    """Reasons for a score-based role (MULE / INNOCENT_HOLDER / UNKNOWN)."""
    if role == Role.INNOCENT_HOLDER:
        summary, folded = _innocent_summary(row, contrib)
        reasons = top_indicators(row, contrib, sign=-1, k=k - 1, window_hours=window_hours, exclude=folded)
        for fact in _fallback_legit_facts(row, window_hours):
            if len(reasons) >= k - 1:
                break
            if fact not in reasons:
                reasons.append(fact)
        return [*reasons, summary]
    if role == Role.MULE:
        reasons = top_indicators(row, contrib, sign=+1, k=k, window_hours=window_hours)
        return reasons or [f"Model mule score of {score:.2f} is above the review threshold"]
    mule = top_indicators(row, contrib, sign=+1, k=k - 1, window_hours=window_hours)
    legit = top_indicators(row, contrib, sign=-1, k=1, window_hours=window_hours)
    picked = (mule[:1] + legit) if legit else mule
    return [
        *picked,
        f"Indicators are mixed (mule score {score:.2f}); officer review needed before any escalation",
    ]


def explain_blocked(
    row: pd.Series,
    contrib: pd.Series,
    blockers: list[str],
    *,
    score: float,
    window_hours: float = 24.0,
) -> list[str]:
    """Reasons for a score in the INNOCENT_HOLDER band that ``innocent_blockers`` downgraded to UNKNOWN."""
    legit = top_indicators(row, contrib, sign=-1, k=1, window_hours=window_hours)
    picked = [*blockers[:1], *legit] if legit else list(blockers[:2])
    return [
        *picked,
        f"Profile otherwise looks legitimate (mule score {score:.2f}); officer review needed before treating "
        "the holder as innocent (SOP Para 5(xii))",
    ]
