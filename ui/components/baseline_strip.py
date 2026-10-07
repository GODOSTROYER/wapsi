"""The key comparison: the whole-account freeze baseline vs Wapsi's amount-specific holds (SOP Para 5(ix), 9.1).

The baseline is a comparison only (what freezing every touched account would immobilise), never a recommendation:
holds are amount-specific by default, and account-level action is only a recorded, supervisor-approved exception.
"""

from __future__ import annotations

from dataclasses import dataclass

from ui.components.formatting import inr, inr_short, ratio_text
from ui.components.html import esc
from wapsi.models import HOLDING_DISPOSITIONS, HoldPlan
from wapsi.money import eq


@dataclass(frozen=True)
class BaselineSummary:
    freeze_amount: float
    freeze_accounts: int
    hold_amount: float
    hold_accounts: int

    @property
    def ratio(self) -> float | None:
        return self.freeze_amount / self.hold_amount if self.hold_amount > 0 else None

    @property
    def spared(self) -> float:
        return max(self.freeze_amount - self.hold_amount, 0.0)

    @property
    def sentence(self) -> str:
        return baseline_sentence(self)


def _accounts(n: int) -> str:
    return f"{n} account{'s' if n != 1 else ''}"


def summarize(plan: HoldPlan) -> BaselineSummary:
    held = [d for d in plan.decisions if d.disposition in HOLDING_DISPOSITIONS and d.hold_amount > 0]
    return BaselineSummary(
        freeze_amount=plan.baseline_freeze_amount,
        freeze_accounts=plan.baseline_freeze_accounts,
        hold_amount=plan.total_hold,
        hold_accounts=len(held),
    )


def baseline_sentence(s: BaselineSummary) -> str:
    if s.hold_amount <= 0:
        return (f"Whole-account freeze baseline: {inr_short(s.freeze_amount)} across "
                f"{_accounts(s.freeze_accounts)}; Wapsi recommends no hold in this case.")
    if eq(s.freeze_amount, s.hold_amount):
        return (f"Whole-account freeze baseline: {inr_short(s.freeze_amount)} across "
                f"{_accounts(s.freeze_accounts)}; Wapsi holds the same {inr_short(s.hold_amount)} across "
                f"{_accounts(s.hold_accounts)} — here every rupee in the account is victim money.")
    return (f"Whole-account freeze baseline: {inr_short(s.freeze_amount)} across "
            f"{_accounts(s.freeze_accounts)}; Wapsi holds {inr_short(s.hold_amount)} across "
            f"{s.hold_accounts} — only the victim's money")


def baseline_strip_html(s: BaselineSummary) -> str:
    ratio = s.ratio
    if ratio is not None and ratio >= 1.05:
        mid = f"<b>{esc(ratio_text(s.freeze_amount, s.hold_amount))}</b>less money immobilised"
    elif ratio is not None:
        mid = "<b>=</b>same amount"
    else:
        mid = "<b>0</b>holds recommended"
    freeze_title = f"{inr(s.freeze_amount)} — balances of every account that received victim money"
    hold_title = f"{inr(s.hold_amount)} — the victim money still in held accounts"
    return (
        '<div class="wp-strip" role="group" aria-label="Whole-account freeze baseline compared with Wapsi holds">'
        f'<div class="wp-strip-side wp-strip-bad" title="{esc(freeze_title)}">'
        '<div class="wp-strip-k">Whole-account freeze baseline (comparison only)</div>'
        f'<div class="wp-strip-v">{esc(inr_short(s.freeze_amount))}</div>'
        f'<div class="wp-strip-n">across {esc(_accounts(s.freeze_accounts))}</div></div>'
        f'<div class="wp-strip-mid">{mid}</div>'
        f'<div class="wp-strip-side wp-strip-good" style="text-align:right" title="{esc(hold_title)}">'
        '<div class="wp-strip-k">Wapsi holds</div>'
        f'<div class="wp-strip-v">{esc(inr_short(s.hold_amount))}</div>'
        f'<div class="wp-strip-n">across {esc(_accounts(s.hold_accounts))}</div></div>'
        f'<div class="wp-strip-msg">{esc(s.sentence)} '
        '<span class="wp-note">Amount-specific holds by default (SOP Para 5(ix) and 9.1); account-level action '
        "only as a recorded, supervisor-approved exception.</span></div>"
        "</div>"
    )
