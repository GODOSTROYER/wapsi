"""Voice Intake: a scared victim's WhatsApp-style voice note (Hinglish / Hindi / English) becomes a verified,
structured NCRP complaint. Transcript and English translation side by side, extracted fields with confidence,
every claimed number reconciled against the ledger, instruction-like text quoted (never followed), and "Open
case" runs the normal pipeline for the matched NCRP ids (whose amounts come from the ledger, not the recording).

The models come from ``WAPSI_VOICE_BACKEND`` (``wapsi.intake.backends``): the open path (Whisper + Gemma 4 on AMD),
Gemini, or the offline demo model. The page shows which models transcribed and extracted, and how fast.
"""

from __future__ import annotations

import logging
from typing import Any

import streamlit as st

from ui.components import html as h
from ui.components.compat import optional
from ui.data_source import voice_llm
from ui.views.common import K_CASE, K_PAGE, Ctx, flash, head, md
from wapsi.models import AuditEvent
from wapsi.timeutil import utcnow

log = logging.getLogger("wapsi.ui.intake")

K_SOURCE, K_SAMPLE, K_UPLOAD, K_RECORD = "voice_source", "voice_sample", "voice_upload", "voice_record"
K_RESULT, K_ERROR = "_voice_result", "_voice_error"
SOURCES = ["Sample voice notes", "Upload a file", "Record now"]
UPLOAD_TYPES = ["wav", "mp3", "m4a", "ogg", "oga", "opus", "webm"]
STATUS_CHIP = {
    "MATCHED": ("Matched", "good"),
    "MISMATCH": ("Mismatch", "bad"),
    "NOT_FOUND": ("Not in ledger", "warn"),
}
LANG_NAMES = {
    "hi-en": "Hinglish",
    "hi": "Hindi",
    "en": "English",
    "mr": "Marathi",
    "gu": "Gujarati",
    "bn": "Bengali",
    "ta": "Tamil",
    "te": "Telugu",
    "kn": "Kannada",
    "ml": "Malayalam",
    "pa": "Punjabi",
    "ur": "Urdu",
    "other": "Other",
}


# --------------------------------------------------------------------------- helpers


def _conf_chip(c: float) -> str:
    tone = "good" if c >= 0.85 else "warn" if c >= 0.6 else "bad"
    word = "high" if tone == "good" else "medium" if tone == "warn" else "low"
    return h.chip(f"{round(c * 100)}%", tone, title=f"Model confidence: {word}")


def _took(ms: int) -> str:
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def _human(code: str) -> str:
    from wapsi.intake.voice import human_code

    return human_code(code) if code and code != "—" else code


WORLD_LABELS = {"showcase": "showcase ledger", "demo": "demo world (synth seed 7)"}
_IDS: dict[int, tuple[Any, frozenset[str]]] = {}


def _ledger_ncrp_ids(ds: Any) -> frozenset[str] | None:
    """NCRP ids in the active ledger (one lookup per ledger object, cached), or None when unknown."""
    try:
        ledger = ds.ledger() if ds is not None else None
        if ledger is None:
            return None
        hit = _IDS.get(id(ledger))
        if hit is None or hit[0] is not ledger:
            hit = (ledger, frozenset(c.ncrp_id for c in ledger.complaints()))
            if len(_IDS) > 8:
                _IDS.clear()
            _IDS[id(ledger)] = hit
        return hit[1]
    except Exception:  # noqa: BLE001 - ordering the samples is a nicety; never break the page over it
        log.warning("could not list the ledger's complaints", exc_info=True)
        return None


# what the browser's <audio> element is told (the voice model gets the sniffed type itself)
_PLAYBACK = {"audio/mp3": "audio/mpeg"}


