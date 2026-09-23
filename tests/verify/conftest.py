"""Fixtures: showcase bundles, ledger and hand-rendered drafts (see drafts.py)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from wapsi.fixtures import showcase as sc
from wapsi.fixtures.bundles import showcase_bundle
from wapsi.models import CaseBundle, Draft, DraftKind

from .drafts import (
    CREATED_2,
    EDD_L_BODY,
    EDD_L_SLOTS,
    HOLD_A_BODY,
    HOLD_A_SLOTS,
    HOLD_M_BODY,
    HOLD_M_SLOTS,
    RESTORE_BODY,
    RESTORE_SLOTS,
    STR_BODY,
    STR_SLOTS,
    STR_STRUCTURED,
    make,
)


@pytest.fixture(scope="session")
def ledger():
    return sc.showcase_ledger()


@pytest.fixture
def b1() -> CaseBundle:
    return showcase_bundle(1)


@pytest.fixture
def b2() -> CaseBundle:
    return showcase_bundle(2)


@pytest.fixture
def hold_a(b1) -> Draft:
    return make(
        "D-HOLD-A",
        b1.case.case_id,
        DraftKind.HOLD_INSTRUCTION,
        sc.A,
        "Hold on the disputed amount in account ⟦account_id⟧",
        HOLD_A_BODY,
        HOLD_A_SLOTS,
        ["SOP-9-1", "SOP-ANNEX-I", "SOP-5-IX", "SOP-9-1-I"],
    )


@pytest.fixture
def edd_l(b1) -> Draft:
    return make(
        "D-EDD-L",
        b1.case.case_id,
        DraftKind.EDD_MEMO,
        sc.L,
        "Enhanced due diligence: account ⟦account_id⟧ (⟦holder_name⟧)",
        EDD_L_BODY,
        EDD_L_SLOTS,
        ["SOP-5-XII", "SOP-10-1", "SOP-9-1"],
    )


@pytest.fixture
def str_draft(b1) -> Draft:
    return make(
        "D-STR",
        b1.case.case_id,
        DraftKind.STR,
        None,
        "Suspicious Transaction Report: NCRP ⟦ncrp_ids⟧",
        STR_BODY,
        STR_SLOTS,
        ["PMLR-2-1-G", "PMLR-8"],
        structured=STR_STRUCTURED,
    )


@pytest.fixture
def restore_2(b2) -> Draft:
    return make(
        "D-REST-2",
        b2.case.case_id,
        DraftKind.RESTORATION_PACK,
        None,
        "Restoration pack: NCRP ⟦ncrp_ids⟧",
        RESTORE_BODY,
        RESTORE_SLOTS,
        ["SOP-5-XIII", "SOP-ANNEX-V", "SOP-11-P2"],
        created=CREATED_2,
    )


@pytest.fixture
def hold_m(b2) -> Draft:
    return make(
        "D-HOLD-M",
        b2.case.case_id,
        DraftKind.HOLD_INSTRUCTION,
        sc.M,
        "Hold on the disputed amount in account ⟦account_id⟧",
        HOLD_M_BODY,
        HOLD_M_SLOTS,
        ["SOP-9-1", "SOP-5-XIII", "SOP-9-1-I"],
        created=CREATED_2,
    )


@pytest.fixture
def with_body() -> Callable[..., Draft]:
    def _with(draft: Draft, body: str | None = None, **update) -> Draft:
        if body is not None:
            update["body"] = body
        return draft.model_copy(update=update, deep=True)

    return _with
