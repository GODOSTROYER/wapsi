"""Hand-rendered drafts for the showcase bundles (docs/draft-contract.md), independent of ``wapsi.draft``.

Slot values are written out literally from the numbers in ``wapsi.fixtures.showcase``.
"""

from __future__ import annotations

from datetime import datetime

from wapsi.fixtures import showcase as sc
from wapsi.models import Draft, DraftKind
from wapsi.timeutil import IST

CREATED = datetime(2026, 9, 10, 14, 0, tzinfo=IST)
CREATED_2 = datetime(2026, 9, 11, 17, 0, tzinfo=IST)
N1, N2, N3 = sc.N1, sc.N2, sc.N3


def render(template: str, slots: dict[str, str]) -> str:
    out = template
    for key, value in slots.items():
        out = out.replace(f"⟦{key}⟧", value)
    assert "⟦" not in out, out
    return out


def make(
    draft_id: str,
    case_id: str,
    kind: DraftKind,
    account_id: str | None,
    title: str,
    template: str,
    slots: dict[str, str],
    cites: list[str],
    structured: dict | None = None,
    created: datetime = CREATED,
) -> Draft:
    return Draft(
        draft_id=draft_id,
        case_id=case_id,
        kind=kind,
        account_id=account_id,
        title=render(title, slots),
        body=render(template, slots),
        slots=dict(slots),
        # written by hand, not by wapsi.draft: no skeleton, so the verifier falls back to any-slot amount checks
        structured={"hand_written": True, **(structured or {})},
        citation_ids=cites,
        created_at=created,
    )


HOLD_A_SLOTS = {
    "account_id": sc.A,
    "bank_id": "HB01",
    "holder_name": "Ravi Kulkarni",
    "disposition": "HOLD_AMOUNT",
    "hold_amount": "₹43,200.00",
    "forward_hold_amount": "₹96,000.00",
    "tainted_amount": "₹43,200.00",
    "balance_now": "₹45,000.00",
    "disputed_amount": "₹4,80,000.00",
    "utrs": "425310000001",
    "inflow_dates": "10-09-2026",
    "ncrp_ids": N1,
    f"victim_amount:{N1}": "₹43,200.00",
    f"reported_at:{N1}": "10-09-2026 13:40 IST",
    f"complaint_amount:{N1}": "₹4,80,000.00",
}

HOLD_A_BODY = f"""To: Branch Manager, ⟦bank_id⟧
With reference to NCRP acknowledgement ⟦ncrp_ids⟧ (reported ⟦reported_at:{N1}⟧, amount \
⟦complaint_amount:{N1}⟧), please place a hold (lien) of ⟦hold_amount⟧ on account ⟦account_id⟧ held by \
⟦holder_name⟧ under Para 9.1 of the SOP [SOP-9-1] and the notice at Annexure-I [SOP-ANNEX-I].
The account received ⟦disputed_amount⟧ of disputed money on ⟦inflow_dates⟧ (UTR ⟦utrs⟧). Tainted money still in \
the account is ⟦tainted_amount⟧ against a balance of ⟦balance_now⟧, and the hold for complaint {N1} is \
⟦victim_amount:{N1}⟧.
This is an amount-specific hold, not a freeze of the account: all other operations continue [SOP-5-IX].
As a first-layer account, also mark a forward hold of ⟦forward_hold_amount⟧ on future credits [SOP-9-1-I].
"""

EDD_L_SLOTS = {
    "account_id": sc.L,
    "holder_name": "Suresh Patil",
    "disposition": "HOLD_AMOUNT_NO_ESCALATION",
    "hold_amount": "₹4,800.00",
    "disputed_amount": "₹4,800.00",
    "balance_now": "₹1,55,000.00",
    "utrs": "622210000003",
    "inflow_dates": "10-09-2026",
    "ncrp_ids": N1,
}

EDD_L_BODY = """The account received ⟦disputed_amount⟧ (UTR ⟦utrs⟧, ⟦inflow_dates⟧) from a first-layer account \
in NCRP ⟦ncrp_ids⟧, labelled as rent and consistent with its history. It may have been operated without the \
holder's knowledge or connivance [SOP-5-XII].
Disposition ⟦disposition⟧: a hold of ⟦hold_amount⟧ only, with the rest of the balance of ⟦balance_now⟧ \
available to the holder. No escalation and no STR reporting for this holder. Complete enhanced due diligence \
within the grievance timeline of Para 10.1 [SOP-10-1]; the hold itself rests on Para 9.1 [SOP-9-1].
"""

