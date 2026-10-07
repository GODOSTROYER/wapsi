"""Regulatory change radar: the curated registry and "what changes for this case" on the showcase bundles."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from wapsi.citations import KNOWN_IDS
from wapsi.domain.regradar import (
    Radar,
    case_impact,
    clause_impact,
    load_radar,
    load_radar_cached,
    validate_radar,
)
from wapsi.fixtures import showcase as sc
from wapsi.fixtures.bundles import showcase_bundle
from wapsi.models import Disposition, DraftKind, Role
from wapsi.money import eq
from wapsi.search.normalize import quote_word_count
from wapsi.search.sources import SOURCES
from wapsi.timeutil import IST

CORPUS_IDS = {s.id for s in SOURCES}
BEFORE_MAX = datetime(2026, 9, 16, 11, 0, tzinfo=IST)  # day 5 of the showcase-1 holds
AFTER_MAX = datetime(2026, 11, 20, 11, 0, tzinfo=IST)  # day 70: past the draft's 60 days, before the SOP's 90


@pytest.fixture(scope="module")
def radar() -> Radar:
    return load_radar()


@pytest.fixture
def b1():
    return showcase_bundle(1)


@pytest.fixture
def b2():
    return showcase_bundle(2)


def _impact(radar: Radar, bundle, now, clause_id: str):
    return clause_impact(radar.clause(clause_id), bundle, now)


def _copy(radar: Radar) -> Radar:
    return radar.model_copy(deep=True)


# --------------------------------------------------------------------------- registry


def test_registry_is_valid_and_every_row_verified(radar):
    assert validate_radar(radar, CORPUS_IDS) == []
    assert radar.baseline_source == "sop-2026"
    assert radar.clauses and all(c.verified for c in radar.clauses)
    assert radar.visible() == radar.clauses
    for c in radar.clauses:
        assert c.review.status == "pending"  # curated rows wait for a human; nothing is self-approved
        for _, side in c.sides():
            assert quote_word_count(side.quote) <= 25, (c.id, side.quote)
            src = radar.source(side.source_id)
            assert src is not None
            if src.type == "pdf" or side.source_id in {"sop-2026"}:
                assert isinstance(side.page, int) and side.page > 0, (c.id, side.source_id)


def test_registry_covers_the_candidate_clauses(radar):
    topics = {c.topic for c in radar.clauses}
    for topic in ("scope", "hold_duration", "intermediary_accounts", "account_level_hold", "grievance",
                  "minimum_amount", "mule_definition_str", "frm_directions"):
        assert topic in topics, topic


def test_sources_have_status_dates_and_urls(radar):
    by_id = {s.id: s for s in radar.sources}
    draft = by_id["rbi-kyc-mule-draft-2026"]
    assert draft.status == "draft" and not draft.corpus
    assert str(draft.issued) == "2026-09-11" and str(draft.comments_close) == "2026-10-02"
    assert str(draft.effective) == "2027-04-01"
    assert draft.url.startswith("https://rbidocs.rbi.org.in/") and "prid=505" in draft.landing_url
    assert by_id["rbi-frm-dir-2026"].status == "in-force" and by_id["rbi-frm-dir-2026"].corpus
    assert by_id["rbi-frm-md-2024"].status == "repealed"
    assert by_id["sop-2026"].corpus
    # every date / status claim is backed by a verbatim quote on the source itself
    for sid in ("rbi-kyc-mule-draft-2026", "rbi-frm-dir-2026", "rbi-dos-repeal-2026"):
        assert by_id[sid].evidence, sid


def test_check_parameters_come_from_the_quotes(radar):
    rr02 = radar.clause("RR-02")
    assert rr02.check.kind == "hold_age_vs_max" and rr02.check.days == 60
    assert "60 days" in rr02.new[0].quote
    assert any("90 calendar days" in s.quote for s in rr02.baseline)
    rr07 = radar.clause("RR-07")
    assert rr07.check.kind == "below_floor" and rr07.check.amount == 1000
    assert "₹ 1000" in rr07.new[0].quote


def test_frm_repeal_row_does_not_claim_para_67_names_the_2024_directions(radar):
    rr11 = radar.clause("RR-11")
    para67 = next(s for s in rr11.new if s.para == "67")
    assert "Master Directions" not in para67.quote and "2024" not in para67.quote
    assert {s.source_id for s in rr11.new} >= {"rbi-frm-dir-2026", "rbi-dos-repeal-2026", "rbi-withdrawn-circulars"}
    assert rr11.check.retired_citation_ids == ["RBI-FRM-MD-2024"]
    assert "RBI-FRM-MD-2024" not in KNOWN_IDS


def test_affected_citations_are_registered(radar):
    for c in radar.clauses:
        for cid in c.affects.citation_ids:
            assert cid in KNOWN_IDS, (c.id, cid)


def test_unverified_rows_are_hidden_by_default(radar):
    r = _copy(radar)
    r.clauses[0].verification.status = "unverified"
    assert r.clauses[0] not in r.visible()
    assert r.clauses[0] in r.visible(include_unverified=True)
    impacts = case_impact(r, showcase_bundle(1), BEFORE_MAX)
    assert r.clauses[0].id not in {i.clause_id for i in impacts}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: setattr(r.clause("RR-02").check, "days", 45), "check.days=45 does not appear"),
        (lambda r: setattr(r.clause("RR-07").check, "amount", 500.0), "check.amount=500 does not appear"),
        (lambda r: setattr(r.clause("RR-01").new[0], "quote", " ".join(["word"] * 26)), "26 words"),
        (lambda r: setattr(r.clause("RR-01").new[0], "source_id", "nowhere"), "unknown source nowhere"),
        (lambda r: r.clause("RR-01").affects.citation_ids.append("SOP-99"), "unknown citation id SOP-99"),
        (lambda r: r.clause("RR-11").check.retired_citation_ids.append("SOP-6"), "retired citation id SOP-6"),
        (lambda r: setattr(r.clause("RR-01").review, "status", "approved"), "needs reviewed_by"),
        (lambda r: setattr(r.clause("RR-10"), "baseline_note", ""), "no baseline quote and no baseline_note"),
        (lambda r: r.clauses.append(r.clauses[0].model_copy(deep=True)), "duplicate clause id RR-01"),
        (lambda r: setattr(r, "baseline_source", "nope"), "baseline_source nope"),
    ],
)
def test_validator_catches_problems(radar, mutate, message):
    r = _copy(radar)
    mutate(r)
    problems = validate_radar(r, CORPUS_IDS)
    assert any(message in p for p in problems), problems


def test_corpus_flag_must_match_the_search_sources(radar):
    r = _copy(radar)
    r.source("rbi-c2r-505").corpus = True
    assert any("marked corpus but not in wapsi.search.sources" in p for p in validate_radar(r, CORPUS_IDS))


def test_load_radar_cached_missing_file(tmp_path):
    assert load_radar_cached(tmp_path / "missing.json") is None


def test_load_rejects_unknown_fields(tmp_path, radar):
    data = json.loads(radar.model_dump_json())
    data["clauses"][0]["surprise"] = 1
    p = tmp_path / "reg_radar.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_radar(p)


# --------------------------------------------------------------------------- what changes for this case


def test_lea_backed_rows_restate_the_showcase_holds(radar, b1):
    imp = _impact(radar, b1, BEFORE_MAX, "RR-01")
    assert imp.applies and imp.severity == "info"
    held = {f.account_id: f.amount for f in imp.findings}
    assert held == {sc.A: 43200.0, sc.B: 27648.0, sc.D: 48000.0, sc.L: 4800.0, sc.P: 19200.0}
    assert eq(sum(held.values()), 142848.0)
    assert "₹1,42,848.00" in imp.summary and sc.N1 in imp.summary


def test_hold_age_within_the_draft_maximum(radar, b1):
    imp = _impact(radar, b1, BEFORE_MAX, "RR-02")
    assert imp.applies and imp.severity == "info"
    assert len(imp.findings) == 5
    assert imp.summary == "All 5 holds are within the RBI DRAFT's 60-day maximum today."
    for f in imp.findings:
        start, cap, lapse = f.dates["start"], f.dates["draft_max"], f.dates["sop_lapse"]
        assert timedelta(days=60) <= cap - start < timedelta(days=61)
        assert timedelta(days=90) <= lapse - start < timedelta(days=91)
        assert "day 5 of 60" in f.text


def test_holds_older_than_the_draft_maximum_are_flagged(radar, b1):
    imp = _impact(radar, b1, AFTER_MAX, "RR-02")
    assert imp.severity == "review"  # CFCFRMS holds sit in the draft's LEA carve-out: confirm, don't breach
    assert imp.summary.startswith("5 of 5 holds are older than the RBI DRAFT's 60-day maximum")
    for f in imp.findings:
        assert f.dates["draft_max"] < AFTER_MAX < f.dates["sop_lapse"]
        assert "past the RBI DRAFT's 60-day maximum" in f.text and "SOP lapse" in f.text


def test_bank_initiated_hold_past_the_maximum_is_an_action(radar, b1):
    bank_only = b1.model_copy(update={"case": b1.case.model_copy(update={"ncrp_ids": []})})
    imp = _impact(radar, bank_only, AFTER_MAX, "RR-02")
    assert imp.severity == "action"
    assert all("would breach the RBI DRAFT if adopted" in f.text for f in imp.findings)


def test_hold_approaching_the_draft_maximum_needs_review(radar, b1):
    cap = _impact(radar, b1, BEFORE_MAX, "RR-02").findings[0].dates["draft_max"]
    imp = _impact(radar, b1, cap - timedelta(days=3), "RR-02")
    assert imp.severity == "review"
    assert "the RBI DRAFT's maximum falls on" in imp.findings[0].text


def test_innocent_holder_needs_an_str_decision(radar, b1, b2):
    imp = _impact(radar, b1, BEFORE_MAX, "RR-04")
    assert imp.severity == "review"
    assert [(f.account_id, f.amount) for f in imp.findings] == [(sc.L, 4800.0)]
    assert not _impact(radar, b2, BEFORE_MAX, "RR-04").applies


def test_pool_hold_stays_on_the_cfcfrms_notice(radar, b1, b2):
    imp = _impact(radar, b1, BEFORE_MAX, "RR-05")
    assert [(f.account_id, f.amount) for f in imp.findings] == [(sc.P, 19200.0)]
    assert not _impact(radar, b2, BEFORE_MAX, "RR-05").applies


def test_amount_level_holds_vs_whole_account_freeze(radar, b1, b2):
    imp = _impact(radar, b1, BEFORE_MAX, "RR-06")
    assert imp.severity == "info" and imp.findings[0].amount == 142848.0
    assert "₹1,42,848.00" in imp.findings[0].text and "₹5,03,50,000.00" in imp.findings[0].text
    over = b1.model_copy(update={"plan": b1.plan.model_copy(update={"over_hold_amount": 100.0})})
    assert _impact(radar, over, BEFORE_MAX, "RR-06").severity == "action"
    assert _impact(radar, b2, BEFORE_MAX, "RR-06").findings[0].amount == 120000.0


def test_below_floor_uses_the_drafts_minimum(radar, b1):
    assert not _impact(radar, b1, BEFORE_MAX, "RR-07").applies
    plan = b1.plan.model_copy(deep=True)
    dec = next(d for d in plan.decisions if d.account_id == sc.L)
    dec.hold_amount = 750.0
    imp = _impact(radar, b1.model_copy(update={"plan": plan}), BEFORE_MAX, "RR-07")
    assert [(f.account_id, f.amount) for f in imp.findings] == [(sc.L, 750.0)]
    assert "below ₹1,000" in imp.findings[0].text


def test_customer_notice_gap(radar, b1):
    imp = _impact(radar, b1, BEFORE_MAX, "RR-09")
    assert imp.applies and len(imp.findings) == 5 and imp.severity == "info"
    drafts = [
        {"draft_id": f"N-{d.account_id}", "case_id": b1.case.case_id, "kind": DraftKind.CUSTOMER_NOTICE,
         "account_id": d.account_id, "title": "Notice", "body": "x", "created_at": BEFORE_MAX}
        for d in b1.plan.decisions if d.hold_amount > 0
    ]
    done = b1.model_validate({**b1.model_dump(), "drafts": drafts})
    imp = _impact(radar, done, BEFORE_MAX, "RR-09")
    assert imp.findings == [] and imp.summary.startswith("A customer notice is drafted for every held account")


def test_retired_citation_in_a_draft_is_flagged(radar, b1):
    assert not _impact(radar, b1, BEFORE_MAX, "RR-11").applies
    draft = {"draft_id": "D-1", "case_id": b1.case.case_id, "kind": DraftKind.STR, "title": "STR", "body": "x",
             "created_at": BEFORE_MAX, "citation_ids": ["RBI-FRM-MD-2024"]}
    stale = b1.model_validate({**b1.model_dump(), "drafts": [draft]})
    imp = _impact(radar, stale, BEFORE_MAX, "RR-11")
    assert imp.severity == "action" and "D-1 cites RBI-FRM-MD-2024" in imp.findings[0].text


def test_no_plan_means_nothing_to_compare(radar, b1):
    bare = b1.model_copy(update={"plan": None})
    impacts = case_impact(radar, bare, BEFORE_MAX)
    assert impacts and not any(i.applies for i in impacts)


def test_case_impact_is_sorted_and_numbers_come_from_the_case(radar, b1):
    impacts = case_impact(radar, b1, AFTER_MAX)
    ranks = [{"action": 2, "review": 1, "info": 0}[i.severity] for i in impacts]
    assert ranks == sorted(ranks, reverse=True)
    assert impacts[0].clause_id in {"RR-02", "RR-04"}
    amounts = {d.hold_amount for d in b1.plan.decisions} | {b1.plan.total_hold, b1.plan.over_hold_amount}
    for i in impacts:
        for f in i.findings:
            assert f.amount is None or f.amount in amounts, (i.clause_id, f.amount)


def test_case_impact_does_not_modify_the_bundle(radar, b1):
    before = b1.model_dump_json()
    case_impact(radar, b1, AFTER_MAX, include_unverified=True)
    assert b1.model_dump_json() == before


def test_unwitting_conduit_gets_the_str_recommendation_wording(radar, b1):
    plan = b1.plan.model_copy(deep=True)
    dec = next(d for d in plan.decisions if d.account_id == sc.L)
    dec.role = Role.UNWITTING_CONDUIT
    dec.reason_codes = [*dec.reason_codes, "UNWITTING_CONDUIT", "STR_RECOMMENDED_OFFICER_DECISION"]
    imp = _impact(radar, b1.model_copy(update={"plan": plan}), BEFORE_MAX, "RR-04")
    assert [(f.account_id, f.amount) for f in imp.findings] == [(sc.L, 4800.0)]
    assert "unwitting conduit" in imp.findings[0].text and "record the STR decision" in imp.findings[0].text
    bona_fide = _impact(radar, b1, BEFORE_MAX, "RR-04").findings[0].text
    assert "bona fide recipient (no STR by default)" in bona_fide and "The RBI DRAFT counts" in bona_fide


def test_account_level_exception_is_recognised(radar, b1):
    plan = b1.plan.model_copy(deep=True)
    dec = next(d for d in plan.decisions if d.account_id == sc.A)
    dec.disposition = Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD
    dec.reason_codes = ["EXCEPTIONAL_ACCOUNT_LEVEL_HOLD", "OFFICER_REQUESTED_EXCEPTION",
                        "FRAUDSTER_CONTROLLED_ACCOUNT", "SUPERVISOR_APPROVED_EXCEPTION"]
    approved = _impact(radar, b1.model_copy(update={"plan": plan}), BEFORE_MAX, "RR-06")
    assert approved.severity == "info" and approved.summary.startswith("1 account-level hold as a recorded exception")
    first = approved.findings[0]
    assert first.account_id == sc.A and first.amount == 43200.0
    assert "fraudster controlled account" in first.text and "last resort" in first.text
    assert approved.findings[-1].amount == 142848.0  # plan total unchanged: the amount-specific holds
    dec.reason_codes = ["EXCEPTIONAL_ACCOUNT_LEVEL_HOLD", "FRAUDSTER_CONTROLLED_ACCOUNT"]  # no approval recorded
    unapproved = _impact(radar, b1.model_copy(update={"plan": plan}), BEFORE_MAX, "RR-06")
    assert unapproved.severity == "action" and "without a recorded supervisor approval" in unapproved.findings[0].text


def test_draft_rows_are_labelled_draft(radar):
    assert radar.source("rbi-kyc-mule-draft-2026").short.startswith("RBI DRAFT")
    assert radar.rests_on_draft(radar.clause("RR-02"))
    assert not radar.rests_on_draft(radar.clause("RR-11"))  # in-force repeal, not a draft
    for c in radar.clauses:
        for text in (c.title, c.applies_to, c.impact, c.suggested_action):
            assert " the draft" not in text and "The draft" not in text, (c.id, text)


def test_radar_path_follows_the_docs_dir(monkeypatch, tmp_path):
    from wapsi.domain.regradar import REG_RADAR_JSON, radar_path

    monkeypatch.delenv("WAPSI_REG_RADAR_JSON", raising=False)
    monkeypatch.delenv("WAPSI_DOCS_DIR", raising=False)
    assert radar_path() == REG_RADAR_JSON
    monkeypatch.setenv("WAPSI_DOCS_DIR", str(tmp_path))
    assert radar_path() == tmp_path / "reg_radar.json"
    monkeypatch.setenv("WAPSI_REG_RADAR_JSON", str(tmp_path / "x.json"))
    assert radar_path() == tmp_path / "x.json"
