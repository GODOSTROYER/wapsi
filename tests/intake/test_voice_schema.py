"""The voice-intake response schema, its validation, audio sniffing and privacy helpers."""

from __future__ import annotations

import copy
import io
import json
import typing
import wave

import pytest

from wapsi.intake import voice as v
from wapsi.intake.samples import load_samples
from wapsi.intake.voice import VOICE_SCHEMA, VoiceExtraction, VoiceIntakeError, validate_extraction


def _literal_args(model, field: str) -> set[str]:
    ann = model.model_fields[field].annotation
    if typing.get_origin(ann) is list:
        ann = typing.get_args(ann)[0]
    return set(typing.get_args(ann))


def _wav(seconds: float = 0.2, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def _good() -> dict:
    return copy.deepcopy(load_samples()[0].expected)


def test_schema_and_model_agree():
    props = VOICE_SCHEMA["properties"]
    assert set(VOICE_SCHEMA["required"]) == set(props) == set(VoiceExtraction.model_fields)
    fields = props["fields"]["properties"]
    assert (
        set(fields)
        == set(props["fields"]["required"])
        == set(v.VoiceFields.model_fields)
        == set(v.FIELD_SPECS)
    )
    assert (
        set(props["languages"]["items"]["enum"])
        == set(v.LANGUAGES)
        == _literal_args(VoiceExtraction, "languages")
    )
    amount = props["amounts"]["items"]
    assert set(amount["properties"]) == set(amount["required"]) == set(v.ClaimedAmount.model_fields)
    assert (
        set(amount["properties"]["kind"]["enum"])
        == set(v.AMOUNT_KINDS)
        == _literal_args(v.ClaimedAmount, "kind")
    )
    ref = props["txn_refs"]["items"]
    assert set(ref["properties"]) == set(ref["required"]) == set(v.TxnRef.model_fields)
    assert set(ref["properties"]["kind"]["enum"]) == set(v.REF_KINDS) == _literal_args(v.TxnRef, "kind")
    assert fields["payment_mode"]["properties"]["value"]["enum"] == v.PAYMENT_MODES
    assert fields["fraud_type"]["properties"]["value"]["enum"] == v.FRAUD_TYPES


def test_schema_is_plain_json_without_refs():
    text = json.dumps(VOICE_SCHEMA)
    assert "$ref" not in text and "$defs" not in text  # inline, Gemini response_json_schema friendly
    assert json.loads(text) == VOICE_SCHEMA


def test_every_sample_expectation_validates():
    samples = load_samples()
    assert len(samples) == 6 and {s.world for s in samples} == {"showcase", "demo"}
    for s in samples:
        out = validate_extraction(s.expected)
        assert out.transcript and out.translation_en and out.languages
        assert all(0 <= a.confidence <= 1 for a in out.amounts)


@pytest.mark.parametrize(
    "mutate, where",
    [
        (lambda d: d.pop("transcript"), "transcript"),
        (lambda d: d["fields"]["victim_name"].update(confidence=1.5), "confidence"),
        (lambda d: d.update(extra="x"), "extra"),
        (
            lambda d: d["amounts"].append(
                {"value": -5, "currency": "INR", "kind": "TRANSFER", "spoken_as": "", "confidence": 0.5}
            ),
            "value",
        ),
        (lambda d: d["txn_refs"].append({"ref": "1", "kind": "BITCOIN", "confidence": 0.5}), "kind"),
        (lambda d: d.update(languages=["klingon"]), "languages"),
    ],
)
def test_invalid_answers_are_rejected(mutate, where):
    data = _good()
    mutate(data)
    with pytest.raises(VoiceIntakeError) as exc:
        validate_extraction(data)
    assert exc.value.code == "SCHEMA" and where in str(exc.value)


def test_enum_fields_and_non_json_are_rejected():
    data = _good()
    data["fields"]["payment_mode"]["value"] = "HAWALA"
    with pytest.raises(VoiceIntakeError, match="payment_mode"):
        validate_extraction(data)
    data = _good()
    data["fields"]["fraud_type"]["value"] = "SOMETHING"
    with pytest.raises(VoiceIntakeError, match="fraud_type"):
        validate_extraction(data)
    for bad in ("not json", "[1, 2]", 42):
        with pytest.raises(VoiceIntakeError) as exc:
            validate_extraction(bad)
        assert exc.value.code == "SCHEMA"
    assert validate_extraction(json.dumps(_good())).fields.victim_name.value == "Meera Joshi"


def test_sniff_audio_by_magic_bytes():
    assert v.sniff_audio(_wav()) == "audio/wav"
    assert v.sniff_audio(b"OggS" + b"\x00" * 40) == "audio/ogg"
    assert v.sniff_audio(b"\x1a\x45\xdf\xa3" + b"\x00" * 40) == "audio/webm"
    assert v.sniff_audio(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 40) == "audio/mp4"
    assert v.sniff_audio(b"ID3\x04" + b"\x00" * 40) == "audio/mp3"
    assert v.sniff_audio(b"\xff\xfb\x90\x00" + b"\x00" * 40) == "audio/mp3"
    with pytest.raises(VoiceIntakeError) as exc:
        v.sniff_audio(b"%PDF-1.7 not audio", "complaint.pdf")
    assert exc.value.code == "UNSUPPORTED_AUDIO"
    with pytest.raises(VoiceIntakeError, match="content does not"):
        v.sniff_audio(b"hello, I am a text file", "voice.wav")


def test_wav_duration():
    assert v.wav_duration_s(_wav(1.5)) == 1.5
    assert v.wav_duration_s(b"OggS....") is None


def test_phone_masking_in_fields_and_text():
    assert v.mask_phone("98123 45621") == "98XXXXXX21"
    assert v.mask_phone("+91 98123-45621") == "98XXXXXX21"
    assert v.mask_phone("1234") == "XXXX"
    assert v.mask_phone("") == ""
    text = "Phone number 98123 45621, UTR 4253 1000 0001."
    masked = v._mask_in_text(text, "9812345621")
    assert "98XXXXXX21" in masked and "45621" not in masked and "4253 1000 0001" in masked


def test_normalize_ref():
    assert v.normalize_ref(" 4253-1000 0001 ") == "425310000001"
    assert v.normalize_ref("utr: ab12") == "UTRAB12"


def test_prompt_carries_the_received_date():
    from datetime import datetime

    from wapsi.timeutil import IST

    prompt = v.build_prompt(datetime(2026, 9, 10, 13, 40, tzinfo=IST))
    assert "10 September 2026, 13:40 IST" in prompt and "aaj" in prompt
    assert "UNTRUSTED" in v.SYSTEM and "instruction_like" in v.SYSTEM


def test_human_code_keeps_acronyms():
    assert v.human_code("KYC_FRAUD") == "KYC fraud"
    assert v.human_code("COURIER_SCAM") == "Courier scam"
    assert v.human_code("UPI_FRAUD") == "UPI fraud"
    assert v.human_code("") == ""


def test_phone_mask_accepts_country_and_trunk_prefixes():
    for raw in ("9812345621", "+91 98123 45621", "919812345621", "09812345621", "098123-45621"):
        assert v.mask_phone(raw) == "98XXXXXX21", raw
