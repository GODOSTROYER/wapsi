"""The public demo's disclosure: the "recorded run" banner (what the deployment is set up to do) and the status chip
(which model and endpoint host actually served a case's drafts), plus the per-visitor "Start over". Offline."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import ui.data_source as data_source
from ui.components.llm_status import (
    BANNER_OFFLINE,
    BANNER_RECORDED,
    banner,
    llm_status,
    served,
    served_chip,
    ui_llm_mode,
    writer_note,
)
from wapsi.adapters.llm_fake import FakeLLM
from wapsi.adapters.llm_replay import Recording, prose_signature
from wapsi.fixtures import showcase as sc
from wapsi.models import ApprovalDecision, Draft, DraftKind

APP = Path(__file__).resolve().parents[2] / "ui" / "app.py"
CASE = "CASE-31009260000101"
LIVE = "Gemma-4-31B-IT on AMD @ developer.amd.com.cn"
RECORDED = f"{LIVE} (recorded 2026-10-14)"
ENDPOINT = {"WAPSI_LLM_BASE_URL": "https://developer.amd.com.cn/radeon/api/v1", "WAPSI_LLM_API_KEY": "rc-test",
            "WAPSI_LLM_MODEL": "Gemma-4-31B-IT"}
LLM_ENVS = ("WAPSI_LLM_BASE_URL", "WAPSI_LLM_API_KEY", "WAPSI_LLM_MODEL", "WAPSI_LLM_JSON_MODE",
            "WAPSI_LLM_EXTRA_BODY", "WAPSI_LLM_PLATFORM", "WAPSI_LLM_RPM", "WAPSI_UI_LLM", "WAPSI_PROSE_RECORDING",
            "WAPSI_DATA_SOURCE")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for name in LLM_ENVS:  # never a live call from a test, whatever the developer's shell holds
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WAPSI_UI_RAISE", "1")
    monkeypatch.setenv("WAPSI_NOW", "2026-09-16T11:00:00+05:30")
    monkeypatch.setenv("WAPSI_LLM_CACHE_DIR", str(tmp_path / "llm-cache"))
    monkeypatch.setattr(data_source, "_LLM_STATE", {})


def recording_of(*names: str, when: str = "2026-10-14") -> Recording:
    rec = Recording()
    for i, name in enumerate(names):
        rec.put(f"{i:064d}", '{"text": "Words."}', served_by=name, case_id=CASE,
                recorded_at=datetime.fromisoformat(f"{when}T05:00:00+00:00").astimezone(UTC))
    return rec


class Bundle:
    def __init__(self, *drafts: Draft) -> None:
        self.drafts = list(drafts)


def draft(writer: str | None, *, fallback: bool = False, i: int = 0) -> Draft:
    return Draft(draft_id=f"D{i}", case_id=CASE, kind=DraftKind.STR, title="t", body="b", llm_model=writer,
                 structured={"llm_fallback": fallback} if writer else {}, created_at=datetime.now(UTC))


# --------------------------------------------------------------------------- the banner


def test_mode_defaults_to_the_plain_stub():
    assert ui_llm_mode({}) == "fake" and ui_llm_mode({"WAPSI_UI_LLM": " AUTO "}) == "auto"
    assert ui_llm_mode({"WAPSI_UI_LLM": "whatever"}) == "fake"


def test_banner_only_when_the_demo_is_not_calling_a_live_model():
    rec = recording_of(LIVE)
    assert banner(llm_status({}, rec)) is None  # the plain local app: nothing to disclose
    assert banner(llm_status({"WAPSI_UI_LLM": "auto", **ENDPOINT}, rec)) is None  # live endpoint configured

    head, text = banner(llm_status({"WAPSI_UI_LLM": "auto"}, rec))
    assert head == BANNER_RECORDED == "Recorded run — live AMD endpoint not configured"
    assert "Gemma-4-31B-IT on AMD (developer.amd.com.cn) on 2026-10-14" in text and "replayed" in text
    assert "Every number is filled from the ledger and re-checked by the verifier, live." in text
    assert "set WAPSI_LLM_BASE_URL, WAPSI_LLM_API_KEY, WAPSI_LLM_MODEL" in text

    partly = {"WAPSI_UI_LLM": "auto", "WAPSI_LLM_BASE_URL": ENDPOINT["WAPSI_LLM_BASE_URL"]}
    head, text = banner(llm_status(partly, rec))
    assert head == BANNER_RECORDED and text.endswith("set WAPSI_LLM_API_KEY, WAPSI_LLM_MODEL.")

    head, text = banner(llm_status({"WAPSI_UI_LLM": "auto"}, None))
    assert head == BANNER_OFFLINE and "offline stub" in text  # no recording bundled: say that instead
    head, text = banner(llm_status({"WAPSI_UI_LLM": "recorded", **ENDPOINT}, rec))
    assert head == "Recorded run — live model calls are switched off"  # configured, deliberately not used
    assert text.endswith("set WAPSI_UI_LLM=auto.")
    head, _ = banner(llm_status({"WAPSI_UI_LLM": "recorded"}, rec))
    assert head == BANNER_RECORDED
    head, _ = banner(llm_status({"WAPSI_UI_LLM": "live"}, rec))
    assert head == BANNER_OFFLINE  # live-only mode without an endpoint replays nothing


def test_status_never_carries_the_key():
    status = llm_status({"WAPSI_UI_LLM": "auto", **ENDPOINT, "WAPSI_LLM_API_KEY": "rc-SENTINEL-0123456789abcdef"},
                        recording_of(LIVE))
    assert status.live and status.endpoint.host == "developer.amd.com.cn"
    assert "SENTINEL" not in repr(status) and "SENTINEL" not in json.dumps(status.describe())
    assert llm_status({"WAPSI_UI_LLM": "auto", "WAPSI_LLM_JSON_MODE": "yaml"}).live is False  # bad config: not live


# --------------------------------------------------------------------------- the chip


def test_chip_names_the_model_and_endpoint_host_that_served_the_drafts():
    chip = served_chip(Bundle(draft(LIVE), draft(LIVE, i=1), draft(None, i=2)))
    assert (chip.text, chip.tone) == ("Gemma 4 on AMD via developer.amd.com.cn", "good")
    assert "written live by Gemma-4-31B-IT on AMD, served from developer.amd.com.cn" in chip.title
    assert "2 drafts" in chip.title

    chip = served_chip(Bundle(draft(RECORDED)))
    assert (chip.text, chip.tone) == ("Recorded run · Gemma 4 on AMD via developer.amd.com.cn", "accent")
    assert "on 2026-10-14 and is replayed" in chip.title

    chip = served_chip(Bundle(draft("fake-llm")))
    assert (chip.text, chip.tone) == ("Offline stub · no model call", "gray")
    assert served_chip(Bundle(draft("gemini-3.8-flash"))).text == "gemini-3.8-flash"
    assert served_chip(Bundle(draft("vllm-rocm:wapsi-llm"))).text == "wapsi-llm on AMD (vLLM-ROCm)"
    assert served_chip(Bundle(draft("wapsi-llm @ 10.0.0.5:8000"))).text == "wapsi-llm via 10.0.0.5:8000"
    assert served_chip(Bundle(draft(None))) is None and served_chip(Bundle()) is None


def test_chip_reports_what_happened_not_what_was_configured():
    # the endpoint was configured, but its prose was rejected or it was not reached: the draft says so
    chip = served_chip(Bundle(draft(LIVE, fallback=True), draft("template-fallback", i=1)))
    assert (chip.text, chip.tone) == ("Fixed paragraphs · model not reached", "warn")
    mixed = Bundle(draft(LIVE), draft(LIVE, i=1), draft(RECORDED, i=2), draft("fake-llm", i=3),
                   draft(LIVE, fallback=True, i=4))
    s = served(mixed)
    assert (s.live, s.recorded, s.stub, s.fixed, s.total) == (2, 1, 1, 1, 5)
    chip = served_chip(mixed)
    assert chip.text == "Gemma 4 on AMD via developer.amd.com.cn · 2 live, 1 recorded, 1 offline stub, 1 fixed"
    assert chip.tone == "warn"
    assert served_chip(Bundle(draft(LIVE), draft(RECORDED, i=1))).tone == "accent"


def test_writer_note_under_each_draft():
    assert writer_note(draft(LIVE)) == ("Prose by Gemma 4 on AMD via developer.amd.com.cn (model Gemma-4-31B-IT, "
                                        "live call); every number is filled from case tables.")
    assert "recorded on 2026-10-14 and replayed" in writer_note(draft(RECORDED))
    assert writer_note(draft("fake-llm")).startswith("Prose by the offline stub (no model call)")
    assert writer_note(draft(LIVE, fallback=True)).startswith("Prose: the fixed paragraph")
    assert writer_note(draft(None)) == "Every number is filled from case tables."
    assert writer_note(draft("gemini-3.8-flash")).startswith("Prose by gemini-3.8-flash;")


# --------------------------------------------------------------------------- the app


def showcase_recording(tmp_path: Path) -> Path:
    """A recording of every showcase prose field, written by the stub in the name of the AMD endpoint."""
    seen: list[list[dict]] = []

    class Spy(FakeLLM):
        def complete(self, messages, **kw):
            seen.append(messages)
            return super().complete(messages, **kw)

    from wapsi.pipeline.run import process_case

    for ids in ([sc.N1], [sc.N2, sc.N3]):
        process_case(sc.showcase_ledger(), ids, llm=Spy())
    rec = Recording()
    for m in seen:
        rec.put(prose_signature(m), FakeLLM().complete(m), served_by=LIVE, case_id=CASE,
                recorded_at=datetime(2026, 10, 14, 5, 0, tzinfo=UTC))
    return rec.save(tmp_path / "demo_prose.json")


def _text(at: AppTest) -> str:
    parts = [html.unescape(m.value) for m in at.markdown]
    parts += [str(x.value) for x in (*at.success, *at.info, *at.warning, *at.error)]
    return "\n".join(parts)


def _app(page: str = "Drafts & Verification") -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=90)
    at.session_state["case_id"] = CASE
    at.session_state["page"] = page
    return at.run()


def test_plain_local_app_shows_no_banner_and_no_chip():
    at = _app()
    assert not at.exception
    text = _text(at)
    assert "live AMD endpoint not configured" not in text and "Offline stub" not in text
    assert "Prose by the offline stub (no model call); every number is filled from case tables." in text
    assert not [b for b in at.button if b.key == "demo_start_over"]
    assert data_source.llm_cache_key() == ("fake",)


def test_recorded_mode_shows_the_banner_and_the_chip(monkeypatch, tmp_path):
    monkeypatch.setenv("WAPSI_UI_LLM", "auto")
    monkeypatch.setenv("WAPSI_PROSE_RECORDING", str(showcase_recording(tmp_path)))
    at = _app()
    assert not at.exception and not at.warning
    text = _text(at)
    assert "Recorded run — live AMD endpoint not configured." in text
    assert "written by Gemma-4-31B-IT on AMD (developer.amd.com.cn) on 2026-10-14" in text
    assert text.count("Recorded run · Gemma 4 on AMD via developer.amd.com.cn") == 2  # sidebar and drafts summary
    assert "Prose in this case" in text and "passed every check" in text
    at.radio(key="page").set_value("Inbox").run()
    assert "Recorded run — live AMD endpoint not configured." in _text(at)  # on every page
    assert data_source.llm_cache_key()[0] == "auto"


def test_offline_banner_without_a_recording(monkeypatch):
    monkeypatch.setenv("WAPSI_UI_LLM", "auto")
    monkeypatch.setenv("WAPSI_PROSE_RECORDING", "none")
    text = _text(_app())
    assert "Offline run — live AMD endpoint not configured." in text and "Recorded run —" not in text
    assert "Offline stub · no model call" in text  # in the demo the stub is disclosed too


def test_live_endpoint_shows_the_live_chip_and_no_banner(monkeypatch, tmp_path):
    for k, v in ENDPOINT.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("WAPSI_UI_LLM", "auto")
    monkeypatch.setenv("WAPSI_PROSE_RECORDING", str(showcase_recording(tmp_path)))

    class LiveStub(FakeLLM):  # stands in for the endpoint: no network in tests
        last_from_cache = False

    asked: list[bool] = []

    def fake_live(fresh: bool):
        asked.append(fresh)
        return LiveStub(name=LIVE)

    monkeypatch.setattr(data_source, "_live_llm", fake_live)
    at = _app()
    assert not at.exception
    text = _text(at)
    assert "live AMD endpoint not configured" not in text
    assert text.count("Gemma 4 on AMD via developer.amd.com.cn") >= 2 and "Recorded run ·" not in text
    assert "Prose by Gemma 4 on AMD via developer.amd.com.cn (model Gemma-4-31B-IT, live call)" in text
    assert asked and not any(asked)  # start-up seeding may read the cache; a regenerate asks afresh


def test_a_failing_live_endpoint_falls_back_to_the_recorded_run_and_says_so(monkeypatch, tmp_path):
    for k, v in ENDPOINT.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("WAPSI_UI_LLM", "auto")
    monkeypatch.setenv("WAPSI_PROSE_RECORDING", str(showcase_recording(tmp_path)))

    class Down:
        name = LIVE

        def complete(self, messages, **kw):
            raise RuntimeError("HTTP 429")

    monkeypatch.setattr(data_source, "_live_llm", lambda fresh: Down())
    text = _text(_app())
    assert "live AMD endpoint not configured" not in text  # it is configured; it just did not answer
    assert "Recorded run · Gemma 4 on AMD via developer.amd.com.cn" in text


def test_start_over_gives_the_visitor_a_fresh_copy(monkeypatch, tmp_path):
    monkeypatch.setenv("WAPSI_UI_LLM", "auto")
    monkeypatch.setenv("WAPSI_PROSE_RECORDING", str(showcase_recording(tmp_path)))
    at = _app("Inbox")
    ds = at.session_state["_wapsi_ds"]
    out = ds.save_approval(CASE, draft_id=None, decision=ApprovalDecision.APPROVED, approver="supervisor@hb01",
                           role="supervisor", reason_code="CHECKS_PASSED")
    assert out.ok and ds.get_bundle(CASE).approvals
    assert "Your own copy." in _text(at)
    at.button(key="demo_start_over").click().run()
    assert not at.exception
    fresh = at.session_state["_wapsi_ds"]
    assert fresh is not ds and fresh.get_bundle(CASE).approvals == []  # decisions dropped, cases as first prepared
    assert fresh.get_bundle(CASE).drafts

    # another visitor (a new session) never saw the first one's decision either
    other = _app("Inbox")
    assert other.session_state["_wapsi_ds"].get_bundle(CASE).approvals == []


def test_prose_llm_modes(monkeypatch, tmp_path):
    from wapsi.adapters.llm_replay import DemoLLM

    assert isinstance(data_source.prose_llm(FakeLLM), FakeLLM)  # default: exactly the stub
    monkeypatch.setenv("WAPSI_UI_LLM", "auto")
    monkeypatch.setenv("WAPSI_PROSE_RECORDING", "none")
    assert isinstance(data_source.prose_llm(FakeLLM), FakeLLM)  # nothing to replay, nothing live
    monkeypatch.setenv("WAPSI_PROSE_RECORDING", str(showcase_recording(tmp_path)))
    replay = data_source.prose_llm(FakeLLM, prefer="recorded", allow_live=False)
    assert isinstance(replay, DemoLLM) and replay.live is None and len(replay.recording) == 5
    for k, v in ENDPOINT.items():
        monkeypatch.setenv(k, v)
    live = data_source.prose_llm(FakeLLM)
    assert live.live is not None and live.live.name == LIVE and live.live.read_cache is True
    assert live.live.limiter.max_wait_s == data_source.UI_LIVE_MAX_WAIT_S and live.live.accept is not None
    fresh = data_source.prose_llm(FakeLLM, fresh=True)
    assert fresh.live.read_cache is False and fresh.live.inner is live.live.inner  # one client, one rate limiter
    assert fresh.live.limiter is live.live.limiter and fresh.gate is live.gate
    assert data_source.prose_llm(FakeLLM, allow_live=False).live is None  # a start-up batch stays off the endpoint
    monkeypatch.setenv("WAPSI_UI_LLM", "recorded")
    assert data_source.prose_llm(FakeLLM).live is None
    monkeypatch.setenv("WAPSI_UI_LLM", "live")
    strict = data_source.prose_llm(FakeLLM)
    assert strict.live is not None and strict.recording is None and strict.stub is None
    assert "rc-test" not in repr(strict) and "rc-test" not in repr(strict.live)
