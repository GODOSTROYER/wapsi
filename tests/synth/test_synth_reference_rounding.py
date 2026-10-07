"""The independent reference applies the per-victim loss cap (r9) the way the hold plan does."""

from __future__ import annotations

from wapsi.models import Disposition, Role
from wapsi.synth.reference import RefDecision, _cap_at_loss


def _dec(acct: str, disp: Disposition, amount: float, per: dict[str, float], layer: int = 1) -> RefDecision:
    outbound = disp == Disposition.OUTBOUND_CFCFRMS_REQUEST
    return RefDecision(account_id=acct, bank_id=acct[:4], disposition=disp,
                       hold_amount=0.0 if outbound else amount, forward_hold_amount=0.0,
                       tainted_amount=amount, balance_now=amount, layer=layer,
                       role=Role.MULE, per_victim=dict(per), forward_per_victim={}, reason_codes=[disp.value],
                       requires_human=False)


def test_thirds_lose_the_excess_paisa_from_the_first_largest_share():
    decs = [_dec(a, Disposition.HOLD_AMOUNT, 6666.67, {"N1": 6666.67}) for a in ("HB01-A", "HB01-B", "HB01-C")]
    assert _cap_at_loss(decs, {"N1": 20_000.0}, ["N1"]) == {"N1": 0.01}
    assert [d.hold_amount for d in decs] == [6666.66, 6666.67, 6666.67]
    assert decs[0].per_victim == {"N1": 6666.66}
    assert sum(d.per_victim["N1"] for d in decs) == 20_000.0


def test_outbound_requests_are_trimmed_in_tainted_amount_and_within_loss_is_untouched():
    decs = [
        _dec("HB01-1", Disposition.HOLD_AMOUNT, 100.01, {"N1": 50.0, "N2": 50.01}),
        _dec("OB03-1", Disposition.OUTBOUND_CFCFRMS_REQUEST, 50.01, {"N1": 50.01}),
        _dec("HB01-2", Disposition.NO_HOLD_UPDATE_ONLY, 0.0, {"N1": 9.0}),  # not recoverable: never counted
    ]
    assert _cap_at_loss(decs, {"N1": 100.0, "N2": 50.01}, ["N1", "N2"]) == {"N1": 0.01}
    assert (decs[0].hold_amount, decs[0].per_victim) == (100.01, {"N1": 50.0, "N2": 50.01})
    assert (decs[1].hold_amount, decs[1].tainted_amount, decs[1].per_victim) == (0.0, 50.0, {"N1": 50.0})
    assert decs[2].per_victim == {"N1": 9.0}


def test_no_amount_goes_to_zero_and_unknown_victims_are_left_alone():
    decs = [_dec("HB01-1", Disposition.HOLD_AMOUNT, 0.01, {"N1": 0.01}),
            _dec("HB01-2", Disposition.HOLD_AMOUNT, 0.01, {"N1": 0.01})]
    assert _cap_at_loss(decs, {"N1": 0.0}, ["N1"]) == {}
    assert _cap_at_loss(decs, {}, ["N1"]) == {}
    assert [d.hold_amount for d in decs] == [0.01, 0.01]
