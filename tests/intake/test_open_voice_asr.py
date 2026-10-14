"""Speech-to-text port and adapters, offline: an httpx mock for the transcription endpoint, a fake pipeline for
the local recogniser, the chat-audio probe, decoding and the offline fake. No model is downloaded or loaded."""

from __future__ import annotations

import io
import json
import struct
import wave

import httpx
import numpy as np
import pytest

from wapsi.adapters.asr_openai_compat import ChatAudioASR, OpenAICompatASR, asr_api_key, silence_wav
from wapsi.adapters.asr_transformers import TransformersASR, device_label
from wapsi.intake import asr as A
from wapsi.intake.asr import (
    ASRError,
    FakeASR,
    SpeechToText,
    Transcription,
    clean_segments,
    decode_audio,
    decode_wav,
    language_code,
    real_time_factor,
    requested_language,
    resample,
)
from wapsi.intake.samples import get_sample

BASE = "http://asr.test/v1"


def _wav(seconds: float = 1.0, rate: int = 16_000, channels: int = 1, width: int = 2, freq: float = 440.0) -> bytes:
    n = int(seconds * rate)
    t = np.arange(n) / rate
    tone = (0.5 * np.sin(2 * np.pi * freq * t) * (2 ** (8 * width - 1) - 1)).astype(np.int64)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 2:
            frame = b"".join(struct.pack("<h", int(v)) * channels for v in tone)
        else:
            frame = b"".join((int(v) + 128).to_bytes(1, "little") * channels for v in tone)
        w.writeframes(frame)
    return buf.getvalue()


class _Clock:
    """A clock that advances ``step`` seconds on every reading."""

    def __init__(self, step: float) -> None:
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _form(request: httpx.Request) -> dict[str, str]:
    """Text fields of a multipart body (enough for these tests: values hold no CR / LF)."""
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    out: dict[str, str] = {}
    for part in request.content.split(b"--" + boundary):
        head, _, body = part.partition(b"\r\n\r\n")
        if b'name="' not in head:
            continue
        name = head.split(b'name="')[1].split(b'"')[0].decode()
        if b"filename=" in head:
            out[name] = "<file:" + head.split(b'filename="')[1].split(b'"')[0].decode() + f":{len(body) - 2}>"
            out[name + ".type"] = head.split(b"Content-Type: ")[1].split(b"\r\n")[0].decode()
        else:
            out[name] = body.rstrip(b"\r\n").decode()
    return out


# --------------------------------------------------------------------------- small helpers


def test_language_and_rtf_helpers():
    assert language_code("Hindi") == "hi" and language_code("<|en|>") == "en" and language_code(None) == ""
    assert requested_language("auto") is None and requested_language("") is None and requested_language("HI") == "hi"
    assert real_time_factor(3.0, 30.0) == 0.1 and real_time_factor(1.0, None) is None and real_time_factor(1, 0) is None
    segs = clean_segments(
        [{"start": 0, "end": 1.26, "text": " a "}, {"timestamp": (1.5, None), "text": "b"}, {"text": ""},
         {"timestamp": [4, 5], "text": "c"}, "junk"],
        duration_s=9.0,
    )  # fmt: skip
    assert [(s.start, s.end, s.text) for s in segs] == [(0.0, 1.26, "a"), (1.5, 4.0, "b"), (4.0, 5.0, "c")]
    assert clean_segments([{"timestamp": (2.0, None), "text": "tail"}], 7.5)[0].end == 7.5
    assert A.audio_filename("audio/ogg") == "voice-note.ogg" and A.audio_filename("audio/mp4") == "voice-note.m4a"


# --------------------------------------------------------------------------- OpenAI-compatible endpoint