def _playback_mime(data: bytes, filename: str) -> tuple[str | None, str]:
    """(MIME type for st.audio, error). The type comes from the bytes, never the name: st.audio needs a real type
    for MP3 / OGG / M4A / WebM uploads, and a file that is not audio is refused before anything plays."""
    from wapsi.intake.voice import MAX_AUDIO_BYTES, VoiceIntakeError, sniff_audio

    if not data:
        return None, "The recording is empty."
    if len(data) > MAX_AUDIO_BYTES:
        return (
            None,
            f"The recording is larger than {MAX_AUDIO_BYTES // (1024 * 1024)} MB; send a shorter one.",
        )
    try:
        mime = sniff_audio(data, filename)
    except VoiceIntakeError as exc:
        return None, f"This file can't be used: {exc}."
    return _PLAYBACK.get(mime, mime), ""


def _voice_model(ds: Any = None) -> Any:
    """The edition's own voice model when its data source provides one (the AMD edition does), else the one
    ``WAPSI_VOICE_BACKEND`` selects."""
    own = getattr(ds, "voice_model", None)
    if callable(own):
        try:
            model = own()
            if model is not None:
                return model
        except Exception:  # noqa: BLE001 - an edition's model is optional; the shared one always builds
            log.warning("the data source's voice model is unavailable", exc_info=True)
    return voice_llm()


def _describe(llm: Any) -> dict[str, Any]:
    from wapsi.intake.backends import describe_voice_model

    return describe_voice_model(llm)


def _model_label(llm: Any) -> tuple[str, str]:
    d = _describe(llm)
    return str(d["label"]), str(d["tone"])


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{seconds % 60:04.1f}"


def _secs(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 1000:.0f} ms" if value < 1 else f"{value:.1f} s"


def _pipeline_pairs(p: Any) -> list[tuple[str, str]]:
    """How the open path read the note: which model did what, where it ran and how long it took."""
    where = {
        "local": "in this app",
        "endpoint": "transcription endpoint",
        "chat-audio": "chat endpoint",
        "fake": "offline stand-in",
    }.get(p.asr_backend, p.asr_backend)
    asr = f"{p.transcribed_by} · {where}" + (f" · {p.asr_device}" if p.asr_device not in ("", "remote") else "")
    pairs = [("Speech-to-text", asr + (" · on AMD" if p.asr_on_amd else ""))]
    if p.audio_s and p.asr_s is not None:
        speed = f" · real-time factor {p.rtf:.2f}" if p.rtf is not None else ""
        pairs.append(("Transcription time", f"{_secs(p.asr_s)} for {p.audio_s:.0f} s of audio{speed}"))
    served = f" · {p.served_by}" if p.served_by else ""
    pairs.append(("Translation and fields", f"{p.structured_by}{served}" + (" · on AMD" if p.llm_on_amd else "")))
    took = _secs(p.extract_s)
    if p.prompt_tokens is not None and p.completion_tokens is not None:
        took += f" · {p.prompt_tokens:,} prompt + {p.completion_tokens:,} output tokens"
    pairs.append(("Extraction time", took))
    checks = f"JSON mode {p.json_mode or 'n/a'} · schema validated"
    if p.attempts > 1:
        checks += f" · {p.attempts} attempts"
    if p.repaired:
        checks += " · answer repaired before validation"
    pairs.append(("Structured output", checks))
    if p.failover or len(p.models_tried) > 1:  # a model was skipped or rejected: say which, and who answered
        pairs.append(("Model chain", "; ".join(p.models_tried)))
    if p.fixes:
        pairs.append(("Checked against the transcript", "; ".join(p.fixes[:4])))
    if p.withheld:
        pairs.append(
            ("Withheld from the text model", f"{p.withheld} instruction-like passage(s): quoted to you, never read by it")
        )
    return pairs


