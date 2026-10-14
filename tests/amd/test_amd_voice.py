"""AMD edition voice intake (offline): API endpoints, metrics, the edition's voice model, the UI data source and
the voice benchmark with its report section. No model, no network."""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from wapsi.fixtures import showcase as sc
from wapsi.intake.samples import get_sample

MEERA = "meera-impersonation"


@pytest.fixture()
def client(demo_services):
    from fastapi.testclient import TestClient

    from editions.amd.app import api

    api.METRICS = api.Metrics()
    with TestClient(api.app) as c:
        yield c


@pytest.fixture()
def open_services(demo_services, monkeypatch):
    """Demo services asked for the open path explicitly: fake recogniser + fake text model, built like real ones."""
    monkeypatch.setenv("WAPSI_VOICE_BACKEND", "open")
    monkeypatch.setenv("WAPSI_ASR_BACKEND", "fake")
    demo_services.voice = None
    return demo_services


# --------------------------------------------------------------------------- API


def test_voice_info_lists_the_model_and_samples_without_secrets(client):
    body = client.get("/intake/voice").json()
    assert body["model"]["backend"] == "fake" and body["model"]["label"].startswith("Offline demo model")
    assert body["max_bytes"] == 15 * 1024 * 1024 and "OGG" in body["formats"]
    assert [s["id"] for s in body["samples"]][:3] == [MEERA, "arjun-kyc-two-payments", "fatima-courier-injection"]
    assert client.get("/healthz").json()["voice"].startswith("Offline demo model")  # known once it is built


def test_healthz_does_not_build_the_voice_model(client, demo_services):
    body = client.get("/healthz").json()
    assert body["voice_backend"] == "auto" and "voice" not in body and demo_services.voice is None


def test_post_a_bundled_sample_returns_the_reconciled_result(client):
    r = client.post("/intake/voice", params={"sample": MEERA})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched_ncrp_ids"] == [sc.N1] and body["counts"] == {"MATCHED": 9, "MISMATCH": 0, "NOT_FOUND": 0}
    assert body["claims_untrusted"] is True and body["injection_flagged"] is False and body["wall_s"] >= 0
    assert body["sample_id"] == MEERA and "98XXXXXX21" in body["transcript"] and "45621" not in r.text
    assert client.post("/intake/voice", params={"sample": "nobody"}).status_code == 404


def test_post_raw_audio_bytes(client):
    sample = get_sample("fatima-courier-injection")
    r = client.post("/intake/voice", params={"filename": "PTT-0007.wav"}, content=sample.read_bytes(),
                    headers={"content-type": "audio/wav"})  # fmt: skip
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched_ncrp_ids"] == [sc.N3] and body["injection_flagged"] is True and body["sample_id"] is None
    assert any("Ignore previous instructions" in s for s in body["flagged_spans"]) and body["filename"] == "PTT-0007.wav"