def test_transcription_endpoint_request_and_verbose_json():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "text": " मेरा नाम मीरा है UTR 4253 1000 0001 ", "language": "hindi", "duration": 30.93,
            "segments": [{"id": 0, "start": 0.0, "end": 4.2, "text": " मेरा नाम मीरा है"},
                         {"id": 1, "start": 4.2, "end": 9.87, "text": " UTR 4253 1000 0001"}],
        })  # fmt: skip

    stt = OpenAICompatASR(BASE, api_key="asr-key-not-real", transport=httpx.MockTransport(handler),
                          clock=_Clock(1.5), on_amd=True)  # fmt: skip
    assert isinstance(stt, SpeechToText) and stt.model == "openai/whisper-large-v3-turbo"
    audio = _wav(0.5)
    out = stt.transcribe(audio, "audio/wav")
    req = seen[0]
    assert str(req.url) == f"{BASE}/audio/transcriptions" and req.headers["authorization"] == "Bearer asr-key-not-real"
    form = _form(req)
    assert form["model"] == "openai/whisper-large-v3-turbo" and form["language"] == "hi"  # the Hinglish default
    assert form["response_format"] == "verbose_json" and form["temperature"] == "0"
    assert form["timestamp_granularities[]"] == "segment" and "prompt" not in form
    assert form["file"] == f"<file:voice-note.wav:{len(audio)}>" and form["file.type"] == "audio/wav"
    assert out.text == "मेरा नाम मीरा है UTR 4253 1000 0001" and out.language == "hi"
    assert [(s.start, s.end) for s in out.segments] == [(0.0, 4.2), (4.2, 9.87)]
    assert out.duration_s == 30.93 and out.elapsed_s == 1.5 and out.rtf == round(1.5 / 30.93, 4)
    assert (out.backend, out.device, out.on_amd, out.model) == ("endpoint", "remote", True, stt.model)
    assert "asr-key-not-real" not in repr(stt) and "***" in repr(stt)


def test_language_prompt_overrides_and_neutral_file_name():
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_form(request))
        return httpx.Response(200, json={"text": "hello"})

    stt = OpenAICompatASR(BASE, "whisper-small", api_key="", language="auto", prompt="UTR, IMPS",
                          transport=httpx.MockTransport(handler))  # fmt: skip
    out = stt.transcribe(b"OggS" + b"\x00" * 64, "audio/ogg")
    assert "language" not in seen[0] and seen[0]["prompt"] == "UTR, IMPS"
    assert seen[0]["file"].startswith("<file:voice-note.ogg:")  # never the caller's own file name
    assert out.language == "" and out.duration_s is None and out.rtf is None
    assert [(s.start, s.text) for s in out.segments] == [(0.0, "hello")]  # one segment when the server sends none
    stt.transcribe(b"OggS" + b"\x00" * 64, "audio/ogg", language="en", prompt="")
    assert seen[1]["language"] == "en" and "prompt" not in seen[1]


def test_steps_down_to_json_when_verbose_json_is_unknown_and_remembers_it():
    formats: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fmt = _form(request)["response_format"]
        formats.append(fmt)
        if fmt == "verbose_json":
            return httpx.Response(400, json={"error": {"message": "response_format verbose_json is not supported"}})
        return httpx.Response(200, json={"text": "namaste", "usage": {"type": "duration", "seconds": 4}})

    stt = OpenAICompatASR(BASE, api_key="", transport=httpx.MockTransport(handler))
    assert stt.transcribe(_wav(0.2), "audio/wav").text == "namaste"
    out = stt.transcribe(_wav(0.2), "audio/wav")
    assert formats == ["verbose_json", "json", "json"] and out.duration_s == 4.0 and out.language == "hi"


def test_plain_text_reply_is_accepted():
    stt = OpenAICompatASR(BASE, api_key="", transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ek do teen")))
    assert stt.transcribe(_wav(0.2), "audio/wav").text == "ek do teen"


def test_retries_429_with_retry_after_then_reports_errors_without_the_key():
    calls, slept = [], []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json={"text": "ok"})

    stt = OpenAICompatASR(BASE, api_key="k", transport=httpx.MockTransport(flaky), sleep=slept.append)
    assert stt.transcribe(_wav(0.2), "audio/wav").text == "ok" and slept == [7.0] and len(calls) == 2

    denied = OpenAICompatASR(BASE, api_key="secret-asr-key-123", sleep=slept.append, transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"detail": {"error": {"message": "bad key"}}})))  # fmt: skip
    with pytest.raises(ASRError) as exc:
        denied.transcribe(_wav(0.2), "audio/wav")
    assert exc.value.status_code == 401 and "WAPSI_ASR_API_KEY" in str(exc.value)
    assert "secret-asr-key-123" not in str(exc.value)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    dead = OpenAICompatASR(BASE, api_key="", max_retries=1, sleep=slept.append, transport=httpx.MockTransport(down))
    with pytest.raises(ASRError) as exc:
        dead.transcribe(_wav(0.2), "audio/wav")
    assert exc.value.code == "ASR_UNAVAILABLE" and "2 attempt" in str(exc.value)


