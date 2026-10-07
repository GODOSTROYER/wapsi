"""Voice Intake: a scared victim's WhatsApp-style voice note (Hinglish / Hindi / English) becomes a verified,
structured NCRP complaint. Transcript and English translation side by side, extracted fields with confidence,
every claimed number reconciled against the ledger, instruction-like text quoted (never followed), and "Open
case" runs the normal pipeline for the matched NCRP ids (whose amounts come from the ledger, not the recording).
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


def _model_label(llm: Any) -> tuple[str, str]:
    name = str(getattr(llm, "name", type(llm).__name__))
    if name.startswith(("gemini-api:", "vertex:")):
        return f"Gemini · {getattr(llm, 'model', name.split(':', 1)[-1])}", "accent"
    return "Offline demo model · knows the bundled samples", "gray"


def _audio_input(viewer: Any) -> tuple[bytes, str, str | None] | None:
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

        sid = st.selectbox(
            "Sample voice note",
            list(samples),
            key=K_SAMPLE,
            format_func=lambda i: viewer.text(samples[i].title, names(i)),
            help="Synthetic Hinglish voice notes read by an offline Windows voice. Each retells a "
            "showcase complaint.",
        )
        s = samples[sid]
        note = f"{s.duration_s:.0f} s · " if s.duration_s else ""
        md(f'<div class="wp-note">{h.esc(note)}{h.esc(viewer.text(s.notes, names(sid)))}</div>')
        with st.expander("What the recording says (synthetic script)"):
            md(h.quote(viewer.text(s.script, names(sid)), "Romanised Hinglish read by text-to-speech"))
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
    llm = voice_llm()
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
                h.Tile("Language", langs, f"read by {r.extracted_by} in {_took(r.elapsed_ms)}"),
            ]
        )
    )

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
        eyebrow="Complaint intake · multimodal",
    )
    from wapsi.intake.voice import VoiceIntakeResult, sha256_hex

    label, tone = _model_label(voice_llm())
    md(
        h.chips(
            [
                h.chip(label, tone, title="Set GEMINI_API_KEY to transcribe any recording with Gemini"),
                h.chip("Synthetic voices and data", "gray"),
            ]
        )
    )

    picked = _audio_input(ctx.viewer)
    if picked is None:
        return
    data, filename, sample_id = picked
    digest = sha256_hex(data)
    st.audio(data, format="audio/wav" if filename.lower().endswith(".wav") else None)
    if st.button(
        "Transcribe and check",
        key="voice_run",
        type="primary",
        help="One multimodal call returns transcript, translation and fields; Wapsi then checks each "
        "claim against the ledger.",
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
        st.info(
            "The offline demo model only recognises the bundled sample voice notes. Set GEMINI_API_KEY to "
            "transcribe your own recording with Gemini."
        )
        return
    _results(ctx, result)
