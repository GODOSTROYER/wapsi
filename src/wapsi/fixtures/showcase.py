"""Hand-computed showcase worlds. Every module tests against these numbers (pro-rata rule).

Showcase 1 (single victim, Process 1) — victim Meera Joshi (OB02) loses ₹4,80,000 on 10-09-2026 11:02 IST.

    T1  11:02  V1 -> A      4,80,000 IMPS   A had ₹20,000 of its own (cash deposit 01-09) -> A tainted ratio 0.96
    T2  11:09  A  -> B      3,00,000 IMPS   tainted 2,88,000
    T3  11:15  A  -> L          5,000 UPI    tainted 4,800   (landlord, rent; §5(xii) innocent holder)
    T4a 11:20  A  -> ATM       50,000        tainted 48,000  (cash-out)
    T4b 11:24  A  -> ATM       50,000        tainted 48,000  (cash-out)
    T5  11:30  A  -> C(OB03)   50,000 UPI    tainted 48,000  (other bank, followed via CFCFRMS)
    T6  11:40  B  -> K          1,200 UPI    tainted 1,152   (kirana, goods delivered; §9.2 no hold)
    T7  11:45  B  -> P         20,000 UPI    tainted 19,200  (payment-aggregator POOL; §9.1(iii): hold the
                                                              disputed amount only, never suspend/seize)
    T8  12:05  B  -> D      2,50,000 IMPS   tainted 2,40,000
    T9  12:30  D  -> E(OB03) 2,00,000 IMPS  tainted 1,92,000
    T10 13:10  E  -> ATM       50,000        tainted 48,000  (cash-out at other bank)

  Remaining tainted: A 43,200 · B 27,648 · D 48,000 · L 4,800 · K 1,152 · P 19,200 · C 48,000 · E 1,44,000
  Totals: stolen 4,80,000 = holdable 1,23,648 + cashed_out 1,44,000 + outbound 1,92,000
                            + intermediary 19,200 + merchant_settled 1,152 ; unlocated 0
  Hold plan: A 43,200 · B 27,648 · D 48,000 · L 4,800 · P 19,200 (amount-specific, pool account) = 1,42,848
  Whole-account-freeze baseline: ₹5,03,50,000 across 6 home accounts (the pool account alone is ₹5,00,20,000)

Showcase 2 (two victims, Process 2) — 11-09-2026.

    U1  15:00  V2 -> M  1,00,000 IMPS
    U2  15:10  V3 -> M    60,000 UPI
    U3  15:30  M  -> ATM  40,000   tainted {N2: 25,000, N3: 15,000}

  M keeps 1,20,000 = {N2: 75,000, N3: 45,000}. N2 has an FIR and M has 2 prior NCRP reports
  -> hold stays amount-specific, plus reason code ESCALATE_S106_CANDIDATE and requires_human=True.
"""

from __future__ import annotations

from datetime import datetime

from wapsi.adapters.memory import InMemoryLedger
from wapsi.models import (
    Account,
    AccountType,
    Bank,
    Channel,
    Complaint,
    Customer,
    DeviceLink,
    Disposition,
    ExternalSignal,
    GstRecord,
    Invoice,
    OrderStatus,
    RestorationProcess,
    RiskLevel,
    Role,
    StrGround,
    Transaction,
)
from wapsi.timeutil import IST

HOME = "HB01"
A = "HB01-1000000001"
B = "HB01-1000000002"
D = "HB01-1000000003"
M = "HB01-1000000004"
L = "HB01-2000000001"
K = "HB01-3000000001"
P = "HB01-9000000001"
V1 = "OB02-5000000001"
V2 = "OB02-5000000002"
V3 = "OB02-5000000003"
TENANT = "OB02-5000000010"
PAYERS = [f"OB02-60000000{i:02d}" for i in range(1, 6)]
C = "OB03-4000000001"
E = "OB03-4000000002"

N1 = "31009260000101"
N2 = "31109260000102"
N3 = "31109260000103"

K_GSTIN = "27ABCDE1234F1Z5"
WORLD_START = datetime(2026, 8, 1, 0, 0, tzinfo=IST)


def _ist(month: int, day: int, hh: int, mm: int) -> datetime:
    return datetime(2026, month, day, hh, mm, tzinfo=IST)


