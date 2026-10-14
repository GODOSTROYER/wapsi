"""``WAPSI_VOICE_BACKEND = auto | open | gemini | fake``: which models read a voice note, decided offline."""

from __future__ import annotations

import json

import httpx
import pytest

from wapsi.adapters.asr_openai_compat import ChatAudioASR, OpenAICompatASR
from wapsi.adapters.asr_transformers import TransformersASR
from wapsi.adapters.llm_fake import FakeLLM
from wapsi.fixtures import showcase as sc
from wapsi.intake import VoiceIntakeError, intake_voice_note
from wapsi.intake import backends as B
from wapsi.intake.asr import FakeASR
from wapsi.intake.backends import (
    UnconfiguredVoiceModel,
    VoiceBackendError,
    build_voice_model,
    describe_voice_model,
    env_signature,
    llm_provider,
    resolve_voice_backend,
    safe_voice_model,
    text_model_chain,
)
from wapsi.intake.open_voice import OpenVoiceLLM
from wapsi.intake.samples import get_sample
from wapsi.verify.injection import RulesGuard

TF = "https://developer.amd.com.cn/radeon/api/v1"
LLM = {"WAPSI_LLM_BASE_URL": TF, "WAPSI_LLM_MODEL": "Gemma-4-31B-IT", "WAPSI_LLM_API_KEY": "tf-key-not-real"}
ASR = {"WAPSI_ASR_BASE_URL": "http://whisper.internal:8002/v1"}
NO_PROBE = {"WAPSI_ASR_CHAT_AUDIO": "off"}


@pytest.fixture(autouse=True)
def no_local_whisper(monkeypatch):
    """This machine may or may not have torch, transformers and cached weights: tests decide, not the machine."""
    monkeypatch.setattr(B, "local_asr_installed", lambda: False)
    monkeypatch.setattr(B, "local_model_cached", lambda model: False)


# --------------------------------------------------------------------------- auto


def test_auto_is_offline_when_nothing_is_configured():
    choice = resolve_voice_backend({})
    assert (choice.backend, choice.requested, choice.asr) == ("fake", "auto", "")
    model = build_voice_model({})
    assert isinstance(model, FakeLLM) and model.name == "offline demo model"


def test_auto_prefers_the_open_path_when_an_asr_endpoint_is_configured():
    env = {**LLM, **ASR, "GEMINI_API_KEY": "gm-key-not-real"}  # open wins over Gemini when both are possible
    choice = resolve_voice_backend(env)
    assert (choice.backend, choice.asr) == ("open", "endpoint") and "whisper.internal" in choice.reason
    model = build_voice_model(env)
    assert isinstance(model, OpenVoiceLLM) and isinstance(model.asr, OpenAICompatASR)
    assert model.asr.model == "openai/whisper-large-v3-turbo" and model.asr.language == "hi"
    assert model.llm_model == "Gemma-4-31B-IT" and model.served_by == "AMD Token Factory" and model.llm_on_amd
    assert model.label() == "Whisper large-v3-turbo (endpoint) + Gemma 4 31B on AMD"
    assert model.guard is not None  # the transcript is screened before the text model reads it


def test_auto_prefers_the_open_path_when_a_local_model_is_on_disk(monkeypatch):
    monkeypatch.setattr(B, "local_asr_installed", lambda: True)
    assert resolve_voice_backend({**LLM, **NO_PROBE}).backend == "fake"  # installed, but auto never downloads weights
    monkeypatch.setattr(B, "local_model_cached", lambda model: model == "openai/whisper-small")
    env = {**LLM, **NO_PROBE, "WAPSI_ASR_MODEL": "openai/whisper-small", "WAPSI_ASR_DEVICE": "cpu"}
    choice = resolve_voice_backend(env)
    assert (choice.backend, choice.asr) == ("open", "local")
    model = build_voice_model(env)
    assert isinstance(model.asr, TransformersASR) and model.asr.model == "openai/whisper-small"
    assert not model.asr.loaded  # nothing is loaded until the first voice note
    assert model.label() == "Whisper small (local) + Gemma 4 31B on AMD"