def _audio_input(viewer: Any, ctx_ds: Any = None) -> tuple[bytes, str, str | None] | None:
    """(audio bytes, file name, sample id) from the chosen source, or None while nothing is chosen."""
    from wapsi.intake.samples import load_samples

    source = st.radio(
        "Voice note source", SOURCES, key=K_SOURCE, horizontal=True, label_visibility="collapsed"
    )
    if source == SOURCES[0]:
        samples = {s.sample_id: s for s in load_samples()}
        if not samples:
            md(
                h.empty_state(
                    "No sample voice notes in this build", "Upload or record a voice note instead.", glyph="♪"
                )
            )
            return None

        def names(i: str) -> list[str]:
            value = ((samples[i].expected.get("fields") or {}).get("victim_name") or {}).get("value") or ""
            return [value] if value else []

        known = _ledger_ncrp_ids(ctx_ds)
        here = {i: known is None or set(s.ncrp_ids) <= known for i, s in samples.items()}

        def label(i: str) -> str:
            title = viewer.text(samples[i].title, names(i))
            return title if here[i] else f"{title} · not in this ledger"

        sid = st.selectbox(
            "Sample voice note",
            sorted(samples, key=lambda i: not here[i]),  # the ones this ledger can reconcile first
            key=K_SAMPLE,
            format_func=label,
            help="Synthetic Hinglish voice notes read by an offline Windows voice. Each retells a synthetic "
            "complaint: from the showcase ledger or from the demo world the cloud deployment loads.",
        )
        s = samples[sid]
        if not here[sid]:
            md(
                h.callout(
                    f"This sample retells NCRP {', '.join(s.ncrp_ids)} from the {WORLD_LABELS.get(s.world, s.world)}, "
                    "which this ledger does not hold, so none of its claims can match. Pick a sample listed "
                    "first, or upload a note about a complaint in this ledger.",
                    "warn",
                )
            )
        note = f"{s.duration_s:.0f} s · " if s.duration_s else ""
        md(f'<div class="wp-note">{h.esc(note)}{h.esc(viewer.text(s.notes, names(sid)))}</div>')
        with st.expander("What the recording says (synthetic script)"):
            from wapsi.intake.voice import mask_phone_in_text

            said_phone = ((s.expected.get("fields") or {}).get("phone") or {}).get("value") or ""
            script = mask_phone_in_text(s.script, said_phone)  # masked here like everywhere else on the page
            md(h.quote(viewer.text(script, names(sid)), "Romanised Hinglish read by text-to-speech"))
        return s.read_bytes(), s.filename, s.sample_id
    if source == SOURCES[1]:
        up = st.file_uploader(
            "Voice note file",
            type=UPLOAD_TYPES,
            key=K_UPLOAD,
            help="WAV, MP3, M4A, OGG / Opus (WhatsApp) or WebM, up to 15 MB.",
        )
        if up is None:
            md('<div class="wp-note">Drop a WhatsApp voice note or any short recording.</div>')
            return None
        return up.getvalue(), up.name, None
    recorder = getattr(st, "audio_input", None) or getattr(st, "experimental_audio_input", None)
    if recorder is None:
        md('<div class="wp-note">Recording needs a newer Streamlit; upload a file instead.</div>')
        return None
    rec = recorder("Record a voice note", key=K_RECORD)
    if rec is None:
        md('<div class="wp-note">Press the microphone, speak in Hinglish, Hindi or English, then stop.</div>')
        return None
    return rec.getvalue(), getattr(rec, "name", "") or "recording.wav", None


def _run(ctx: Ctx, data: bytes, filename: str, sample_id: str | None) -> None:
    from wapsi.adapters.llm_openai_compat import LLMError
    from wapsi.intake.voice import VoiceIntakeError, intake_voice_note, sha256_hex
    from wapsi.verify.injection import RulesGuard

    st.session_state.pop(K_ERROR, None)
    llm = _voice_model(ctx.ds)
    try:
        with st.spinner("Listening, transcribing and checking every number against the ledger…"):
            result = intake_voice_note(
                data,
                llm=llm,
                ledger=ctx.ds.ledger(),
                guard=RulesGuard(),
                filename=filename,
                received_at=ctx.now,
                sample_id=sample_id,
            )
    except VoiceIntakeError as exc:
        st.session_state[K_ERROR] = (sha256_hex(data), str(exc))
        st.session_state.pop(K_RESULT, None)
        return
    except LLMError as exc:  # missing key, quota, ...: the message says what to do and never contains the key
        st.session_state[K_ERROR] = (sha256_hex(data), f"The model couldn't read the recording: {exc}"[:600])
        st.session_state.pop(K_RESULT, None)
        return
    except Exception as exc:  # noqa: BLE001 - never break the page on a bad upload
        log.exception("voice intake failed")
        st.session_state[K_ERROR] = (
            sha256_hex(data),
            f"The recording couldn't be processed ({type(exc).__name__}).",
        )
        st.session_state.pop(K_RESULT, None)
        return
    st.session_state[K_RESULT] = result.model_dump(mode="json")
    counts = result.counts()
    ctx.ds.append_audit(
        None,
        AuditEvent(
            at=utcnow(),
            actor=ctx.viewer.actor,
            role=ctx.viewer.role,
            action="voice_intake_run",
            object_type="voice_note",
            object_id=result.audio_sha256[:16],
            details={
                "extracted_by": result.extracted_by,
                "voice_backend": result.pipeline.backend if result.pipeline else "",
                "matched_ncrp_ids": result.matched_ncrp_ids,
                "claims_matched": counts["MATCHED"],
                "claims_mismatched": counts["MISMATCH"],
                "claims_not_found": counts["NOT_FOUND"],
                "instruction_like_text": result.injection_flagged,
            },
        ),
    )