def test_llm_key_is_reused_only_for_the_same_host():
    env = {"WAPSI_LLM_BASE_URL": "https://gpu.example/v1", "WAPSI_LLM_API_KEY": "llm-key"}
    assert asr_api_key("https://gpu.example:8443/v1", env) == "llm-key"
    assert asr_api_key("https://elsewhere.example/v1", env) is None  # never sent to a host it was not issued for
    assert asr_api_key("https://elsewhere.example/v1", {**env, "WAPSI_ASR_API_KEY": "own"}) == "own"
    assert asr_api_key(BASE, {}) is None


# --------------------------------------------------------------------------- chat-audio adapter


def test_chat_audio_probe_and_transcription():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": " Sir, mera naam Meera hai. "}}]})

    stt = ChatAudioASR("http://llm.test/v1", "gemma-audio", api_key="", transport=httpx.MockTransport(handler),
                       clock=_Clock(2.0))  # fmt: skip
    assert stt.probe() is True and stt.probe_detail == "audio parts accepted"
    probe = bodies[0]
    assert probe["max_tokens"] == 8 and probe["messages"][0]["role"] == "system"  # one system message, first
    part = probe["messages"][1]["content"][0]
    assert part["type"] == "input_audio" and part["input_audio"]["format"] == "wav"
    out = stt.transcribe(_wav(2.0), "audio/wav")
    assert out.text == "Sir, mera naam Meera hai." and out.backend == "chat-audio" and out.duration_s == 2.0
    assert out.rtf == 1.0 and len(out.segments) == 1 and out.segments[0].end == 2.0
    assert "UNTRUSTED" in bodies[1]["messages"][0]["content"] and bodies[1]["temperature"] == 0.0
    stt.transcribe(b"OggS" + b"\x00" * 32, "audio/ogg")
    url_part = bodies[2]["messages"][1]["content"][0]
    assert url_part["type"] == "audio_url" and url_part["audio_url"]["url"].startswith("data:audio/ogg;base64,")
    assert len(silence_wav()) < 10_000


def test_chat_audio_probe_is_false_when_the_gateway_rejects_audio():
    """What the AMD Token Factory answered on 2026-10-14: HTTP 400 for audio content parts."""
    reject = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": {"message": "cannot deserialize"}}))
    stt = ChatAudioASR("http://llm.test/v1", "Gemma-4-31B-IT", api_key="", transport=reject)
    assert stt.probe() is False and "400" in stt.probe_detail
    with pytest.raises(ASRError) as exc:
        stt.transcribe(_wav(0.2), "audio/wav")
    assert exc.value.code == "ASR_UNAVAILABLE" and "does not accept audio" in str(exc.value)


# --------------------------------------------------------------------------- local transformers pipeline


class _FakePipe:
    """Stands in for transformers' ASR pipeline: records the call, returns Whisper-shaped chunks."""

    def __init__(self, multilingual: bool = True) -> None:
        self.calls: list[tuple[dict, dict]] = []
        self.model = type("M", (), {"generation_config": type("G", (), {"is_multilingual": multilingual})()})()
        self.tokenizer = type("T", (), {"get_prompt_ids": staticmethod(lambda text, return_tensors=None: [1, 2, 3])})()

    def __call__(self, inputs, **kwargs):
        self.calls.append((inputs, kwargs))
        return {"text": " Sir mera naam Meera Joshi hai. UTR 425310000001.",
                "chunks": [{"timestamp": (0.0, 3.5), "text": " Sir mera naam Meera Joshi hai."},
                           {"timestamp": (3.5, None), "text": " UTR 425310000001."}]}  # fmt: skip