STR_SLOTS = {
    "ncrp_ids": N1,
    "str_ground": "PROCEEDS_OF_CRIME",
    "total_stolen": "₹4,80,000.00",
    "total_cashed_out": "₹1,44,000.00",
    "total_outbound": "₹1,92,000.00",
    "total_hold": "₹1,42,848.00",
    f"complaint_amount:{N1}": "₹4,80,000.00",
    f"reported_at:{N1}": "10-09-2026 13:40 IST",
}

STR_BODY = f"""Ground of suspicion: ⟦str_ground⟧ (proceeds of crime) under Rule 2(1)(g) [PMLR-2-1-G].
The victim reported a loss of ⟦complaint_amount:{N1}⟧ on ⟦reported_at:{N1}⟧. Of the ⟦total_stolen⟧ traced, \
⟦total_cashed_out⟧ was withdrawn in cash (UTR ATM000000004, ATM000000005, ATM000000011) and ⟦total_outbound⟧ \
moved to accounts at other banks; ⟦total_hold⟧ is on hold at this bank.
Accounts reported: {sc.A}, {sc.B}, {sc.D}. They received the victim's money and moved it on within minutes \
(UTR 425310000001, 425310000002, 425310000009).
Filed within seven working days of forming the suspicion [PMLR-8].
"""

STR_STRUCTURED = {
    "report_type": "STR",
    "reporting_entity": "HB01",
    "grounds_of_suspicion": ["PROCEEDS_OF_CRIME"],
    "linked_complaints": [N1],
    "accounts": [
        {"account_id": sc.A, "role": "MULE", "amount_held": 43200.0},
        {"account_id": sc.B, "role": "MULE", "amount_held": 27648.0},
        {"account_id": sc.D, "role": "MULE", "amount_held": 48000.0},
    ],
    "transactions": [
        {"utr": u} for u in ["425310000001", "425310000002", "425310000009", "ATM000000004", "ATM000000005"]
    ],
}

RESTORE_SLOTS = {
    "ncrp_ids": f"{N2}, {N3}",
    "restoration_process": "P2",
    "total_stolen": "₹1,60,000.00",
    "total_cashed_out": "₹40,000.00",
    "total_recoverable": "₹1,20,000.00",
    f"victim_amount:{N2}": "₹75,000.00",
    f"victim_amount:{N3}": "₹45,000.00",
    f"complaint_amount:{N2}": "₹1,00,000.00",
    f"complaint_amount:{N3}": "₹60,000.00",
    f"reported_at:{N2}": "11-09-2026 16:05 IST",
    f"reported_at:{N3}": "11-09-2026 16:20 IST",
}

RESTORE_BODY = f"""Restoration under Process ⟦restoration_process⟧ for NCRP ⟦ncrp_ids⟧.
Two victims lost ⟦total_stolen⟧ to account {sc.M}; ⟦total_cashed_out⟧ was withdrawn in cash \
(UTR ATM000000103). The ⟦total_recoverable⟧ on hold is to be restored pro-rata [SOP-5-XIII] [SOP-ANNEX-V] \
under Process 2 [SOP-11-P2]:
- NCRP {N2} (reported ⟦reported_at:{N2}⟧, claimed ⟦complaint_amount:{N2}⟧): ⟦victim_amount:{N2}⟧
- NCRP {N3} (reported ⟦reported_at:{N3}⟧, claimed ⟦complaint_amount:{N3}⟧): ⟦victim_amount:{N3}⟧
"""

HOLD_M_SLOTS = {
    "account_id": sc.M,
    "holder_name": "Deepak Rao",
    "disposition": "HOLD_AMOUNT",
    "hold_amount": "₹1,20,000.00",
    "forward_hold_amount": "₹40,000.00",
    "disputed_amount": "₹1,60,000.00",
    "utrs": "425320000101, 622220000102",
    "inflow_dates": "11-09-2026",
    f"victim_amount:{N2}": "₹75,000.00",
    f"victim_amount:{N3}": "₹45,000.00",
}

HOLD_M_BODY = f"""Please place a hold of ⟦hold_amount⟧ on account ⟦account_id⟧ (⟦holder_name⟧) [SOP-9-1]. It \
received ⟦disputed_amount⟧ on ⟦inflow_dates⟧ (UTR ⟦utrs⟧): ⟦victim_amount:{N2}⟧ for NCRP {N2} and \
⟦victim_amount:{N3}⟧ for NCRP {N3}, attributed pro-rata [SOP-5-XIII].
Mark a forward hold of ⟦forward_hold_amount⟧ on future credits [SOP-9-1-I]. The account is not to be frozen.
"""