def _open_case(ctx: Ctx, ids: list[str], details: dict[str, Any]) -> None:
    """Button callback (runs before the widgets are rebuilt, so the page and case keys may be set)."""
    out = ctx.ds.open_case_for_ncrp(ids, actor=ctx.viewer.actor, role=ctx.viewer.role, details=details)
    if out.ok and out.bundle is not None:
        st.session_state[K_CASE] = out.bundle.case.case_id
        st.session_state[K_PAGE] = "Case Room"
        flash(out.message, "success")
    else:
        flash(out.message, "info" if out.unavailable else "error")


# --------------------------------------------------------------------------- results


def _results(ctx: Ctx, r: Any) -> None:
    v = ctx.viewer
    name = r.field("victim_name")
    names = [name.value] if name else []

    def mask(text: str) -> str:
        return v.text(text, names, [])

    counts = r.counts()
    clean = (
        r.matched_ncrp_ids and not counts["MISMATCH"] and not counts["NOT_FOUND"] and not r.injection_flagged
    )
    md(h.callout(r.verdict, "accent" if clean else "warn", strong="Ledger check."))
    total = sum(counts.values())
    langs = ", ".join(LANG_NAMES.get(x, x) for x in r.languages) or "not detected"
    md(
        h.tiles(
            [
                h.Tile(
                    "Claims matched",
                    f"{counts['MATCHED']} of {total}" if total else "—",
                    "UTR, amount, time, account…",
                    accent=bool(clean),
                ),
                h.Tile("Mismatched", str(counts["MISMATCH"]), "ledger value is used"),
                h.Tile("Not in ledger", str(counts["NOT_FOUND"]), "verify before acting"),
                h.Tile(
                    "Language",
                    langs,
                    f"read by {r.pipeline.label if r.pipeline and r.pipeline.label else r.extracted_by} "
                    f"in {_took(r.elapsed_ms)}",
                ),
            ]
        )
    )
    if r.pipeline is not None and r.pipeline.backend == "open":
        md(h.section("How it was read"))
        md(h.kv(_pipeline_pairs(r.pipeline)))

    if r.injection_flagged:
        md(
            h.callout(
                "The recording contains text addressed to the system. It is quoted below and was not "
                "followed: a voice note can never release, approve or change a hold.",
                "warn",
                strong="Instruction-like text, not followed.",
            )
        )
        for span in r.flagged_spans[:5]:
            md(h.quote(mask(span), "Quoted from the recording"))
        if r.guard.categories:
            md(h.chips([h.chip(c.replace("_", " "), "bad") for c in r.guard.categories]))

    c1, c2 = st.columns(2)
    with c1:
        md(h.section(f"Transcript · {langs}"))
        md(h.document(mask(r.transcript) or "(nothing transcribed)", r.flagged_spans))
    with c2:
        md(h.section("English translation"))
        md(h.document(mask(r.translation_en) or "(nothing translated)", r.flagged_spans))
    if r.segments:
        with st.expander(f"Timed transcript · {len(r.segments)} speech-to-text segments"):
            md(
                h.table(
                    [("From", False), ("To", False), ("Heard", False)],
                    [
                        h.Row(
                            [
                                h.Cell(_clock(s.start), nowrap=True),
                                h.Cell(_clock(s.end), nowrap=True),
                                h.Cell(mask(s.text)),
                            ]
                        )
                        for s in r.segments[:200]
                    ],
                    caption="Timestamps come from the speech recogniser; the caller's number is masked in code.",
                )
            )

    md(h.section("Extracted complaint fields"))
    rows: list[h.Row] = []
    claimed = h.badge(
        "claimed by complainant", title="Checked against the ledger below; never used for money"
    )
    for f in r.fields:
        value = v.name(f.display) if f.key == "victim_name" else mask(f.display or f.value)
        rows.append(
            h.Row(
                [
                    h.Cell(f.label),
                    h.Cell(html=h.esc(value) + (" " + claimed if f.claimed else "")),
                    h.Cell(html=_conf_chip(f.confidence), right=True),
                ]
            )
        )
    kinds = {"TRANSFER": "Amount paid", "TOTAL_LOSS": "Total lost", "OTHER": "Amount mentioned"}
    from wapsi.money import fmt_inr

    for a in r.amounts:
        shown = fmt_inr(a.value) if a.currency.upper() in ("INR", "") else f"{a.currency} {a.value:,.2f}"
        spoken = f' <span class="wp-note">“{h.esc(a.spoken_as)}”</span>' if a.spoken_as else ""
        rows.append(
            h.Row(
                [
                    h.Cell(kinds.get(a.kind, "Amount")),
                    h.Cell(html=f'<span class="wp-num">{h.esc(shown)}</span> {claimed}{spoken}'),
                    h.Cell(html=_conf_chip(a.confidence), right=True),
                ]
            )
        )
    for t in r.txn_refs:
        rows.append(
            h.Row(
                [
                    h.Cell(t.kind.replace("_", " ")),
                    h.Cell(html=f"{h.mono(t.ref)} {claimed}"),
                    h.Cell(html=_conf_chip(t.confidence), right=True),
                ]
            )
        )
    if rows:
        md(
            h.table(
                [("Field", False), ("Value", False), ("Confidence", True)],
                rows,
                caption="Numbers and identifiers are what the complainant said. The phone is masked in code "
                "before display.",
            )
        )
    else:
        md('<div class="wp-note">No complaint fields were heard.</div>')

    md(h.section("Reconciliation with the ledger"))
    if r.reconciliation:
        recon = []
        for row in r.reconciliation:
            label, tone = STATUS_CHIP[row.status]
            said, ledger = row.claimed, row.ledger
            if row.key == "victim_name":
                said, ledger = v.name(said), (v.name(ledger) if ledger != "—" else ledger)
            elif row.key == "phone":  # already masked in code; auditors see the last two digits only
                said, ledger = v.mobile(said), (v.mobile(ledger) if ledger != "—" else ledger)
            elif row.key == "fraud_type":
                said, ledger = _human(said), _human(ledger)
            sub = f'<span class="wp-note">{h.esc(row.note)}</span>' if row.note else None
            recon.append(
                h.Row(
                    [
                        h.Cell(row.claim),
                        h.Cell(said, nowrap=row.money),
                        h.Cell(ledger),
                        h.Cell(row.ncrp_id or "—", nowrap=True),
                        h.Cell(html=h.chip(label, tone), right=True, nowrap=True),
                    ],
                    sub=sub,
                )
            )
        md(
            h.table(
                [
                    ("Claim", False),
                    ("Claimed by complainant", False),
                    ("Ledger", False),
                    ("NCRP", False),
                    ("Status", True),
                ],
                recon,
                caption="The ledger decides. A claimed amount or UTR that is not in the ledger is shown, never "
                "acted on.",
            )
        )
    else:
        md('<div class="wp-note">Nothing to reconcile.</div>')

    if r.matched_ncrp_ids:
        ids = list(r.matched_ncrp_ids)
        details = {
            "audio_sha256": r.audio_sha256,
            "extracted_by": r.extracted_by,
            "claims_matched": counts["MATCHED"],
            "claims_mismatched": counts["MISMATCH"],
            "claims_not_found": counts["NOT_FOUND"],
            "instruction_like_text": r.injection_flagged,
        }
        disabled = not v.can_run
        st.button(
            f"Open case for NCRP {', '.join(ids)}",
            key="voice_open_case",
            type="primary",
            disabled=disabled,
            on_click=_open_case,
            args=(ctx, ids, details),
            help="Auditors have read-only access."
            if disabled
            else "Runs the money-trail pipeline for the matched complaint(s). Amounts come from the ledger.",
            **optional(st.button, icon=":material/folder_open:"),
        )
    else:
        md(
            h.callout(
                "No NCRP complaint matches this voice note. Ask the caller to register on NCRP (or call "
                "1930) first: Wapsi never opens a case or holds money on a voice note alone.",
                "warn",
            )
        )

    with st.expander("Structured model output (JSON)"):
        payload = r.model_dump(
            mode="json",
            include={
                "languages",
                "fields",
                "amounts",
                "txn_refs",
                "instruction_like",
                "matched_ncrp_ids",
                "extracted_by",
                "audio_sha256",
                "elapsed_ms",
                "pipeline",
            },
        )
        if v.masked:
            payload["fields"] = [f for f in payload["fields"] if f["key"] != "victim_name"]
        st.json(payload, expanded=False)