def test_auto_falls_back_to_gemini_then_to_the_offline_model():
    assert resolve_voice_backend({**LLM, **NO_PROBE, "GEMINI_API_KEY": "gm-key-not-real"}).backend == "gemini"
    model = build_voice_model({"GEMINI_API_KEY": "gm-key-not-real"})
    assert model.name.startswith("gemini-api:") and "gm-key" not in repr(model)
    vertex = build_voice_model({"WAPSI_GEMINI_BACKEND": "vertex", "GOOGLE_CLOUD_PROJECT": "wapsi-test"})
    assert vertex.name.startswith("vertex:")
    stuck = resolve_voice_backend({**LLM, **NO_PROBE})
    assert stuck.backend == "fake" and "WAPSI_ASR_BASE_URL" in stuck.reason  # says what is missing
    assert resolve_voice_backend({**ASR}).backend == "fake"  # a recogniser alone cannot fill the fields
    assert resolve_voice_backend({**LLM, **ASR, "WAPSI_VOICE_OFFLINE": "1"}).backend == "fake"


def test_auto_uses_the_chat_endpoint_for_audio_only_when_a_probe_says_it_accepts_audio(tmp_path):
    calls: list[dict] = []

    def accepts(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "."}}]})

    env = {**LLM, "WAPSI_ASR_PROBE_CACHE": str(tmp_path / "probe.json")}
    choice = resolve_voice_backend(env, transport=httpx.MockTransport(accepts))
    assert (choice.backend, choice.asr) == ("open", "chat-audio") and "probed" in choice.reason
    assert calls[0]["model"] == "Gemma-4-31B-IT" and calls[0]["messages"][1]["content"][0]["type"] == "input_audio"
    resolve_voice_backend(env, transport=httpx.MockTransport(accepts))
    B.reset_probe_cache()  # a new process reads the small cache file instead of probing again
    resolve_voice_backend(env, transport=httpx.MockTransport(accepts))
    assert len(calls) == 1
    stored = json.loads((tmp_path / "probe.json").read_text(encoding="utf-8"))
    assert "tf-key-not-real" not in json.dumps(stored) and TF not in json.dumps(stored)  # a hash, no URL, no key
    model = build_voice_model(env, transport=httpx.MockTransport(accepts))
    assert isinstance(model.asr, ChatAudioASR) and model.label() == "Gemma 4 31B (audio) + Gemma 4 31B on AMD"


def test_auto_does_not_use_chat_audio_when_the_gateway_rejects_it(tmp_path):
    """The AMD Token Factory on 2026-10-14: HTTP 400 for audio parts, so speech-to-text runs elsewhere."""
    reject = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": {"message": "cannot deserialize"}}))
    env = {**LLM, "WAPSI_ASR_PROBE_CACHE": str(tmp_path / "probe.json")}
    assert resolve_voice_backend(env, transport=reject).backend == "fake"
    never = httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError("probing is off")))
    assert resolve_voice_backend({**LLM, **NO_PROBE}, transport=never).backend == "fake"


# --------------------------------------------------------------------------- explicit backends


def test_explicit_open_needs_a_recogniser_and_a_text_llm_and_says_so():
    with pytest.raises(VoiceBackendError) as exc:
        resolve_voice_backend({"WAPSI_VOICE_BACKEND": "open", **LLM, **NO_PROBE})
    assert exc.value.code == "NOT_CONFIGURED" and "WAPSI_ASR_BASE_URL" in str(exc.value)
    with pytest.raises(VoiceBackendError, match="WAPSI_LLM_BASE_URL"):
        resolve_voice_backend({"WAPSI_VOICE_BACKEND": "open", **ASR})
    with pytest.raises(VoiceBackendError, match="uv sync --extra asr"):
        resolve_voice_backend({"WAPSI_VOICE_BACKEND": "open", **LLM, "WAPSI_ASR_BACKEND": "local"})
    with pytest.raises(ValueError, match="WAPSI_VOICE_BACKEND"):
        resolve_voice_backend({"WAPSI_VOICE_BACKEND": "whisperx"})
    with pytest.raises(ValueError, match="WAPSI_ASR_BACKEND"):
        resolve_voice_backend({"WAPSI_VOICE_BACKEND": "open", **LLM, "WAPSI_ASR_BACKEND": "cloud"})
    stand_in = safe_voice_model({"WAPSI_VOICE_BACKEND": "open", **LLM, **NO_PROBE})
    assert isinstance(stand_in, UnconfiguredVoiceModel)
    with pytest.raises(VoiceIntakeError) as exc:
        intake_voice_note(get_sample("meera-impersonation").read_bytes(), llm=stand_in, ledger=sc.showcase_ledger(),
                          guard=RulesGuard())  # fmt: skip
    assert exc.value.code == "NOT_CONFIGURED" and "WAPSI_ASR_BASE_URL" in str(exc.value)
    assert describe_voice_model(stand_in)["backend"] == "unconfigured"


