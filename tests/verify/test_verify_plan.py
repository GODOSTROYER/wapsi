from __future__ import annotations

import pytest

from wapsi.fixtures import showcase as sc
from wapsi.models import AccountType, Disposition, RestorationProcess, Role, VerificationReport
from wapsi.verify import PLAN_DRAFT_ID, summarize, verify_plan
from wapsi.verify.policy import freeze_instructions
from wapsi.verify.report import code_of


def failing(report: VerificationReport, severity: str = "error") -> set[str]:
    return {code_of(c) for c in report.checks if not c.passed and c.severity == severity}


def dec(bundle, acct):
    return next(d for d in bundle.plan.decisions if d.account_id == acct)


@pytest.mark.parametrize("bundle", ["b1", "b2"])
def test_showcase_plans_pass(request, ledger, bundle):
    b = request.getfixturevalue(bundle)
    report = verify_plan(b, ledger)
    assert report.passed, summarize(report)
    assert report.draft_id == PLAN_DRAFT_ID == "PLAN"
    assert failing(report, "warning") == set()


def test_plan_without_ledger_warns_on_unverifiable_escalation(b2):
    report = verify_plan(b2)
    assert report.passed
    assert failing(report, "warning") == {"escalation_unverified"}


def test_over_hold_in_plan(b1, ledger):
    dec(b1, sc.A).hold_amount = 45000.0  # balance, not tainted money (43,200)
    report = verify_plan(b1, ledger)
    assert not report.passed
    assert "over_hold" in failing(report)
    msg = next(c.message for c in report.checks if code_of(c) == "over_hold")
    assert "₹43,200.00" in msg


def test_over_hold_reported_by_plan_field(b1, ledger):
    b1.plan.over_hold_amount = 100.0
    assert "over_hold" in failing(verify_plan(b1, ledger))


def test_whole_balance_hold_on_innocent_holder(b1, ledger):
    d = dec(b1, sc.L)
    d.hold_amount = 155000.0
    b1.plan.total_hold += 155000.0 - 4800.0
    b1.plan.total_recoverable += 155000.0 - 4800.0
    assert "over_hold" in failing(verify_plan(b1, ledger))


def test_forward_hold_on_layer_2(b1, ledger):
    dec(b1, sc.B).forward_hold_amount = 10000.0
    b1.plan.total_forward_hold += 10000.0
    report = verify_plan(b1, ledger)
    assert failing(report) == {"forward_hold_not_first_layer"}


def test_forward_hold_disposition_on_layer_3(b1, ledger):
    d = dec(b1, sc.D)
    d.disposition = Disposition.FORWARD_HOLD
    d.hold_amount = 0.0
    assert "forward_hold_not_first_layer" in failing(verify_plan(b1, ledger))


def test_showcase_pool_account_holds_the_disputed_amount(b1, ledger):
    d = dec(b1, sc.P)
    assert d.disposition == Disposition.HOLD_AMOUNT_INTERMEDIARY and d.hold_amount == 19200.0
    assert verify_plan(b1, ledger).passed


def test_hold_amount_disposition_on_pool_account(b1, ledger):
    dec(b1, sc.P).disposition = Disposition.HOLD_AMOUNT  # right amount, wrong disposition
    report = verify_plan(b1, ledger)
    assert not report.passed
    assert failing(report) == {"intermediary_disposition"}
    msg = next(c.message for c in report.checks if code_of(c) == "intermediary_disposition")
    assert "[SOP-9-1-III]" in msg and "[SOP-5-X]" not in msg


def test_pool_type_is_taken_from_the_trace_not_the_plan(b1, ledger):
    # even if the plan mislabels the pool account's role, the trace says POOL
    d = dec(b1, sc.P)
    d.role = Role.MULE
    d.disposition = Disposition.HOLD_AMOUNT
    b1.classifications.pop(sc.P)
    assert "intermediary_disposition" in failing(verify_plan(b1, ledger))