def _t(txn_id, ts, frm, to, amount, channel, utr, remark="", label=None) -> Transaction:
    bank = lambda a: a.split("-")[0] if a else None  # noqa: E731
    return Transaction(
        txn_id=txn_id, ts=ts, from_account=frm, to_account=to, from_bank_id=bank(frm), to_bank_id=bank(to),
        amount=amount, channel=channel, utr=utr, remark=remark, counterparty_label=label,
    )


def showcase_ledger() -> InMemoryLedger:
    banks = [
        Bank(bank_id=HOME, name="Sahyadri Co-operative Bank (synthetic)", is_home=True),
        Bank(bank_id="OB02", name="Konkan Commercial Bank (synthetic)"),
        Bank(bank_id="OB03", name="Deccan Payments Bank (synthetic)"),
    ]
    ind = AccountType.INDIVIDUAL
    accounts = [
        Account(account_id=A, bank_id=HOME, customer_id="CUST-A", account_type=ind, opened_at=_ist(8, 30, 10, 0)),
        Account(account_id=B, bank_id=HOME, customer_id="CUST-B", account_type=ind, opened_at=_ist(8, 20, 12, 0)),
        Account(account_id=D, bank_id=HOME, customer_id="CUST-D", account_type=ind, opened_at=_ist(9, 1, 16, 0)),
        Account(account_id=M, bank_id=HOME, customer_id="CUST-M", account_type=ind, opened_at=_ist(9, 5, 11, 0)),
        Account(account_id=L, bank_id=HOME, customer_id="CUST-L", account_type=ind,
                opened_at=datetime(2019, 6, 3, 10, 0, tzinfo=IST), opening_balance=145000.0),
        Account(account_id=K, bank_id=HOME, customer_id="CUST-K", account_type=AccountType.MERCHANT,
                opened_at=datetime(2019, 4, 2, 10, 0, tzinfo=IST), gstin=K_GSTIN, opening_balance=45000.0),
        Account(account_id=P, bank_id=HOME, customer_id=None, account_type=AccountType.POOL,
                opened_at=datetime(2021, 1, 15, 10, 0, tzinfo=IST), opening_balance=50000000.0),
        Account(account_id=V1, bank_id="OB02", customer_id="CUST-V1", account_type=ind,
                opened_at=datetime(2015, 5, 5, 10, 0, tzinfo=IST), opening_balance=1000000.0),
        Account(account_id=V2, bank_id="OB02", customer_id="CUST-V2", account_type=ind,
                opened_at=datetime(2017, 7, 7, 10, 0, tzinfo=IST), opening_balance=300000.0),
        Account(account_id=V3, bank_id="OB02", customer_id="CUST-V3", account_type=ind,
                opened_at=datetime(2018, 8, 8, 10, 0, tzinfo=IST), opening_balance=200000.0),
        Account(account_id=TENANT, bank_id="OB02", customer_id="CUST-T", account_type=ind,
                opened_at=datetime(2016, 1, 1, 10, 0, tzinfo=IST), opening_balance=90000.0),
        *[
            Account(account_id=p, bank_id="OB02", customer_id=f"CUST-P{i}", account_type=ind,
                    opened_at=datetime(2020, 1, 1, 10, 0, tzinfo=IST), opening_balance=20000.0)
            for i, p in enumerate(PAYERS)
        ],
        Account(account_id=C, bank_id="OB03", customer_id="CUST-C", account_type=ind, opened_at=_ist(8, 28, 9, 0)),
        Account(account_id=E, bank_id="OB03", customer_id="CUST-E", account_type=ind, opened_at=_ist(9, 2, 9, 0)),
    ]
    txns = [
        # background history
        _t("TX-H01", _ist(8, 10, 9, 30), TENANT, L, 5000.0, Channel.UPI, "622210001001", "rent Aug"),
        _t("TX-H02", _ist(9, 1, 17, 5), None, A, 20000.0, Channel.CASH_DEPOSIT, "CSH000000001", "cash deposit"),
        *[
            _t(f"TX-K0{i}", _ist(9, 9, 10 + i, 15), p, K, amt, Channel.UPI, f"62221000200{i}", "groceries")
            for i, (p, amt) in enumerate(zip(PAYERS, [800.0, 1200.0, 950.0, 1050.0, 1000.0], strict=True))
        ],
        # showcase 1
        _t("TX-001", _ist(9, 10, 11, 2), V1, A, 480000.0, Channel.IMPS, "425310000001", "urgent refund"),
        _t("TX-002", _ist(9, 10, 11, 9), A, B, 300000.0, Channel.IMPS, "425310000002"),
        _t("TX-003", _ist(9, 10, 11, 15), A, L, 5000.0, Channel.UPI, "622210000003", "rent Sept"),
        _t("TX-004", _ist(9, 10, 11, 20), A, None, 50000.0, Channel.ATM, "ATM000000004", label="ATM PUNE-SHIVAJINAGAR-07"),
        _t("TX-005", _ist(9, 10, 11, 24), A, None, 50000.0, Channel.ATM, "ATM000000005", label="ATM PUNE-SHIVAJINAGAR-07"),
        _t("TX-006", _ist(9, 10, 11, 30), A, C, 50000.0, Channel.UPI, "622210000006"),
        _t("TX-007", _ist(9, 10, 11, 40), B, K, 1200.0, Channel.UPI, "622210000007", "order 5521"),
        _t("TX-008", _ist(9, 10, 11, 45), B, P, 20000.0, Channel.UPI, "622210000008", "wallet topup"),
        _t("TX-009", _ist(9, 10, 12, 5), B, D, 250000.0, Channel.IMPS, "425310000009"),
        _t("TX-010", _ist(9, 10, 12, 30), D, E, 200000.0, Channel.IMPS, "425310000010"),
        _t("TX-011", _ist(9, 10, 13, 10), E, None, 50000.0, Channel.ATM, "ATM000000011", label="ATM NASHIK-CBS-02"),
        # showcase 2
        _t("TX-101", _ist(9, 11, 15, 0), V2, M, 100000.0, Channel.IMPS, "425320000101", "KYC update fee"),
        _t("TX-102", _ist(9, 11, 15, 10), V3, M, 60000.0, Channel.UPI, "622220000102", "courier customs"),
        _t("TX-103", _ist(9, 11, 15, 30), M, None, 40000.0, Channel.ATM, "ATM000000103", label="ATM THANE-WEST-11"),
    ]
    complaints = [
        Complaint(
            ncrp_id=N1, reported_at=_ist(9, 10, 13, 40), victim_account=V1, victim_name="Meera Joshi",
            victim_mobile="98XXXXXX21", amount_claimed=480000.0, source="1930", category="IMPERSONATION",
            narrative_text=(
                "Caller said he was from my bank and my account would be blocked. He asked me to move money to a "
                "'safe account' HB01-1000000001 for verification. I sent Rs 4,80,000 by IMPS at 11:02."
            ),
        ),
        Complaint(
            ncrp_id=N2, reported_at=_ist(9, 11, 16, 5), victim_account=V2, victim_name="Arjun Nair",
            victim_mobile="97XXXXXX45", amount_claimed=100000.0, category="KYC_FRAUD",
            fir_number="FIR 112/2026, Cyber PS Pune", language="hi-en",
            narrative_text="KYC update ke naam pe call aaya, maine 1,00,000 bhej diye account HB01-1000000004 mein.",
        ),
        Complaint(
            ncrp_id=N3, reported_at=_ist(9, 11, 16, 20), victim_account=V3, victim_name="Fatima Shaikh",
            victim_mobile="99XXXXXX08", amount_claimed=60000.0, category="COURIER_SCAM",
            narrative_text="Told my parcel was held at customs; paid Rs 60,000 by UPI to HB01-1000000004.",
        ),
    ]
    customers = [
        Customer(customer_id="CUST-A", name="Ravi Kulkarni", mobile="90XXXXXX11", pan_masked="XXXXX1234X", kyc_tier="MIN", city="Pune"),
        Customer(customer_id="CUST-B", name="Imran Sayyed", mobile="90XXXXXX12", pan_masked="XXXXX2234X", kyc_tier="MIN", city="Pune"),
        Customer(customer_id="CUST-D", name="Sunil Pawar", mobile="90XXXXXX13", pan_masked="XXXXX3234X", kyc_tier="VKYC", city="Nashik"),
        Customer(customer_id="CUST-M", name="Deepak Rao", mobile="90XXXXXX14", pan_masked="XXXXX4234X", kyc_tier="MIN", city="Thane"),
        Customer(customer_id="CUST-L", name="Suresh Patil", mobile="94XXXXXX55", pan_masked="XXXXX5234X", kyc_tier="FULL", city="Pune"),
        Customer(customer_id="CUST-K", name="Shree Ganesh Kirana Stores", mobile="93XXXXXX66", pan_masked="XXXXX6234X", kyc_tier="FULL", city="Pune"),
    ]
    invoices = [
        Invoice(invoice_id="INV-K-5521", merchant_account=K, payer_account=B, txn_id="TX-007", amount=1200.0,
                ordered_at=_ist(9, 10, 11, 38), delivered_at=_ist(9, 10, 11, 55), order_status=OrderStatus.DELIVERED),
    ]
    signals = [
        ExternalSignal(account_id=A, fri_risk=RiskLevel.HIGH, mulehunter_score=0.82, ncrp_prior_reports=1),
        ExternalSignal(account_id=B, fri_risk=RiskLevel.MEDIUM, mulehunter_score=0.74, ncrp_prior_reports=0),
        ExternalSignal(account_id=D, fri_risk=RiskLevel.VERY_HIGH, mulehunter_score=0.91, ncrp_prior_reports=2),
        ExternalSignal(account_id=M, fri_risk=RiskLevel.HIGH, mulehunter_score=0.86, ncrp_prior_reports=2),
        ExternalSignal(account_id=L, fri_risk=RiskLevel.NONE, mulehunter_score=0.03, ncrp_prior_reports=0),
        ExternalSignal(account_id=K, fri_risk=RiskLevel.NONE, mulehunter_score=0.05, ncrp_prior_reports=0),
        ExternalSignal(account_id=C, fri_risk=RiskLevel.HIGH, mulehunter_score=0.70, ncrp_prior_reports=1),
        ExternalSignal(account_id=E, fri_risk=RiskLevel.HIGH, mulehunter_score=0.77, ncrp_prior_reports=0),
    ]
    links = [
        DeviceLink(device_id="DEV-7781", account_id=A, first_seen=_ist(9, 1, 16, 0)),
        DeviceLink(device_id="DEV-7781", account_id=B, first_seen=_ist(9, 1, 16, 5)),
        DeviceLink(device_id="DEV-7781", account_id=D, first_seen=_ist(9, 1, 16, 10)),
        DeviceLink(device_id="DEV-8890", account_id=M, first_seen=_ist(9, 5, 11, 0)),
        DeviceLink(device_id="DEV-1002", account_id=L, first_seen=datetime(2021, 3, 1, 9, 0, tzinfo=IST)),
        DeviceLink(device_id="DEV-2003", account_id=K, first_seen=datetime(2020, 2, 1, 9, 0, tzinfo=IST)),
    ]
    gst = [GstRecord(gstin=K_GSTIN, legal_name="Shree Ganesh Kirana Stores", status="ACTIVE",
                     registered_at=datetime(2019, 4, 1).date())]
    return InMemoryLedger(
        banks=banks, accounts=accounts, transactions=txns, complaints=complaints,
        complaint_txns=[(N1, "TX-001"), (N2, "TX-101"), (N3, "TX-102")],
        customers=customers, invoices=invoices, signals=signals, device_links=links, gst=gst,
    )


