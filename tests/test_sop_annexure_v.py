"""Wapsi reproduces the SOP's own pro-rata worked example (Annexure-V, Illustration 2) to the paisa.

Account statement (SOP 02.01.2026, Annexure-V, Illustration 2, printed page 95):
    15.12.2024 12:20  credit 85,326  (V)   13:23 credit 50,000 (X)   14:30 credit 30,000 (Z)
    15.12.2024 12:51  credit 38,443  (W)   13:55 credit 50,000 (Y)   15:06 debit  47,226
    balance available after seizure 2,06,543
Shares to be released pro rata (printed page 96): X 40,695.08 · V 69,446.97 · Y 40,695.08 · Z 24,417.05 ·
W 31,288.82. The figures are also recorded in data/docs/facts.json (ANNEX_V_ILL2_*).
"""

from datetime import datetime

from wapsi.adapters.memory import InMemoryLedger
from wapsi.config import TraceConfig, WapsiConfig
from wapsi.graph import build_hold_plan, trace_case
from wapsi.models import (
    Account,
    AccountType,
    Bank,
    Channel,
    ClassificationResult,
    Complaint,
    RestorationProcess,
    Role,
    Transaction,
)
from wapsi.money import eq
from wapsi.timeutil import IST

MULE = "HB01-7000000001"
CREDITS = {  # complainant -> (time, amount)
    "V": ((12, 20), 85326.0),
    "W": ((12, 51), 38443.0),
    "X": ((13, 23), 50000.0),
    "Y": ((13, 55), 50000.0),
    "Z": ((14, 30), 30000.0),
}
SOP_SHARES = {"X": 40695.08, "V": 69446.97, "Y": 40695.08, "Z": 24417.05, "W": 31288.82}
NCRP = {k: f"3151224000000{i}" for i, k in enumerate(CREDITS, start=1)}


def _ts(hh: int, mm: int) -> datetime:
    return datetime(2024, 12, 15, hh, mm, tzinfo=IST)


def _ledger() -> InMemoryLedger:
    victims = {k: f"OB02-51000000{i:02d}" for i, k in enumerate(CREDITS, start=1)}
    accounts = [
        Account(account_id=MULE, bank_id="HB01", customer_id="C-M", account_type=AccountType.INDIVIDUAL,
                opened_at=datetime(2024, 11, 1, tzinfo=IST)),
        *[
            Account(account_id=a, bank_id="OB02", customer_id=f"C-{k}", account_type=AccountType.INDIVIDUAL,
                    opened_at=datetime(2015, 1, 1, tzinfo=IST), opening_balance=500000.0)
            for k, a in victims.items()
        ],
    ]
    txns = [
        Transaction(txn_id=f"T-{k}", ts=_ts(*hm), from_account=victims[k], to_account=MULE, from_bank_id="OB02",
                    to_bank_id="HB01", amount=amt, channel=Channel.IMPS, utr=f"43490000000{i}")
        for i, (k, (hm, amt)) in enumerate(CREDITS.items(), start=1)
    ]
    txns.append(Transaction(txn_id="T-DEBIT", ts=_ts(15, 6), from_account=MULE, to_account=None,
                            from_bank_id="HB01", amount=47226.0, channel=Channel.ATM, utr="ATM000000099",
                            counterparty_label="ATM"))
    complaints = [
        Complaint(ncrp_id=NCRP[k], reported_at=datetime(2024, 12, 16, 18, 0, tzinfo=IST),
                  victim_account=victims[k], victim_name=f"Complainant {k}", victim_mobile="9XXXXXXXXX",
                  amount_claimed=amt, narrative_text="Defrauded; money sent to the account below.")
        for k, (_, amt) in CREDITS.items()
    ]
    return InMemoryLedger(
        banks=[Bank(bank_id="HB01", name="Home (synthetic)", is_home=True), Bank(bank_id="OB02", name="Other")],
        accounts=accounts, transactions=txns, complaints=complaints,
        complaint_txns=[(NCRP[k], f"T-{k}") for k in CREDITS],
    )


def test_pro_rata_matches_sop_annexure_v_illustration_2():
    ledger = _ledger()
    trace = trace_case(ledger, list(NCRP.values()), TraceConfig())
    st = trace.accounts[MULE]
    assert eq(st.balance_now, 206543.0)
    for k, share in SOP_SHARES.items():
        assert eq(st.tainted_now[NCRP[k]], share), (k, st.tainted_now[NCRP[k]], share)

    roles = {MULE: ClassificationResult(account_id=MULE, role=Role.MULE, score=0.9)}
    plan = build_hold_plan(trace, roles, ledger, WapsiConfig())
    (dec,) = plan.decisions
    assert eq(dec.hold_amount, 206543.0)
    assert plan.restoration_process == RestorationProcess.P2
    for k, share in SOP_SHARES.items():
        assert eq(dec.per_victim[NCRP[k]], share), k
        assert eq(plan.per_victim_recoverable[NCRP[k]], share), k