def test_pool_account_over_hold(b1, ledger):
    d = dec(b1, sc.P)
    d.hold_amount = 50_000.0  # the pool balance is ₹5,00,20,000 but only ₹19,200 is disputed
    b1.plan.total_hold += 50_000.0 - 19_200.0
    b1.plan.total_recoverable += 50_000.0 - 19_200.0
    assert "over_hold" in failing(verify_plan(b1, ledger))


def test_pool_account_left_unheld(b1, ledger):
    d = dec(b1, sc.P)
    d.disposition = Disposition.NO_ACTION
    d.hold_amount = 0.0
    d.per_victim = {}
    b1.plan.total_hold -= 19_200.0
    b1.plan.total_recoverable -= 19_200.0
    b1.plan.per_victim_recoverable = {sc.N1: b1.plan.total_recoverable}
    assert failing(verify_plan(b1, ledger)) == {"intermediary_disposition"}


def test_forward_hold_on_pool_account(b1, ledger):
    dec(b1, sc.P).forward_hold_amount = 1000.0
    b1.plan.total_forward_hold += 1000.0
    report = verify_plan(b1, ledger)
    assert failing(report) == {"forward_hold_on_intermediary", "forward_hold_not_first_layer"}
    msg = next(c.message for c in report.checks if code_of(c) == "forward_hold_on_intermediary")
    assert "[SOP-9-1-I]" in msg


def test_pool_account_never_flagged_for_escalation(b1, ledger):
    d = dec(b1, sc.P)
    d.reason_codes.append("ESCALATE_S106_CANDIDATE")
    d.requires_human = True
    assert "intermediary_escalated" in failing(verify_plan(b1, ledger))


def test_intermediary_disposition_only_for_intermediaries(b1, ledger):
    dec(b1, sc.B).disposition = Disposition.HOLD_AMOUNT_INTERMEDIARY
    assert failing(verify_plan(b1, ledger)) == {"intermediary_disposition_misused"}


def test_other_bank_pool_account_is_an_outbound_request(b1, ledger):
    st = b1.trace.accounts[sc.C]
    st.account_type = AccountType.POOL
    b1.classifications[sc.C].role = Role.INTERMEDIARY
    dec(b1, sc.C).role = Role.INTERMEDIARY
    assert verify_plan(b1, ledger).passed  # OUTBOUND_CFCFRMS_REQUEST: the other bank holds the amount
    dec(b1, sc.C).disposition = Disposition.HOLD_AMOUNT_INTERMEDIARY
    assert "intermediary_disposition" in failing(verify_plan(b1, ledger))


def test_delivered_merchant_held(b1, ledger):
    d = dec(b1, sc.K)
    d.disposition = Disposition.HOLD_AMOUNT
    d.hold_amount = 1152.0
    report = verify_plan(b1, ledger)
    assert "delivered_merchant_held" in failing(report)
    # without the ledger the invoice cannot be checked, so this rule cannot fire
    assert "delivered_merchant_held" not in failing(verify_plan(b1))


def test_update_only_with_hold(b1, ledger):
    dec(b1, sc.K).hold_amount = 1000.0
    assert {"update_only_with_hold", "hold_without_holding_disposition"} <= failing(verify_plan(b1, ledger))


def test_innocent_holder_flagged_for_escalation(b1, ledger):
    d = dec(b1, sc.L)
    d.reason_codes.append("ESCALATE_S106_CANDIDATE")
    d.requires_human = True
    assert "innocent_escalated" in failing(verify_plan(b1, ledger))


def test_innocent_by_classification_even_if_plan_says_mule(b1, ledger):
    d = dec(b1, sc.L)
    d.role = Role.MULE
    d.disposition = Disposition.HOLD_AMOUNT
    d.reason_codes.append("ESCALATE_S106_CANDIDATE")
    d.requires_human = True
    report = verify_plan(b1, ledger)
    assert "innocent_escalated" in failing(report)
    assert "innocent_disposition" in failing(report, "warning")


def test_escalation_without_fir(b2, ledger):
    b2.complaints[0].fir_number = None
    assert "escalation_without_fir" in failing(verify_plan(b2, ledger))


def test_escalation_without_enough_prior_reports(b1, ledger):
    d = dec(b1, sc.A)  # A has 1 prior report and no FIR
    d.reason_codes.append("ESCALATE_S106_CANDIDATE")
    d.requires_human = True
    assert {"escalation_without_fir", "escalation_insufficient_reports"} <= failing(verify_plan(b1, ledger))