SHOWCASE_1 = {
    "ncrp_ids": [N1],
    "totals": {"stolen": 480000.0, "holdable": 123648.0, "cashed_out": 144000.0, "outbound": 192000.0,
               "intermediary": 19200.0, "merchant_settled": 1152.0, "unlocated": 0.0},
    "tainted_now": {A: 43200.0, B: 27648.0, D: 48000.0, L: 4800.0, K: 1152.0, P: 19200.0, C: 48000.0, E: 144000.0},
    "balance_now": {A: 45000.0, B: 28800.0, D: 50000.0, L: 155000.0, K: 51200.0, P: 50020000.0,
                    C: 50000.0, E: 150000.0},
    "layers": {V1: 0, A: 1, B: 2, L: 2, C: 2, K: 3, P: 3, D: 3, E: 4},
    "tainted_received": {A: 480000.0, B: 288000.0, L: 4800.0, C: 48000.0, K: 1152.0, P: 19200.0,
                         D: 240000.0, E: 192000.0},
    # txn_id -> (depth, tainted rupees carried); depth = layer of the receiving side (cash-outs: sender layer + 1)
    "edges": {"TX-001": (1, 480000.0), "TX-002": (2, 288000.0), "TX-003": (2, 4800.0), "TX-004": (2, 48000.0),
              "TX-005": (2, 48000.0), "TX-006": (2, 48000.0), "TX-007": (3, 1152.0), "TX-008": (3, 19200.0),
              "TX-009": (3, 240000.0), "TX-010": (4, 192000.0), "TX-011": (5, 48000.0)},
    "roles": {A: Role.MULE, B: Role.MULE, D: Role.MULE, L: Role.INNOCENT_HOLDER, K: Role.MERCHANT,
              P: Role.INTERMEDIARY, C: Role.MULE, E: Role.MULE},
    "dispositions": {A: Disposition.HOLD_AMOUNT, B: Disposition.HOLD_AMOUNT, D: Disposition.HOLD_AMOUNT,
                     L: Disposition.HOLD_AMOUNT_NO_ESCALATION, K: Disposition.NO_HOLD_UPDATE_ONLY,
                     P: Disposition.HOLD_AMOUNT_INTERMEDIARY, C: Disposition.OUTBOUND_CFCFRMS_REQUEST,
                     E: Disposition.OUTBOUND_CFCFRMS_REQUEST},
    # hold_amount per account; OUTBOUND rows carry the request amount in tainted_amount and hold 0
    "hold": {A: 43200.0, B: 27648.0, D: 48000.0, L: 4800.0, K: 0.0, P: 19200.0, C: 0.0, E: 0.0},
    # SOP 5(x): first-layer account whose balance is below the disputed amount -> forward hold on future credits
    # for the tainted money it cashed out (downstream money is held elsewhere, so it is not double counted)
    "forward_hold": {A: 96000.0},
    "total_forward_hold": 96000.0,
    "outbound_request": {C: 48000.0, E: 144000.0},
    "total_hold": 142848.0,
    "total_recoverable": 334848.0,  # home holds + outbound request amounts
    "baseline_freeze_amount": 50350000.0,  # balances of the 6 home accounts that received tainted money
    "baseline_freeze_accounts": 6,
    "over_hold_amount": 0.0,
    "process": RestorationProcess.P1,
    "str_ground": StrGround.PROCEEDS_OF_CRIME,
    "cashed_out_txns": ["TX-004", "TX-005", "TX-011"],
}