def test_local_pipeline_gets_16k_mono_and_hindi_transcribe_settings():
    pipe = _FakePipe()
    built: list[dict] = []

    def factory(**kw):
        built.append(kw)
        return pipe

    stt = TransformersASR("openai/whisper-large-v3-turbo", pipeline_factory=factory, clock=_Clock(0.75),
                          hardware={"device": "cpu", "accelerator": "cpu", "gpu_name": None, "on_amd": False})  # fmt: skip
    assert isinstance(stt, SpeechToText) and not stt.loaded  # nothing is loaded at construction
    out = stt.transcribe(_wav(2.0, rate=8_000, channels=2), "audio/wav")
    assert built == [{"model": "openai/whisper-large-v3-turbo", "device": "cpu"}] and stt.loaded
    inputs, kwargs = pipe.calls[0]
    assert inputs["sampling_rate"] == 16_000 and inputs["raw"].dtype == np.float32
    assert inputs["raw"].ndim == 1 and abs(len(inputs["raw"]) - 32_000) <= 2  # resampled, down-mixed
    assert kwargs == {"return_timestamps": True, "generate_kwargs": {"task": "transcribe", "language": "hi"}}
    assert out.text.startswith("Sir mera naam") and out.language == "hi" and out.duration_s == 2.0
    assert [(s.start, s.end) for s in out.segments] == [(0.0, 3.5), (3.5, 3.5)]  # an open end never runs backwards
    assert out.elapsed_s == 0.75 and out.rtf == 0.375  # the one-off model load is not part of the factor
    assert (out.backend, out.device, out.on_amd) == ("local", "cpu", False)
    stt.transcribe(_wav(0.5), "audio/wav", language="auto", prompt="UTR, IMPS")
    assert len(built) == 1  # the model stays loaded
    again = pipe.calls[1][1]
    assert again["return_language"] is True and again["generate_kwargs"] == {"task": "transcribe", "prompt_ids": [1, 2, 3]}


def test_local_pipeline_on_a_rocm_gpu_is_labelled_as_amd_and_english_only_models_get_no_language():
    pipe = _FakePipe(multilingual=False)
    rocm = {"device": "cuda:0", "accelerator": "rocm", "gpu_name": "AMD Instinct MI300X", "on_amd": True}
    stt = TransformersASR("openai/whisper-small.en", pipeline_factory=lambda **kw: pipe, hardware=rocm)
    out = stt.transcribe(_wav(0.5), "audio/wav")
    assert "generate_kwargs" not in pipe.calls[0][1]
    assert out.on_amd is True and out.device == "cuda:0 (ROCm: AMD Instinct MI300X)"
    assert device_label({"device": "cuda:0", "accelerator": "cuda", "gpu_name": None}) == "cuda:0 (CUDA)"
    with pytest.raises(ValueError):
        TransformersASR("m", device="tpu")


def test_local_pipeline_failures_are_asr_errors():
    def boom(**kw):
        class P:
            model = tokenizer = None

            def __call__(self, *a, **k):
                raise RuntimeError("out of memory")

        return P()

    stt = TransformersASR("m", pipeline_factory=boom, hardware={"device": "cpu", "accelerator": "cpu"})
    with pytest.raises(ASRError) as exc:
        stt.transcribe(_wav(0.3), "audio/wav")
    assert "out of memory" in str(exc.value) and exc.value.code == "ASR_FAILED"


# --------------------------------------------------------------------------- the real transformers pipeline