def test_escalation_needs_human_and_hold_amount(b2, ledger):
    d = dec(b2, sc.M)
    d.requires_human = False
    d.disposition = Disposition.ESCALATE_S106_CANDIDATE
    assert {"escalation_without_human", "reserved_disposition"} <= failing(verify_plan(b2, ledger))


def test_p2_required_for_two_victims(b2, ledger):
    b2.plan.restoration_process = RestorationProcess.P1
    assert "restoration_process" in failing(verify_plan(b2, ledger))


def test_p2_with_single_victim_is_a_warning(b1, ledger):
    b1.plan.restoration_process = RestorationProcess.P2
    report = verify_plan(b1, ledger)
    assert report.passed and "restoration_process" in failing(report, "warning")


def test_per_victim_recomputed_from_decisions(b2, ledger):
    b2.plan.per_victim_recoverable = {sc.N2: 120000.0}
    assert "per_victim_mismatch" in failing(verify_plan(b2, ledger))


def test_per_victim_exceeding_victim_taint(b2, ledger):
    dec(b2, sc.M).per_victim = {sc.N2: 100000.0, sc.N3: 20000.0}
    b2.plan.per_victim_recoverable = {sc.N2: 100000.0, sc.N3: 20000.0}
    assert "per_victim_exceeds_taint" in failing(verify_plan(b2, ledger))


def test_plan_totals(b1, ledger):
    b1.plan.total_hold = 200000.0
    assert "plan_total_mismatch" in failing(verify_plan(b1, ledger))


def test_outbound_rows(b1, ledger):
    d = dec(b1, sc.E)
    d.hold_amount = 10.0
    d.tainted_amount = 150000.0
    codes = failing(verify_plan(b1, ledger))
    assert {"outbound_with_hold", "outbound_over_request"} <= codes


def test_victim_account_in_plan(b1, ledger):
    d = dec(b1, sc.A).model_copy(
        update={
            "account_id": sc.V1,
            "hold_amount": 0.0,
            "forward_hold_amount": 0.0,
            "per_victim": {},
            "disposition": Disposition.NO_ACTION,
        }
    )
    b1.plan.decisions.append(d)
    assert "victim_in_plan" in failing(verify_plan(b1, ledger))


def test_duplicate_and_unknown_decisions(b1, ledger):
    b1.plan.decisions.append(dec(b1, sc.K).model_copy())
    b1.plan.decisions.append(dec(b1, sc.K).model_copy(update={"account_id": "HB01-1999999999"}))
    codes = failing(verify_plan(b1, ledger))
    assert {"duplicate_decision", "decision_unknown_account"} <= codes


def test_undecided_account_warning(b1, ledger):
    b1.plan.decisions = [d for d in b1.plan.decisions if d.account_id != sc.K]
    assert "undecided_accounts" in failing(verify_plan(b1, ledger), "warning")


def test_missing_plan(b1):
    b1.plan = None
    report = verify_plan(b1)
    assert not report.passed and failing(report) == {"no_plan"}


def test_freeze_instruction_detector():
    assert freeze_instructions("Freeze the whole account now.") == ["Freeze the whole account now."]
    assert freeze_instructions("Freeze it immediately.")  # no amount, no negation: treated as whole-account
    assert freeze_instructions("Hold ₹43,200.00 (a freeze of that amount only).") == []
    assert freeze_instructions("Without delay, freeze the entire account.")
    assert freeze_instructions("Compared with a full freeze of ₹5,03,50,000.00, the plan holds less.") == []
    assert freeze_instructions("Do not, under any circumstances, freeze the account.") == []


def test_unvalidated_string_enums_are_tolerated(b2, ledger, restore_2):
    from wapsi.verify import verify_draft

    b2.plan.restoration_process = "P1"  # assignment skips pydantic validation
    assert "restoration_process" in failing(verify_plan(b2, ledger))
    assert "slot_mismatch" in failing(verify_draft(restore_2, b2, ledger))