def test_bad_uploads_get_clear_errors(client):
    r = client.post("/intake/voice", content=b"%PDF-1.7 a complaint form", headers={"content-type": "audio/wav"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "UNSUPPORTED_AUDIO"
    r = client.post("/intake/voice", content=b"")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "EMPTY_AUDIO"
    r = client.post("/intake/voice", content=b"RIFF", headers={"content-length": str(16 * 1024 * 1024)})
    assert r.status_code == 413
    metrics = client.get("/metrics").text
    assert "wapsi_voice_failures_total 2" in metrics


def test_voice_intake_requires_the_api_token_when_one_is_set(client, demo_services):
    from editions.amd.app.services import set_services

    set_services(replace(demo_services, settings=replace(demo_services.settings, api_token="s3cret")))
    assert client.post("/intake/voice", params={"sample": MEERA}).status_code == 401
    ok = client.post("/intake/voice", params={"sample": MEERA}, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_open_path_through_the_api_reports_models_timings_and_metrics(client, open_services):
    info = client.get("/intake/voice").json()["model"]
    assert info["backend"] == "open" and info["transcribed_by"] == "fake-asr" and info["asr_backend"] == "fake"
    r = client.post("/intake/voice", params={"sample": "arjun-kyc-two-payments"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"] == {"MATCHED": 8, "MISMATCH": 2, "NOT_FOUND": 1}  # the same reconciliation as ever
    p = body["pipeline"]
    assert p["backend"] == "open" and p["structured_by"] == "fake-transcript-llm" and p["attempts"] == 1
    assert p["audio_s"] == 30.83 and p["rtf"] is None  # the stand-in recogniser claims no real-time factor
    assert body["segments"] and body["extracted_by"] == "fake-asr + fake-transcript-llm"
    metrics = client.get("/metrics").text
    assert "wapsi_voice_intakes_total 1" in metrics and "wapsi_voice_matched_total 1" in metrics
    assert "wapsi_voice_asr_seconds_sum 0" in metrics  # nothing was measured, nothing is counted
    assert client.get("/healthz").json()["voice"] == "fake-asr + fake-transcript-llm"


def test_voice_metrics_accumulate_asr_and_extraction_seconds():
    from editions.amd.app.api import Metrics
    from wapsi.intake.asr import VoicePipelineInfo

    class Result:
        matched_ncrp_ids = ["x"]
        injection_flagged = True
        pipeline = VoicePipelineInfo(backend="open", audio_s=30.0, asr_s=6.0, rtf=0.2, extract_s=1.8)

    m = Metrics()
    m.record_voice(Result(), 8.1)
    m.record_voice(Result(), 7.9)
    text = m.render(None)
    assert "wapsi_voice_audio_seconds_sum 60" in text and "wapsi_voice_asr_seconds_sum 12" in text
    assert "wapsi_voice_extract_seconds_sum 3.6" in text and "wapsi_voice_injection_flags_total 2" in text


# --------------------------------------------------------------------------- the edition's voice model


def test_edition_voice_model_uses_the_editions_own_llm_for_extraction(tmp_path, monkeypatch):
    from editions.amd.app.services import build_services, voice_model
    from editions.amd.app.settings import AmdSettings
    from wapsi.adapters.llm_openai_compat import OpenAICompatLLM
    from wapsi.intake.open_voice import OpenVoiceLLM

    monkeypatch.setenv("WAPSI_ASR_BASE_URL", "http://asr:8002/v1")
    monkeypatch.setenv("WAPSI_ASR_ON_AMD", "1")
    monkeypatch.setenv("WAPSI_LLM_ON_AMD", "1")
    llm = OpenAICompatLLM("http://llm:8000/v1", "wapsi-llm", api_key="", name="vllm-rocm:wapsi-llm")
    svc = build_services(AmdSettings(state_dir=str(tmp_path / "s"), ui_seed_limit=0), llm=llm, search=False)
    model = voice_model(svc)
    assert isinstance(model, OpenVoiceLLM) and model.extractor.llm is llm and voice_model(svc) is model
    assert model.label() == "Whisper large-v3-turbo + wapsi-llm on AMD" and model.served_by == "vLLM-ROCm on AMD"
    assert model.extractor.chain.models == ["wapsi-llm"]  # an own vLLM serves one model
    assert svc.describe()["voice"] == model.label()


def test_edition_voice_model_falls_back_safely(tmp_path, monkeypatch):
    from editions.amd.app.services import build_services, build_voice
    from editions.amd.app.settings import AmdSettings
    from wapsi.adapters.llm_fake import FakeLLM
    from wapsi.adapters.llm_openai_compat import OpenAICompatLLM
    from wapsi.intake.backends import UnconfiguredVoiceModel

    monkeypatch.setattr("wapsi.intake.backends.local_asr_installed", lambda: False)
    monkeypatch.setenv("WAPSI_ASR_CHAT_AUDIO", "off")
    llm = OpenAICompatLLM("http://llm:8000/v1", "wapsi-llm", api_key="")
    svc = build_services(AmdSettings(state_dir=str(tmp_path / "s"), ui_seed_limit=0), llm=llm, search=False)
    assert isinstance(build_voice(svc), FakeLLM)  # no recogniser configured: the offline model, never a crash
    monkeypatch.setenv("WAPSI_VOICE_BACKEND", "open")
    stand_in = build_voice(svc)
    assert isinstance(stand_in, UnconfiguredVoiceModel) and "WAPSI_ASR_BASE_URL" in stand_in.problem
    demo = build_services(AmdSettings(demo_mode=True, state_dir=str(tmp_path / "d"), ui_seed_limit=0), search=False)
    monkeypatch.delenv("WAPSI_VOICE_BACKEND")
    assert build_voice(demo) is demo.llm  # demo mode keeps the offline model


def test_ui_data_source_hands_the_page_the_editions_voice_model(open_services):
    from editions.amd.app.ui_source import AmdDataSource
    from ui.views.intake import _describe, _voice_model

    ds = AmdDataSource(open_services, seed_limit=0)
    model = _voice_model(ds)
    assert model is open_services.voice and _describe(model)["backend"] == "open"


# --------------------------------------------------------------------------- benchmark + report


def test_voice_bench_harness_check_and_report(tmp_path, capsys):
    from editions.amd.bench import report, voice_bench

    out = tmp_path / "results"
    assert voice_bench.main(["--fake", "--samples", MEERA, "fatima-courier-injection", "--out", str(out)]) == 0
    capsys.readouterr()
    result = json.loads((out / "voice_bench.json").read_text(encoding="utf-8"))
    assert result["kind"] == "voice_bench" and result["settings"]["fake"] is True
    m = result["metrics"]
    assert (m["runs"], m["ok"], m["samples_as_documented"], m["samples_checked"]) == (2, 2, 2, 2)
    assert m["value_agreement"] == 1.0 and m["asr_rtf"]["n"] == 0 and m["answers_by_model"] == {"fake-transcript-llm": 2}
    assert "not measurements of any hardware" in result["provenance"][0]
    assert result["hardware"]["asr"]["on_amd"] is False and result["hardware"]["llm"]["on_amd"] is False
    md = (out / "voice_bench.md").read_text(encoding="utf-8")
    assert "Offline harness check" in md and "Samples reconciled as documented" in md
    assert report.main(["--results-dir", str(out)]) == 0
    merged = (out / "AMD_RESULTS.md").read_text(encoding="utf-8")
    assert "## Voice intake: fake-asr + fake-transcript-llm" in merged and "Not a measurement" in merged
    assert "# Wapsi on AMD — voice intake results" in merged and "MI300X" not in merged  # no droplet claim
    assert "## Headline" not in merged  # a harness check contributes no headline number
    assert [p.name for p in report.discover(out)["voice"]] == ["voice_bench.json"]


class _Clock:
    def __init__(self, step: float) -> None:
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_voice_bench_measures_real_time_factor_and_extraction_latency_and_labels_them(tmp_path):
    """A fake recogniser with a known speed and a scripted text model stand in for Whisper and Gemma: the bench's
    arithmetic, cache and provenance are what is tested."""
    from editions.amd.bench import report, voice_bench
    from wapsi.adapters.llm_openai_compat import TokenUsage
    from wapsi.intake.asr import FakeASR

    class Gemma:
        model = name = "Gemma-4-31B-IT"
        base_url = "https://developer.amd.com.cn/radeon/api/v1"
        json_mode = "json_object"
        last_cost_usd = 0.0002

        def __init__(self) -> None:
            self.calls = 0

        def complete_with_usage(self, messages, *, temperature=0.0, max_tokens=1024, json_schema=None,
                                extra_body=None):
            self.calls += 1
            time.sleep(0.02)  # a measurable latency for the live call
            answer = {k: v for k, v in get_sample(MEERA).expected.items() if k != "transcript"}
            return json.dumps(answer), TokenUsage(1450, 420, 1870, 1)

        def complete(self, messages, **kw):
            return self.complete_with_usage(messages, **kw)[0]

    gemma, cache = Gemma(), tmp_path / "cache"
    env = {"WAPSI_LLM_MODELS": "Gemma-4-31B-IT"}  # one model: no probe, no failover in this test
    items = voice_bench.load_items([MEERA], [], "showcase")

    def run():
        recogniser = FakeASR(name="openai/whisper-large-v3-turbo", rtf=0.25)
        model, log = voice_bench.build_model("auto", fake=False, cache_dir=cache, min_interval_s=0.0, env=env,
                                             text_llm=gemma, recogniser=recogniser)  # fmt: skip
        return voice_bench.run_bench(items, model, log, repeat=2, asr="auto")

    first = run()
    assert gemma.calls == 1  # the second repeat of the same note is a replay
    m = first["metrics"]
    assert m["asr_rtf"]["p50"] == 0.25 and m["asr_s"]["p50"] == pytest.approx(30.93 * 0.25, abs=1e-3)
    assert m["samples_as_documented"] == 2 and m["answers_by_model"] == {"Gemma-4-31B-IT": 2}
    assert m["prompt_tokens"]["mean"] == 1450 and m["cost_usd_total"] == pytest.approx(0.0004)
    live, replay = first["runs"]
    assert live["llm_cached"] is False and replay["llm_cached"] is True
    assert replay["extract_s"] == live["extract_s"] >= 0.02  # a replay reports the latency measured live
    assert live["total_s"] == pytest.approx(live["asr_s"] + live["extract_s"])
    assert first["label"] == "Whisper large-v3-turbo (stand-in) + Gemma 4 31B on AMD"
    prov = " ".join(first["provenance"])
    assert "stand-in" in prov and "AMD Token Factory" in prov and "not an MI300X droplet benchmark" in prov
    again = run()
    assert gemma.calls == 1 and again["metrics"]["llm_cached_runs"] == 2  # a second run costs no request
    assert first["settings"]["received_at"] == voice_bench.DEFAULT_RECEIVED_AT  # a fixed date: the prompt replays
    assert again["runs"][0]["llm_measured_at"] == first["runs"][0]["llm_measured_at"]  # when it was really measured
    prompt = json.loads(next(cache.glob("*.json")).read_text(encoding="utf-8"))
    assert "measured_at" in prompt and "16 September 2026" not in prompt["text"]  # the answer, not the prompt
    stored = "".join(p.read_text(encoding="utf-8") for p in cache.glob("*.json"))
    assert "Authorization" not in stored and "measured_at" in stored

    out = tmp_path / "r"
    voice_bench.write_json(out / "voice_bench.json", first)
    assert report.main(["--results-dir", str(out)]) == 0
    md = (out / "AMD_RESULTS.md").read_text(encoding="utf-8")
    assert "speech-to-text real-time factor p50" in md and "not an MI300X droplet benchmark" in md
    assert "| Gemma-4-31B-IT | 2 |" in md


def test_bench_cache_never_stores_or_replays_a_health_probe(tmp_path):
    from editions.amd.bench.voice_bench import CachedLLM, Pacer

    class Inner:
        model = name = "m"
        base_url = ""

        def __init__(self) -> None:
            self.n = 0

        def complete(self, messages, *, temperature=0.0, max_tokens=1024, json_schema=None):
            self.n += 1
            return "AMD OK"

    slept: list[float] = []
    clock = _Clock(1.0)
    inner = Inner()
    llm = CachedLLM(inner, tmp_path, pacer=Pacer(5.0, sleep=slept.append, clock=clock), clock=clock)
    probe = [{"role": "user", "content": "Reply with exactly: AMD OK"}]
    assert llm.complete(probe, max_tokens=12) == "AMD OK" and llm.complete(probe, max_tokens=12) == "AMD OK"
    assert inner.n == 2 and list(tmp_path.glob("*.json")) == []  # a probe asks about now
    assert llm.complete(probe, max_tokens=500) == "AMD OK" and llm.complete(probe, max_tokens=500) == "AMD OK"
    assert inner.n == 3 and len(list(tmp_path.glob("*.json"))) == 1
    assert [c["probe"] for c in llm.calls] == [True, True, False, False] and llm.calls[-1]["cached"] is True
    assert len(slept) == 2 and all(0 < s <= 5.0 for s in slept)  # live calls are paced, replays are not


# --------------------------------------------------------------------------- Whisper-on-AMD transcription server


@pytest.fixture()
def asr_client():
    """The transcription server with a fake pipeline behind the real ``TransformersASR`` (no torch, no weights)."""
    from fastapi.testclient import TestClient

    from editions.amd.asr import server
    from wapsi.adapters.asr_transformers import TransformersASR

    class Pipe:
        model = tokenizer = None

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def __call__(self, inputs, **kwargs):
            self.calls.append({"samples": len(inputs["raw"]), "rate": inputs["sampling_rate"], **kwargs})
            return {"text": " Sir, mera naam Meera Joshi hai. UTR four two five three.",
                    "chunks": [{"timestamp": (0.0, 2.5), "text": " Sir, mera naam Meera Joshi hai."},
                               {"timestamp": (2.5, 6.0), "text": " UTR four two five three."}]}  # fmt: skip

    pipe = Pipe()
    rocm = {"device": "cuda:0", "accelerator": "rocm", "gpu_name": "AMD Instinct MI300X", "on_amd": True}
    server.set_recogniser(TransformersASR("openai/whisper-large-v3-turbo", pipeline_factory=lambda **kw: pipe,
                                          hardware=rocm, clock=_Clock(1.5)))  # fmt: skip
    with TestClient(server.app) as c:
        c.pipe = pipe
        yield c
    server.set_recogniser(None)


def test_asr_server_speaks_the_openai_transcription_api(asr_client):
    wav = get_sample(MEERA).read_bytes()
    health = asr_client.get("/healthz").json()
    assert health["status"] == "ok" and health["model"] == "openai/whisper-large-v3-turbo" and health["loaded"] is False
    r = asr_client.post("/v1/audio/transcriptions", files={"file": ("PTT-0001.wav", wav, "audio/wav")},
                        data={"model": "openai/whisper-large-v3-turbo", "language": "hi"})  # fmt: skip
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"].startswith("Sir, mera naam Meera Joshi hai.") and body["language"] == "hi"
    assert body["duration"] == 30.93 and [s["end"] for s in body["segments"]] == [2.5, 6.0]
    assert body["x_wapsi"] == {"model": "openai/whisper-large-v3-turbo", "elapsed_s": 1.5, "rtf": round(1.5 / 30.93, 4),
                               "device": "cuda:0 (ROCm: AMD Instinct MI300X)", "on_amd": True}  # fmt: skip
    call = asr_client.pipe.calls[0]
    assert call["rate"] == 16_000 and call["generate_kwargs"] == {"task": "transcribe", "language": "hi"}
    as_text = asr_client.post("/v1/audio/transcriptions", files={"file": ("a.wav", wav, "audio/wav")},
                              data={"response_format": "text"})  # fmt: skip
    assert as_text.text.startswith("Sir, mera naam") and as_text.headers["content-type"].startswith("text/plain")
    assert asr_client.get("/v1/models").json()["data"][0]["id"] == "openai/whisper-large-v3-turbo"


def test_asr_server_rejects_what_is_not_audio_and_checks_the_key(asr_client, monkeypatch):
    post = asr_client.post
    assert post("/v1/audio/transcriptions", files={"file": ("x.wav", b"%PDF-1.7", "audio/wav")}).status_code == 400
    assert post("/v1/audio/transcriptions", data={"model": "m"}, files={"other": ("a", b"1")}).status_code == 400
    assert post("/v1/audio/transcriptions", content=b"{}", headers={"content-type": "application/json"}).status_code == 415
    bad = post("/v1/audio/transcriptions", files={"file": ("a.wav", get_sample(MEERA).read_bytes(), "audio/wav")},
               data={"response_format": "srt"})  # fmt: skip
    assert bad.status_code == 400
    monkeypatch.setenv("WAPSI_ASR_API_KEY", "asr-key-not-real")
    wav = {"file": ("a.wav", get_sample(MEERA).read_bytes(), "audio/wav")}
    assert post("/v1/audio/transcriptions", files=wav).status_code == 401
    assert post("/v1/audio/transcriptions", files=wav, headers={"Authorization": "Bearer asr-key-not-real"}).status_code == 200


def test_client_adapter_and_server_agree_end_to_end(asr_client):
    """``OpenAICompatASR`` against the server over the test transport: the real request, the real response."""
    from wapsi.adapters.asr_openai_compat import OpenAICompatASR

    stt = OpenAICompatASR("http://testserver/v1", api_key="", transport=asr_client._transport, on_amd=True)
    out = stt.transcribe(get_sample(MEERA).read_bytes(), "audio/wav")
    assert out.text.endswith("UTR four two five three.") and out.language == "hi" and out.duration_s == 30.93
    assert [(s.start, s.end) for s in out.segments] == [(0.0, 2.5), (2.5, 6.0)] and out.backend == "endpoint"
    assert asr_client.pipe.calls[0]["generate_kwargs"]["language"] == "hi"
