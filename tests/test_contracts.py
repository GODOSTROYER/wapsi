from datetime import datetime

from wapsi import citations, money
from wapsi.fixtures import showcase as sc
from wapsi.ports import Ledger
from wapsi.timeutil import IST


def test_ledger_protocol():
    assert isinstance(sc.showcase_ledger(), Ledger)


def test_showcase_final_balances():
    ledger = sc.showcase_ledger()
    end = datetime(2026, 9, 30, tzinfo=IST)
    bal = ledger.balances_at(sc.SHOWCASE_1["balance_now"], end)
    for acct, expected in sc.SHOWCASE_1["balance_now"].items():
        assert money.eq(bal[acct], expected), (acct, bal[acct], expected)
    assert money.eq(ledger.balances_at([sc.M], end)[sc.M], 120000.0)


def test_showcase_totals_balance():
    t = sc.SHOWCASE_1["totals"]
    parts = t["holdable"] + t["cashed_out"] + t["outbound"] + t["intermediary"] + t["merchant_settled"]
    assert money.eq(parts + t["unlocated"], t["stolen"])


def test_visibility():
    ledger = sc.showcase_ledger()
    start, end = datetime(2026, 8, 1, tzinfo=IST), datetime(2026, 9, 30, tzinfo=IST)
    home_view = {t.txn_id for t in ledger.transactions_touching([sc.D, sc.E], start, end)}
    assert "TX-010" in home_view  # D (home) -> E (other bank)
    assert "TX-011" not in home_view  # E's ATM withdrawal is other-bank only
    consortium = {t.txn_id for t in ledger.consortium_transactions([sc.E, sc.D], start, end)}
    assert consortium == {"TX-010", "TX-011"}


def test_money_format():
    assert money.fmt_inr(480000) == "₹4,80,000.00"
    assert money.fmt_inr(50350000) == "₹5,03,50,000.00"
    assert money.fmt_inr(999.5, paise=False) == "₹999"
    assert money.find_amounts("sent Rs 4,80,000 and ₹1,152.00")[1][1] == 1152.0


def test_citation_registry():
    reg = citations.registry()
    assert "SOP-5-XIII" in reg and reg["SOP-9-1"].section


def test_showcase_bundles_consistent():
    from wapsi.fixtures.bundles import showcase_bundle

    b1 = showcase_bundle(1)
    assert money.eq(b1.plan.total_hold, sc.SHOWCASE_1["total_hold"])
    assert money.eq(b1.plan.total_recoverable, sc.SHOWCASE_1["total_recoverable"])
    assert money.eq(b1.plan.total_forward_hold, 96000.0)
    assert money.eq(b1.plan.total_hold, 142848.0)
    assert b1.trace.accounts[sc.V1].layer == 0
    assert money.eq(sum(e.tainted_total for e in b1.trace.edges if e.to_account is None), 144000.0)
    b2 = showcase_bundle(2)
    dec = {d.account_id: d for d in b2.plan.decisions}[sc.M]
    assert dec.per_victim == {sc.N2: 75000.0, sc.N3: 45000.0}
    assert dec.requires_human and "ESCALATE_S106_CANDIDATE" in dec.reason_codes
    assert b2.plan.restoration_process.value == "P2"
    assert b2.trace.edges[-1].tainted == {sc.N2: 25000.0, sc.N3: 15000.0}


def test_packaged_citations_in_sync():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert (root / "data/docs/citations.json").read_bytes() == (root / "src/wapsi/_data/citations.json").read_bytes(), (
        "run: cp data/docs/citations.json src/wapsi/_data/citations.json"
    )
    reg = citations.registry()
    assert reg["SOP-9-1-III"].quote and reg["SOP-9-1-III"].page == 26