# --------------------------------------------------------------------------- page


def render(ctx: Ctx) -> None:
    head(
        "Voice intake",
        "A victim's voice note in Hinglish, Hindi or English becomes a structured NCRP complaint in seconds. "
        "Every number is checked against the ledger.",
        eyebrow="Complaint intake · voice",
    )
    from wapsi.intake.voice import VoiceIntakeResult, sha256_hex

    about = _describe(_voice_model(ctx.ds))
    md(
        h.chips(
            [
                h.chip(str(about["label"]), str(about["tone"]), title=str(about.get("hint") or "")),
                h.chip("Synthetic voices and data", "gray"),
            ]
        )
    )
    if about["backend"] == "unconfigured":
        st.warning(f"The voice model is not configured: {about.get('hint')}")

    picked = _audio_input(ctx.viewer, ctx.ds)
    if picked is None:
        return
    data, filename, sample_id = picked
    mime, problem = _playback_mime(data, filename)
    if mime is None:
        st.error(problem)
        return
    digest = sha256_hex(data)
    st.audio(data, format=mime)
    if st.button(
        "Transcribe and check",
        key="voice_run",
        type="primary",
        help=(
            "A speech recogniser transcribes; an open text model translates and extracts the fields; Wapsi "
            "then checks each claim against the ledger."
            if about["backend"] == "open"
            else "One multimodal call returns transcript, translation and fields; Wapsi then checks each "
            "claim against the ledger."
        ),
        **optional(st.button, icon=":material/graphic_eq:"),
    ):
        _run(ctx, data, filename, sample_id)

    err = st.session_state.get(K_ERROR)
    if err and err[0] == digest:
        st.error(err[1])
        return
    stored = st.session_state.get(K_RESULT)
    if not stored or stored.get("audio_sha256") != digest:
        md('<div class="wp-note">Press <b>Transcribe and check</b> to read this voice note.</div>')
        return
    result = VoiceIntakeResult.model_validate(stored)
    if result.empty:
        if result.pipeline is not None and result.pipeline.backend == "open":
            st.info(
                f"No speech was recognised in this recording ({result.pipeline.transcribed_by}). Check the "
                "recording, or the language hint WAPSI_ASR_LANGUAGE."
            )
        else:
            st.info(
                "The offline demo model only recognises the bundled sample voice notes. To transcribe your own "
                "recording on open models set WAPSI_LLM_BASE_URL (Gemma 4 on AMD) and WAPSI_ASR_BASE_URL or "
                "WAPSI_ASR_BACKEND=local (Whisper); or set GEMINI_API_KEY to use Gemini."
            )
        return
    _results(ctx, result)