SHOWCASE_2 = {
    "ncrp_ids": [N2, N3],
    "totals": {"stolen": 160000.0, "holdable": 120000.0, "cashed_out": 40000.0, "outbound": 0.0,
               "intermediary": 0.0, "merchant_settled": 0.0, "unlocated": 0.0},
    "tainted_now_by_victim": {M: {N2: 75000.0, N3: 45000.0}},
    "balance_now": {M: 120000.0},
    "layers": {V2: 0, V3: 0, M: 1},
    "tainted_received": {M: 160000.0},
    "edges": {"TX-101": (1, 100000.0), "TX-102": (1, 60000.0), "TX-103": (2, 40000.0)},
    "edge_taint_by_victim": {"TX-103": {N2: 25000.0, N3: 15000.0}},
    "roles": {M: Role.MULE},
    "dispositions": {M: Disposition.HOLD_AMOUNT},
    "hold": {M: 120000.0},
    "forward_hold": {M: 40000.0},
    "forward_hold_by_victim": {M: {N2: 25000.0, N3: 15000.0}},
    "per_victim_recoverable": {N2: 75000.0, N3: 45000.0},
    "requires_human": {M: True},
    "reason_codes_include": {M: ["ESCALATE_S106_CANDIDATE"]},
    "baseline_freeze_amount": 120000.0,
    "baseline_freeze_accounts": 1,
    "process": RestorationProcess.P2,
}
