"""Public entry point of the money-trail engine: ``trace_case``."""

from __future__ import annotations

from collections.abc import Iterable

from wapsi.config import TraceConfig
from wapsi.graph.expand import expand_candidates
from wapsi.graph.taint import simulate
from wapsi.models import TraceResult
from wapsi.ports import Ledger
from wapsi.timeutil import utcnow


def trace_case(
    ledger: Ledger, ncrp_ids: Iterable[str], cfg: TraceConfig | None = None, case_id: str | None = None
) -> TraceResult:
    """Follow the victims' money from the complaint transactions and apportion it per ``cfg.rule``.

    ``TraceResult.accounts`` holds the victim accounts (layer 0; a victim account that received another
    complaint's money is listed at its trail layer instead) and every account that received tainted money;
    ``TraceResult.edges`` holds the tainted transfers (account-to-account edges carrying less than
    ``cfg.min_edge_amount`` are left out of the list, the accounting stays exact).
    """
    cfg = cfg or TraceConfig()
    ids = list(dict.fromkeys(ncrp_ids))
    if not ids:
        raise ValueError("trace_case needs at least one ncrp_id")
    exp = expand_candidates(ledger, ids, cfg)
    sim = simulate(ledger, exp, cfg)
    return TraceResult(
        case_id=case_id or f"CASE-{ids[0]}",
        ncrp_ids=ids,
        rule=cfg.rule,
        window_start=exp.window_start,
        window_end=exp.window_end,
        victim_seed=sim.victim_seed,
        edges=sim.edges,
        accounts=sim.accounts,
        totals=sim.totals,
        warnings=list(dict.fromkeys([*exp.warnings, *sim.warnings])),
        computed_at=utcnow(),
    )
