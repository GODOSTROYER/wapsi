"""Recorded prose and the demo's LLM chain (``wapsi.adapters.llm_replay``): what a recording is keyed by, who a
draft says wrote its prose, fail-over from a failing or garbling live endpoint, and the recorder script. Offline."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from wapsi.adapters.llm_env import parse_served
from wapsi.adapters.llm_fake import FakeLLM
from wapsi.adapters.llm_replay import (
    LIVE,
    RECORDED,
    STUB,
    DemoLLM,
    LiveGate,
    Recording,
    prose_answer_problem,
    prose_signature,
)
from wapsi.adapters.llm_resilience import ResilientLLM, UnusableAnswer
from wapsi.fixtures import showcase as sc
from wapsi.pipeline.run import process_case

ROOT = Path(__file__).resolve().parents[1]
LIVE_NAME = "Gemma-4-31B-IT on AMD @ developer.amd.com.cn"
GARBAGE = '{"' + "/" * 400  # what a broken endpoint really returned with HTTP 200 (observed 2026-10-14)


def request(task: str = "case_summary", total: str = "₹4,80,000.00", instructions: str = "Write it.") -> list[dict]:
    payload = {"task": task, "instructions": instructions, "facts": {"case": {"opened_at": "2026-10-14T00:00:00Z"}},
               "placeholders": [{"name": "total_stolen", "value": total, "meaning": "total reported stolen"}]}
    return [{"role": "system", "content": "Rules."}, {"role": "user", "content": json.dumps(payload)}]


class Live:
    """A stand-in for the live endpoint: answers like the stub, fails or garbles on demand."""

    name = LIVE_NAME

    def __init__(self, fail: BaseException | None = None, garble: bool = False) -> None:
        self.fail, self.garble, self.calls = fail, garble, 0
        self.inner = FakeLLM()

    def complete(self, messages, *, temperature=0.0, max_tokens=1024, json_schema=None):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        if self.garble:
            return GARBAGE
        return self.inner.complete(messages, temperature=temperature, max_tokens=max_tokens, json_schema=json_schema)


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


# --------------------------------------------------------------------------- the key and the file


def test_signature_follows_the_slots_not_the_wording():
    base = prose_signature(request())
    assert base and len(base) == 64
    assert prose_signature(request(instructions="Please write the paragraph.")) == base  # rewording keeps it
    reworded_system = [{"role": "system", "content": "Other rules."}, request()[1]]
    assert prose_signature(reworded_system) == base
    assert prose_signature(request(total="₹4,79,000.00")) != base  # other figures: never replayed onto them
    assert prose_signature(request(task="str_grounds")) != base
    assert prose_signature([{"role": "user", "content": "Summarise the case."}]) is None  # not a prose request
    assert prose_signature([{"role": "user", "content": json.dumps({"task": "x"})}]) is None


def test_recording_round_trip_and_summary(tmp_path):
    rec = Recording()
    rec.put("a" * 64, '{"text": "One."}', served_by=LIVE_NAME, task="case_summary", case_id="CASE-1",
            draft_id="D1", attempts=2, latency_s=1.7)
    path = rec.save(tmp_path / "rec" / "demo_prose.json")
    assert path.read_bytes().endswith(b"\n") and b"\r\n" not in path.read_bytes()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["v"] == 1 and "never training data" in data["purpose"]
    back = Recording.load(path)
    entry = back.get("a" * 64)
    assert len(back) == 1 and entry["text"] == '{"text": "One."}' and entry["latency_s"] == 1.7
    day = entry["recorded_at"][:10]
    assert Recording.writer_name(entry) == f"{LIVE_NAME} (recorded {day})"
    assert back.summary() == {"entries": 1, "served_by": [LIVE_NAME], "cases": 1, "recorded_from": day,
                              "recorded_to": day}
    assert len(Recording.load(tmp_path / "missing.json")) == 0 and len(Recording.load(None)) == 0
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert len(Recording.load(tmp_path / "bad.json")) == 0  # unreadable: the demo then uses the stub and says so


# --------------------------------------------------------------------------- the chain


def recording_for(*requests: list[dict]) -> Recording:
    rec = Recording()
    for r in requests:
        rec.put(prose_signature(r), '{"text": "Recorded words about ⟦total_stolen⟧."}', served_by=LIVE_NAME)
    return rec


def test_chain_order_and_names():
    r = request()
    rec = recording_for(r)
    live_first = DemoLLM(live=Live(), recording=rec, stub=FakeLLM(), prefer=LIVE)
    assert live_first.name == LIVE_NAME  # before any call: what would answer
    live_first.complete(r)
    assert live_first.name == LIVE_NAME and live_first.counts == {LIVE: 1}
    assert live_first.answers[0].latency_s is not None and live_first.answers[0].signature == prose_signature(r)

    recorded_first = DemoLLM(live=Live(), recording=rec, stub=FakeLLM(), prefer=RECORDED)
    assert recorded_first.complete(r) == '{"text": "Recorded words about ⟦total_stolen⟧."}'
    assert parse_served(recorded_first.name).kind == "recorded" and recorded_first.live.calls == 0
    recorded_first.complete(request(total="₹1.00"))  # nothing recorded for these figures: the live endpoint
    assert recorded_first.name == LIVE_NAME and recorded_first.counts == {RECORDED: 1, LIVE: 1}

    offline = DemoLLM(recording=rec, stub=FakeLLM())
    offline.complete(request(total="₹1.00"))
    assert offline.name == "fake-llm" and offline.counts == {STUB: 1}
    with pytest.raises(ValueError):
        DemoLLM(prefer="fastest")
    with pytest.raises(RuntimeError, match="no model"):
        DemoLLM().complete(r)


def test_a_rejected_recorded_answer_is_not_offered_again():
    r = request()
    llm = DemoLLM(recording=recording_for(r), stub=FakeLLM(), prefer=RECORDED)
    llm.complete(r)
    retry = [*r, {"role": "assistant", "content": "x"}, {"role": "user", "content": "Your answer broke the rules."}]
    llm.complete(retry)  # the drafter's feedback turn: the recording is stale for this build
    assert [a.source for a in llm.answers] == [RECORDED, STUB]


def test_live_failure_fails_over_and_closes_the_gate_for_a_cool_down():
    r = request()
    clock = Clock()
    gate = LiveGate(cooldown_s=60, clock=clock)
    err = RuntimeError("boom")
    err.status_code, err.retry_after_s = 429, 30.0
    live = Live(fail=err)
    llm = DemoLLM(live=live, recording=recording_for(r), stub=FakeLLM(), gate=gate)
    llm.complete(r)
    assert parse_served(llm.name).kind == "recorded" and llm.counts == {"live_failed": 1, RECORDED: 1}
    assert gate.last_error == "RuntimeError (HTTP 429)" and not gate.is_open()
    llm.complete(r)
    assert live.calls == 1  # not hammered while the gate is closed
    clock.t += 31  # the server's Retry-After has passed
    assert gate.is_open()
    live.fail = None
    DemoLLM(live=live, recording=recording_for(r), stub=FakeLLM(), gate=gate).complete(r)
    assert live.calls == 2

    strict = DemoLLM(live=Live(fail=err), gate=LiveGate())  # WAPSI_UI_LLM=live: no replay, no stub
    with pytest.raises(RuntimeError, match="boom"):
        strict.complete(r)


def test_a_garbled_live_answer_counts_as_a_failure():
    """HTTP 200 with degenerate output: the answer is not shown, the recorded run answers instead."""
    r = request()
    assert prose_answer_problem(r, GARBAGE) and prose_answer_problem(r, "") == "the answer is empty"
    assert prose_answer_problem(r, '{"text": "Two complaints report ⟦total_stolen⟧ stolen."}') is None
    assert prose_answer_problem([{"role": "user", "content": "free text"}], GARBAGE) is None  # not a prose request
    llm = DemoLLM(live=Live(garble=True), recording=recording_for(r), stub=FakeLLM())
    assert llm.complete(r) == '{"text": "Recorded words about ⟦total_stolen⟧."}'
    assert llm.counts == {"live_failed": 1, RECORDED: 1} and not llm.gate.is_open()
    assert llm.gate.last_error == "UnusableLiveAnswer"


def test_resilient_llm_never_caches_an_unusable_answer(tmp_path):
    r = request()
    bad = Live(garble=True)
    llm = ResilientLLM(bad, model_id="m", rpm=0, rpd=0, cache_dir=tmp_path, accept=prose_answer_problem)
    with pytest.raises(UnusableAnswer, match="unusable"):
        llm.complete(r)
    assert llm.report()["unusable_answers"] == 1 and llm.report()["errors"] == 1
    assert not [p for p in tmp_path.rglob("*.json") if p.name != "_quota.json"]  # nothing was stored
    # an answer cached by a run without the check is dropped on sight, and the model is asked again
    ResilientLLM(bad, model_id="m", rpm=0, rpd=0, cache_dir=tmp_path).complete(r)
    good = Live()
    fixed = ResilientLLM(good, model_id="m", rpm=0, rpd=0, cache_dir=tmp_path, accept=prose_answer_problem)
    assert prose_answer_problem(r, fixed.complete(r)) is None and good.calls == 1
    assert fixed.report()["cache_hits"] == 0
    assert fixed.complete(r) and good.calls == 1 and fixed.report()["cache_hits"] == 1  # the good answer is cached


# --------------------------------------------------------------------------- drafts say who wrote them


def test_each_draft_records_its_own_writer():
    """One pipeline run, three sources: every prose draft names the one that answered it, and the audit chain
    seals that name with the draft."""
    ledger = sc.showcase_ledger()
    seen: list[list[dict]] = []

    class Spy(FakeLLM):
        def complete(self, messages, **kw):
            seen.append(messages)
            return super().complete(messages, **kw)

    process_case(ledger, [sc.N1], llm=Spy())
    first = {prose_signature(m): m for m in seen if len(m) == 2}
    assert len(first) == 3  # the showcase case has three prose fields
    sigs = list(first)
    rec = Recording()
    rec.put(sigs[0], FakeLLM().complete(first[sigs[0]]), served_by=LIVE_NAME)

    class OneShot(Live):  # the live endpoint answers once, then is rate limited
        def complete(self, messages, **kw):
            if self.calls >= 1:
                self.calls += 1
                raise RuntimeError("rate limited")
            return super().complete(messages, **kw)

    llm = DemoLLM(live=OneShot(), recording=rec, stub=FakeLLM(), prefer=RECORDED)
    b = process_case(ledger, [sc.N1], llm=llm)
    prose_drafts = [d for d in b.drafts if d.llm_model]
    kinds = sorted(parse_served(d.llm_model).kind for d in prose_drafts)
    assert kinds == ["endpoint", "recorded", "stub"] and llm.counts[RECORDED] == llm.counts[LIVE] == 1
    assert all(r.passed for r in b.verifications.values())  # replayed prose is verified like any other
    sealed = {e.object_id: e.details["llm_model"] for e in b.audit if e.action == "draft_created"}
    assert all(sealed[d.draft_id] == d.llm_model for d in prose_drafts)


# --------------------------------------------------------------------------- the recorder


def load_recorder():
    spec = importlib.util.spec_from_file_location("record_demo_prose", ROOT / "scripts" / "record_demo_prose.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_recorder_records_accepted_answers_and_a_rerun_asks_nothing():
    recorder = load_recorder()
    rec = Recording()
    live = Live()
    out = recorder.record_source("showcase", rec, live, log=lambda *_: None)
    assert (out["cases"], out["fields"], out["recorded"], out["rejected"], out["missing"]) == (2, 5, 5, 0, 0)
    assert len(rec) == 5 and live.calls == 5
    assert {e["served_by"] for e in rec.entries.values()} == {LIVE_NAME}
    assert {e["case_id"] for e in rec.entries.values()} == {"CASE-31009260000101", "CASE-31109260000102"}
    assert all("⟦" in e["text"] and e["draft_id"] and e["task"] for e in rec.entries.values())

    again = recorder.record_source("showcase", rec, live, log=lambda *_: None)
    assert again["already"] == 5 and again["recorded"] == 0 and live.calls == 5

    dry = recorder.record_source("showcase", Recording(), None, log=lambda *_: None)
    assert dry["missing"] == 5 and dry["recorded"] == 0

    # replayed, every showcase draft is written by the recording and still passes the verifier
    b = process_case(sc.showcase_ledger(), [sc.N1], llm=DemoLLM(recording=rec, stub=FakeLLM(), prefer=RECORDED))
    assert {parse_served(d.llm_model).kind for d in b.drafts if d.llm_model} == {"recorded"}
    assert all(r.passed for r in b.verifications.values())


def test_recorder_never_records_garbage_and_stops_at_its_budget():
    recorder = load_recorder()
    rec = Recording()
    out = recorder.record_source("showcase", rec, Live(garble=True), log=lambda *_: None)
    assert len(rec) == 0 and out["recorded"] == 0  # a degenerate answer is never kept

    class Counting:
        def __init__(self):
            self.api_calls = 0

    live = Live()
    inner = type("R", (), {"name": LIVE_NAME, "stats": Counting(), "last_from_cache": False})()

    def complete(messages, **kw):
        inner.stats.api_calls += 1
        return live.complete(messages, **kw)

    inner.complete = complete
    budgeted = recorder.Budgeted(inner, max_calls=2)
    out = recorder.record_source("showcase", rec, budgeted, log=lambda *_: None)
    assert live.calls == 2 and out["recorded"] == 2 and out["missing"] == 3


def test_the_bundled_recording_covers_the_showcase_cases():
    """``data/recordings/demo_prose.json`` is what the public demo replays when no live endpoint is configured.
    If this fails after the showcase figures or the prose slots changed, record again:
    ``uv run python scripts/record_demo_prose.py`` (needs the endpoint variables)."""
    path = ROOT / "data" / "recordings" / "demo_prose.json"
    rec = Recording.load(path)
    assert len(rec) >= 5, f"{path} is missing or empty"
    recorder = load_recorder()
    dry = recorder.record_source("showcase", rec, None, log=lambda *_: None)
    assert dry["missing"] == 0 and dry["already"] == dry["fields"] == 5, dry
    for entry in rec.entries.values():
        writer = parse_served(Recording.writer_name(entry))
        assert writer.kind == "recorded" and writer.host and writer.recorded_on, entry["served_by"]
        assert prose_answer_problem(request(), entry["text"]) is None
        assert "₹" not in entry["text"]  # amounts are placeholders: no figure is in the recording
    for ncrp in ([sc.N1], [sc.N2, sc.N3]):
        b = process_case(sc.showcase_ledger(), ncrp, llm=DemoLLM(recording=rec, stub=FakeLLM(), prefer=RECORDED))
        prose_drafts = [d for d in b.drafts if d.llm_model]
        assert prose_drafts and all(parse_served(d.llm_model).kind == "recorded" for d in prose_drafts)
        assert not any(d.structured.get("llm_fallback") for d in prose_drafts)
        assert all(r.passed for r in b.verifications.values())
    text = path.read_text(encoding="utf-8")
    assert "rc-" not in text and "Bearer" not in text and "api_key" not in text.lower()