def _tiny_whisper(tmp_path):
    """A random-weight Whisper small enough to build in memory: real architecture, real tokenizer class, real
    feature extractor, nothing downloaded. What it "hears" is noise; the call path is what is tested."""
    torch = pytest.importorskip("torch", reason="asr extra not installed (uv sync --extra asr)")
    transformers = pytest.importorskip("transformers", reason="asr extra not installed (uv sync --extra asr)")

    vocab = {tok: i for i, tok in enumerate([chr(i) for i in range(33, 127)] + ["Ġ"])}
    (tmp_path / "vocab.json").write_text(json.dumps(vocab), encoding="utf-8")
    (tmp_path / "merges.txt").write_text("#version: 0.2\n", encoding="utf-8")
    end = "<|endoftext|>"
    tok = transformers.WhisperTokenizer(str(tmp_path / "vocab.json"), str(tmp_path / "merges.txt"), unk_token=end,
                                        bos_token=end, eos_token=end, pad_token=end)  # fmt: skip
    specials = [end, "<|startoftranscript|>", "<|en|>", "<|hi|>", "<|translate|>", "<|transcribe|>", "<|startoflm|>",
                "<|startofprev|>", "<|nospeech|>", "<|notimestamps|>"]  # fmt: skip
    tok.add_special_tokens({"additional_special_tokens": [s for s in specials if s not in tok.get_vocab()]})
    tok.add_tokens([f"<|{i * 0.02:.2f}|>" for i in range(1501)], special_tokens=True)
    ids = tok.convert_tokens_to_ids
    cfg = transformers.WhisperConfig(
        vocab_size=len(tok), num_mel_bins=80, d_model=32, encoder_layers=1, decoder_layers=1,
        encoder_attention_heads=2, decoder_attention_heads=2, encoder_ffn_dim=32, decoder_ffn_dim=32,
        max_source_positions=1500, max_target_positions=448, pad_token_id=ids(end), bos_token_id=ids(end),
        eos_token_id=ids(end), decoder_start_token_id=ids("<|startoftranscript|>"), suppress_tokens=[],
        begin_suppress_tokens=[],
    )  # fmt: skip
    torch.manual_seed(0)
    model = transformers.WhisperForConditionalGeneration(cfg).eval()
    g = model.generation_config
    g.lang_to_id = {"<|en|>": ids("<|en|>"), "<|hi|>": ids("<|hi|>")}
    g.task_to_id = {"transcribe": ids("<|transcribe|>"), "translate": ids("<|translate|>")}
    g.is_multilingual = True
    g.no_timestamps_token_id = ids("<|notimestamps|>")
    g.prev_sot_token_id = ids("<|startofprev|>")
    g.no_speech_token_id = ids("<|nospeech|>")
    g.max_initial_timestamp_index = 50
    g.max_length = 12  # a few tokens per 30 s window keeps this fast
    g.suppress_tokens, g.begin_suppress_tokens, g.forced_decoder_ids = [], [], None
    g.alignment_heads = [[0, 0]]
    g.return_timestamps = False
    extractor = transformers.WhisperFeatureExtractor(feature_size=80)

    def factory(model: str, device: str):  # TransformersASR._build with these objects instead of a model id
        return transformers.pipeline("automatic-speech-recognition", model=whisper, tokenizer=tok,
                                     feature_extractor=extractor, device=device, dtype=torch.float32)  # fmt: skip

    whisper = model
    return factory


def test_local_adapter_drives_the_real_transformers_pipeline(tmp_path):
    """The arguments ``TransformersASR`` sends are the ones the installed transformers accepts: 16 kHz input as a
    dict, Hindi + transcribe, timestamps, language detection. (Clips over Whisper's 30 s window take the same
    arguments; with random weights the long-form loop is too slow to run here.)"""
    factory = _tiny_whisper(tmp_path)
    stt = TransformersASR("tiny-random-whisper", pipeline_factory=factory, language="hi", device="cpu")
    note = stt.transcribe(get_sample("fatima-courier-injection").read_bytes(), "audio/wav")  # 26.75 s
    assert note.duration_s == 26.75 and note.backend == "local" and note.device == "cpu" and not note.on_amd
    assert note.language == "hi" and note.rtf is not None and note.rtf > 0 and note.elapsed_s > 0
    assert all(s.end >= s.start >= 0 for s in note.segments)
    detected = stt.transcribe(_wav(2.0, rate=8_000, channels=2), "audio/wav", language="auto")
    assert detected.language in ("en", "hi") and detected.duration_s == 2.0  # whatever random weights "detect"
    assert stt.load_s is not None and stt.loaded