def test_explicit_open_may_use_a_local_model_that_is_not_downloaded_yet(monkeypatch):
    monkeypatch.setattr(B, "local_asr_installed", lambda: True)
    choice = resolve_voice_backend({"WAPSI_VOICE_BACKEND": "open", **LLM, **NO_PROBE})
    assert (choice.backend, choice.asr) == ("open", "local")  # the operator asked: the first note loads the weights


def test_explicit_gemini_and_fake_ignore_everything_else():
    env = {**LLM, **ASR, "GEMINI_API_KEY": "gm-key-not-real"}
    assert resolve_voice_backend({**env, "WAPSI_VOICE_BACKEND": "gemini"}).backend == "gemini"
    assert resolve_voice_backend({**env, "WAPSI_VOICE_BACKEND": "FAKE"}).backend == "fake"
    assert isinstance(build_voice_model({**env, "WAPSI_VOICE_BACKEND": "fake"}), FakeLLM)


def test_the_whole_open_pipeline_runs_offline_with_the_fake_recogniser():
    env = {"WAPSI_VOICE_BACKEND": "open", "WAPSI_ASR_BACKEND": "fake"}
    model = build_voice_model(env)
    assert isinstance(model, OpenVoiceLLM) and isinstance(model.asr, FakeASR)
    sample = get_sample("meera-impersonation")
    r = intake_voice_note(sample.read_bytes(), llm=model, ledger=sc.showcase_ledger(), guard=RulesGuard())
    assert r.matched_ncrp_ids == sample.expect["matched"] and r.counts() == sample.expect["counts"]
    assert r.pipeline.backend == "open" and r.pipeline.label == "fake-asr + fake-transcript-llm"
    assert not r.pipeline.llm_on_amd and not r.pipeline.asr_on_amd  # nothing here claims AMD hardware


# --------------------------------------------------------------------------- models, chain, hardware claims


def test_token_factory_gets_the_default_model_chain_and_the_default_model():
    chain = text_model_chain({"WAPSI_LLM_BASE_URL": TF})
    assert chain.models == ["Gemma-4-31B-IT", "DeepSeek-V4-Flash", "Qwen3.8-27B", "MiniCPM-V-4.7-35B-A3B"]
    assert chain.probe is True and all(m.base_url == TF and m.json_mode == "json_object" for m in chain.llms)
    assert "Authorization" not in chain.llms[0]._client.headers  # no key in this environment, none invented
    custom = text_model_chain({**LLM, "WAPSI_VOICE_LLM_MODELS": "DeepSeek-V4-Flash, Gemma-4-31B-IT",
                               "WAPSI_LLM_COOLDOWN_S": "30", "WAPSI_LLM_HEALTH_PROBE": "0"})  # fmt: skip
    assert custom.models == ["DeepSeek-V4-Flash", "Gemma-4-31B-IT"] and custom.cooldown_s == 30.0 and not custom.probe
    assert custom.llms[0]._client.headers["Authorization"] == "Bearer tf-key-not-real"
    own = text_model_chain({"WAPSI_LLM_BASE_URL": "http://llm:8000/v1", "WAPSI_LLM_MODEL": "wapsi-llm"})
    assert own.models == ["wapsi-llm"] and own.probe is False  # one model on an own vLLM: nothing to fail over to
    with pytest.raises(VoiceBackendError):
        text_model_chain({"WAPSI_LLM_BASE_URL": "http://llm:8000/v1"})