# --------------------------------------------------------------------------- decoding


def test_wav_decoding_and_resampling():
    samples, rate = decode_wav(_wav(1.0, rate=8_000, channels=2))
    assert rate == 8_000 and samples.dtype == np.float32 and len(samples) == 8_000 and abs(samples).max() <= 1.0
    eight_bit, _ = decode_wav(_wav(0.25, width=1))
    assert eight_bit.dtype == np.float32 and abs(eight_bit).max() <= 1.0
    out = resample(samples, 8_000, 16_000)
    assert abs(len(out) - 16_000) <= 2 and out.dtype == np.float32
    assert resample(samples, 16_000, 16_000) is not None and len(resample(samples, 16_000)) == len(samples)
    audio, duration = decode_audio(_wav(1.5, rate=44_100), "audio/wav")
    assert duration == 1.5 and abs(len(audio) - 24_000) <= 2
    assert A.audio_duration_s(_wav(1.25)) == 1.25 and A.audio_duration_s(b"OggS", "audio/ogg") is None
    assert A.wav_info(b"not a wav") is None


def test_other_containers_need_a_decoder_and_say_so(monkeypatch):
    monkeypatch.setattr(A.shutil, "which", lambda name: None)
    monkeypatch.setattr(A.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ASRError) as exc:
        decode_audio(b"OggS" + b"\x00" * 200, "audio/ogg")
    assert exc.value.code == "ASR_DECODE" and "ffmpeg" in str(exc.value) and "WAPSI_ASR_BASE_URL" in str(exc.value)


# --------------------------------------------------------------------------- offline fake


def test_fake_asr_reads_the_bundled_samples_and_hears_nothing_else():
    s = get_sample("meera-impersonation")
    stt = FakeASR()
    out = stt.transcribe(s.read_bytes(), "audio/wav")
    assert isinstance(stt, SpeechToText) and isinstance(out, Transcription)
    assert out.text == s.script and out.language == "hi" and out.backend == "fake" and out.duration_s == 30.93
    assert out.rtf is None and out.elapsed_s == 0.0  # no speech model ran: no real-time factor is invented
    assert len(out.segments) >= 5 and out.segments[0].start == 0.0 and out.segments[-1].end == pytest.approx(30.93)
    assert " ".join(x.text for x in out.segments) == s.script
    assert stt.calls[0]["sha256"] == s.sha256
    unknown = stt.transcribe(s.read_bytes() + b"\x00", "audio/wav")
    assert unknown.text == "" and unknown.segments == [] and unknown.language == ""
    timed = FakeASR({s.sha256: "ek do teen."}, rtf=0.25).transcribe(s.read_bytes(), "audio/wav")
    assert timed.text == "ek do teen." and timed.rtf == 0.25 and timed.elapsed_s == pytest.approx(30.93 * 0.25, abs=1e-3)


def test_whatsapp_style_ogg_opus_decodes_through_soundfile():
    """WhatsApp voice notes are OGG / Opus: the ``asr`` extra's soundfile reads them without ffmpeg."""
    sf = pytest.importorskip("soundfile", reason="asr extra not installed (uv sync --extra asr)")
    from wapsi.intake.voice import sniff_audio

    tone = (0.3 * np.sin(2 * np.pi * 440 * np.arange(48_000) / 48_000)).astype("float32")
    buf = io.BytesIO()
    try:
        sf.write(buf, tone, 48_000, format="OGG", subtype="OPUS")
    except Exception as exc:  # noqa: BLE001 - an old libsndfile without Opus
        pytest.skip(f"this libsndfile cannot write Opus: {exc}")
    data = buf.getvalue()
    assert sniff_audio(data, "PTT-20261014-WA0001.opus") == "audio/ogg"
    samples, duration = decode_audio(data, "audio/ogg")
    assert samples.dtype == np.float32 and duration == pytest.approx(1.0, abs=0.05)
    assert abs(len(samples) - 16_000) <= 400  # resampled to 16 kHz for Whisper