def test_an_editions_own_llm_keeps_its_place_in_the_chain():
    from wapsi.adapters.llm_openai_compat import OpenAICompatLLM

    own = OpenAICompatLLM(TF, "Gemma-4-31B-IT", api_key="k", name="vllm-rocm:Gemma-4-31B-IT")
    chain = text_model_chain({"WAPSI_LLM_API_KEY": "tf-key-not-real"}, text_llm=own)
    assert chain.llms[0] is own and chain.models[1:] == ["DeepSeek-V4-Flash", "Qwen3.8-27B", "MiniCPM-V-4.7-35B-A3B"]
    model = build_voice_model({**ASR, "WAPSI_LLM_API_KEY": "tf-key-not-real"}, text_llm=own)
    assert isinstance(model, OpenVoiceLLM) and model.extractor.llm is own and model.llm_on_amd


def test_on_amd_is_claimed_only_when_it_is_known():
    assert llm_provider(TF, {}) == ("AMD Token Factory", True)
    assert llm_provider("http://llm:8000/v1", {}) == ("llm", False)
    assert llm_provider("http://llm:8000/v1", {"WAPSI_LLM_ON_AMD": "1"}) == ("vLLM-ROCm on AMD", True)
    assert llm_provider("https://amd.com.evil.example/v1", {})[1] is False  # a look-alike host is not AMD
    laptop = build_voice_model({**ASR, "WAPSI_LLM_BASE_URL": "http://127.0.0.1:11434/v1", "WAPSI_LLM_MODEL": "gemma4"})
    assert laptop.label() == "Whisper large-v3-turbo + gemma4" and not laptop.llm_on_amd
    droplet = build_voice_model({**ASR, "WAPSI_ASR_ON_AMD": "1", "WAPSI_LLM_BASE_URL": "http://llm:8000/v1",
                                 "WAPSI_LLM_MODEL": "google/gemma-4-31B-it", "WAPSI_LLM_ON_AMD": "1"})  # fmt: skip
    assert droplet.label() == "Whisper large-v3-turbo + Gemma 4 31B on AMD"
    assert droplet.describe()["asr_on_amd"] and droplet.served_by == "vLLM-ROCm on AMD"


def test_descriptions_and_cache_keys_hold_no_secret():
    env = {**LLM, **ASR, "WAPSI_ASR_API_KEY": "asr-key-not-real", "GEMINI_API_KEY": "gm-key-not-real"}
    sig = env_signature(env)
    flat = json.dumps(sig)
    for secret in ("tf-key-not-real", "asr-key-not-real", "gm-key-not-real"):
        assert secret not in flat
    assert env_signature(env) == sig and env_signature({**env, "WAPSI_LLM_API_KEY": "other"}) != sig
    model = build_voice_model(env)
    d = describe_voice_model(model)
    assert d["backend"] == "open" and d["tone"] == "accent" and d["label"] == model.label()
    assert d["model_chain"][0] == "Gemma-4-31B-IT" and "whisper-large-v3-turbo" in d["hint"]
    dumped = json.dumps(d) + repr(model) + repr(model.asr)
    assert "tf-key-not-real" not in dumped and "asr-key-not-real" not in dumped
    assert model.asr._http._client.headers["Authorization"] == "Bearer asr-key-not-real"
    assert describe_voice_model(FakeLLM(name="offline demo model"))["label"].startswith("Offline demo model")
    assert describe_voice_model(build_voice_model({"GEMINI_API_KEY": "gm-key-not-real"}))["backend"] == "gemini"


def test_asr_key_falls_back_to_the_llm_key_only_on_the_llm_host():
    same = build_voice_model({**LLM, "WAPSI_ASR_BASE_URL": "https://developer.amd.com.cn/whisper/v1"})
    assert same.asr._http._client.headers["Authorization"] == "Bearer tf-key-not-real" and same.asr.on_amd
    other = build_voice_model({**LLM, **ASR})
    assert "Authorization" not in other.asr._http._client.headers
