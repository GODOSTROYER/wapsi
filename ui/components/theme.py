"""Design tokens and the one CSS block the app injects per run.

Restrained palette: neutral ink derived from Streamlit's own text colour (``currentColor``) so surfaces and borders
work in light and dark themes without detection; one accent (ink blue); categorical hues (validated with the
dataviz palette checker, adjacent pairs, both modes) only for dispositions; status hues only for real state.

No external fonts, images or scripts (Streamlit in Snowflake CSP).
"""

from __future__ import annotations

from wapsi.models import (
    ApprovalDecision,
    CaseStatus,
    ClockStatus,
    Disposition,
    Role,
)

ACCENT_LIGHT = "#256abf"
ACCENT_DARK = "#3987e5"

# Categorical hues: (light, dark). Order validated for adjacent-pair CVD separation in both modes.
SERIES: dict[str, tuple[str, str]] = {
    "blue": ("#2a78d6", "#3987e5"),
    "orange": ("#eb6834", "#d95926"),
    "teal": ("#1baf7a", "#199e70"),
    "yellow": ("#eda100", "#c98500"),
    "violet": ("#4a3aa7", "#9085e9"),
    "red": ("#e34948", "#e66767"),
    "gray": ("#8a8f9c", "#8a8f9c"),
    "ink": ("#1b1d23", "#eef0f4"),
}

# Status hues (fixed, never reused as series colours); always paired with an icon + label.
STATUS: dict[str, str] = {"good": "#0ca30c", "warn": "#fab219", "serious": "#ec835a", "bad": "#d03b3b"}

# Chip tones: (base hue, share of hue mixed into the inherited text colour). The foreground is
# color-mix(base, currentColor) and the background a 14% tint, so chips follow Streamlit's light/dark theme
# without detection. Shares were chosen so text stays >= 4.6:1 on its tint in both default themes.
CHIP_TONES: dict[str, tuple[str, int]] = {
    "blue": ("#2a78d6", 70),
    "orange": ("#eb6834", 55),
    "teal": ("#1baf7a", 50),
    "yellow": ("#c98500", 50),
    "violet": ("#6a5bd0", 75),
    "red": ("#e34948", 65),
    "gray": ("#8a8f9c", 55),
    "good": ("#0ca30c", 55),
    "warn": ("#c98500", 50),
    "bad": ("#d03b3b", 75),
    "accent": ("#256abf", 70),
}
CHIP_TINT = 14

STATUS_ICONS = {"good": "✓", "warn": "!", "bad": "✕", "gray": "•", "accent": "•"}

DISPOSITION_TONE: dict[Disposition, str] = {
    Disposition.HOLD_AMOUNT: "blue",
    Disposition.OUTBOUND_CFCFRMS_REQUEST: "orange",
    Disposition.HOLD_AMOUNT_NO_ESCALATION: "teal",
    Disposition.NO_HOLD_UPDATE_ONLY: "yellow",
    Disposition.FORWARD_HOLD: "violet",
    Disposition.HOLD_IN_POOL: "violet",
    # an amount hold like HOLD_AMOUNT; the cylinder shape and the label tell them apart
    Disposition.HOLD_AMOUNT_INTERMEDIARY: "blue",
    Disposition.CASHED_OUT: "red",
    Disposition.ESCALATE_S106_CANDIDATE: "blue",
    Disposition.NO_ACTION: "gray",
}

ROLE_TONE: dict[Role, str] = {
    Role.VICTIM: "gray",
    Role.MULE: "red",
    Role.INNOCENT_HOLDER: "teal",
    Role.MERCHANT: "yellow",
    Role.INTERMEDIARY: "gray",
    Role.LEGIT: "gray",
    Role.UNKNOWN: "gray",
}

CASE_STATUS_TONE: dict[CaseStatus, str] = {
    CaseStatus.NEW: "accent",
    CaseStatus.TRACED: "accent",
    CaseStatus.PLANNED: "warn",
    CaseStatus.DRAFTED: "warn",
    CaseStatus.VERIFIED: "warn",
    CaseStatus.APPROVED: "good",
    CaseStatus.CLOSED: "gray",
}

CLOCK_STATUS_TONE: dict[ClockStatus, str] = {
    ClockStatus.OPEN: "gray",
    ClockStatus.WARNING: "warn",
    ClockStatus.OVERDUE: "bad",
    ClockStatus.DONE: "good",
}

APPROVAL_TONE: dict[ApprovalDecision | None, str] = {
    ApprovalDecision.APPROVED: "good",
    ApprovalDecision.CHANGES_REQUESTED: "warn",
    ApprovalDecision.REJECTED: "bad",
    None: "gray",
}

CLOCK_STATUS_HEX: dict[ClockStatus, str] = {
    ClockStatus.OPEN: "#2a78d6",
    ClockStatus.WARNING: STATUS["warn"],
    ClockStatus.OVERDUE: STATUS["bad"],
    ClockStatus.DONE: STATUS["good"],
}

MONO = '"Source Code Pro", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'


def series_hex(tone: str, dark: bool = False) -> str:
    light, dk = SERIES.get(tone, SERIES["gray"])
    return dk if dark else light


def accent(dark: bool = False) -> str:
    return ACCENT_DARK if dark else ACCENT_LIGHT


def _tone_vars(dark: bool) -> str:
    parts = []
    for tone, (base, share) in CHIP_TONES.items():
        parts.append(f"--wp-{tone}-fg:color-mix(in srgb,{base} {share}%,currentColor);"
                     f"--wp-{tone}-bg:color-mix(in srgb,{base} {CHIP_TINT}%,transparent);")
    for tone, (light, dk) in SERIES.items():
        parts.append(f"--wp-s-{tone}:{dk if dark else light};")
    parts.append(f"--wp-accent:{accent(dark)};")
    return "".join(parts)


_CSS = """
.wp-root, .wp-root *{box-sizing:border-box}
[data-testid="stAppViewContainer"] .block-container{padding-top:2.2rem;padding-bottom:3rem;max-width:1480px}
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p{margin-bottom:.25rem}
.wp-muted{color:color-mix(in srgb,currentColor 72%,transparent)}
.wp-mono{font-family:__MONO__;font-size:.92em;letter-spacing:-.01em}
.wp-num{font-variant-numeric:tabular-nums}
.wp-brand{display:flex;align-items:center;gap:.6rem;margin:.1rem 0 .9rem}
.wp-brand-mark{width:2.1rem;height:2.1rem;border-radius:.55rem;background:var(--wp-accent);color:#fff;
 display:flex;align-items:center;justify-content:center;font-weight:700;font-size:1.15rem}
.wp-brand-name{font-weight:700;font-size:1.15rem;line-height:1.1}
.wp-brand-sub{font-size:.78rem;color:color-mix(in srgb,currentColor 72%,transparent)}
.wp-head{margin:0 0 1rem}
.wp-head h1{font-size:1.65rem;font-weight:700;margin:0;padding:0;line-height:1.25}
.wp-head p{margin:.2rem 0 0;color:color-mix(in srgb,currentColor 72%,transparent);font-size:.98rem}
.wp-eyebrow{font-size:.74rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
 color:color-mix(in srgb,currentColor 72%,transparent);margin:0 0 .15rem}
.wp-casebar{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem .9rem;padding:.6rem .85rem;margin:0 0 1rem;
 border:1px solid color-mix(in srgb,currentColor 14%,transparent);border-radius:.6rem;
 background:color-mix(in srgb,currentColor 3%,transparent)}
.wp-casebar b{font-weight:600}
.wp-sep{width:1px;height:1.1rem;background:color-mix(in srgb,currentColor 18%,transparent)}
.wp-chip{display:inline-flex;align-items:center;gap:.3rem;padding:.12rem .55rem;border-radius:999px;
 font-size:.78rem;font-weight:600;line-height:1.35;white-space:nowrap;vertical-align:middle;
 color:var(--wp-gray-fg);background:var(--wp-gray-bg)}
.wp-chip-dot{width:.5rem;height:.5rem;border-radius:50%;display:inline-block;flex:none}
.wp-chips{display:flex;flex-wrap:wrap;gap:.35rem;align-items:center}
.wp-badge{display:inline-flex;align-items:center;gap:.35rem;padding:.2rem .6rem;border-radius:.4rem;
 font-size:.8rem;font-weight:600;border:1px solid color-mix(in srgb,var(--wp-accent) 45%,transparent);
 color:var(--wp-accent-fg);background:var(--wp-accent-bg)}
.wp-tiles{display:grid;gap:.6rem;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));margin:0 0 .9rem}
.wp-tile{padding:.7rem .85rem;border-radius:.6rem;border:1px solid color-mix(in srgb,currentColor 13%,transparent);
 background:color-mix(in srgb,currentColor 2.5%,transparent);min-width:0}
.wp-tile-label{font-size:.78rem;color:color-mix(in srgb,currentColor 72%,transparent);display:flex;gap:.35rem;
 align-items:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.wp-tile-value{font-size:1.45rem;font-weight:700;line-height:1.25;font-variant-numeric:tabular-nums;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.wp-tile-note{font-size:.76rem;color:color-mix(in srgb,currentColor 72%,transparent);margin-top:.1rem}
.wp-tile-accent{border-color:color-mix(in srgb,var(--wp-accent) 55%,transparent);
 background:color-mix(in srgb,var(--wp-accent) 7%,transparent)}
.wp-tile-accent .wp-tile-value{color:var(--wp-accent-fg)}
.wp-strip{display:grid;grid-template-columns:1fr auto 1fr;gap:1rem;align-items:center;padding:.9rem 1.1rem;
 margin:0 0 1rem;border-radius:.7rem;border:1px solid color-mix(in srgb,currentColor 13%,transparent);
 background:color-mix(in srgb,currentColor 2.5%,transparent)}
.wp-strip-side .wp-strip-k{font-size:.78rem;color:color-mix(in srgb,currentColor 72%,transparent)}
.wp-strip-side .wp-strip-v{font-size:1.5rem;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.2}
.wp-strip-side .wp-strip-n{font-size:.82rem;color:color-mix(in srgb,currentColor 72%,transparent)}
.wp-strip-bad .wp-strip-v{text-decoration:line-through;text-decoration-thickness:2px;
 text-decoration-color:color-mix(in srgb,currentColor 45%,transparent);
 color:color-mix(in srgb,currentColor 72%,transparent)}
.wp-strip-good .wp-strip-v{color:var(--wp-accent-fg)}
.wp-strip-mid{text-align:center;font-size:.8rem;color:color-mix(in srgb,currentColor 72%,transparent)}
.wp-strip-msg .wp-note{display:block;font-weight:400;margin-top:.15rem}
.wp-strip-mid b{display:block;font-size:1.25rem;color:var(--wp-accent-fg)}
.wp-strip-msg{grid-column:1/-1;font-size:.95rem;font-weight:600;padding-top:.55rem;
 border-top:1px solid color-mix(in srgb,currentColor 10%,transparent)}
.wp-table-wrap{overflow-x:auto;border:1px solid color-mix(in srgb,currentColor 13%,transparent);border-radius:.6rem;
 margin:0 0 1rem;scrollbar-width:thin}
.wp-table{width:100%;border-collapse:collapse;font-size:.88rem}
.wp-table th{text-align:left;font-weight:600;font-size:.76rem;letter-spacing:.02em;padding:.55rem .7rem;
 color:color-mix(in srgb,currentColor 72%,transparent);background:color-mix(in srgb,currentColor 3.5%,transparent);
 border-bottom:1px solid color-mix(in srgb,currentColor 13%,transparent);white-space:nowrap}
.wp-table td{padding:.5rem .7rem;border-bottom:1px solid color-mix(in srgb,currentColor 8%,transparent);
 vertical-align:top}
.wp-table tr:last-child td{border-bottom:none}
.wp-table .wp-mono, .wp-table td.nw{white-space:nowrap}
.wp-table .wp-chip{white-space:normal}
.wp-cell-rec{max-width:15rem;display:flex;flex-wrap:wrap;gap:.25rem}
.wp-table td.r, .wp-table th.r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.wp-table tr.wp-sub td{padding-top:0;color:color-mix(in srgb,currentColor 75%,transparent);font-size:.84rem}
.wp-table tr.wp-sel td{background:color-mix(in srgb,var(--wp-accent) 6%,transparent)}
.wp-cite{display:inline-block;margin:0 .25rem .15rem 0;padding:.05rem .4rem;border-radius:.3rem;font-size:.74rem;
 border:1px solid color-mix(in srgb,currentColor 16%,transparent);white-space:nowrap}
.wp-empty{padding:2rem 1.5rem;text-align:center;border:1px dashed color-mix(in srgb,currentColor 22%,transparent);
 border-radius:.8rem;margin:.25rem 0 1rem;background:color-mix(in srgb,currentColor 2%,transparent)}
.wp-empty-icon{width:2.6rem;height:2.6rem;margin:0 auto .6rem;border-radius:50%;display:flex;align-items:center;
 justify-content:center;font-size:1.2rem;background:var(--wp-accent-bg);color:var(--wp-accent-fg)}
.wp-empty h3{font-size:1.05rem;margin:0 0 .3rem;padding:0;font-weight:650}
.wp-empty p{margin:0 auto;max-width:36rem;font-size:.9rem;color:color-mix(in srgb,currentColor 72%,transparent)}
.wp-empty code{font-size:.85em}
.wp-kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(8.5rem,1fr));gap:.55rem .9rem;margin:.4rem 0 .8rem}
.wp-kv div{min-width:0}
.wp-kv .k{font-size:.74rem;color:color-mix(in srgb,currentColor 72%,transparent)}
.wp-kv .v{font-weight:600;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.wp-section{font-size:.8rem;font-weight:650;letter-spacing:.05em;text-transform:uppercase;margin:.9rem 0 .35rem;
 color:color-mix(in srgb,currentColor 72%,transparent)}
.wp-list{margin:.1rem 0 .6rem;padding-left:1.1rem}
.wp-list li{margin:.15rem 0;font-size:.9rem}
.wp-legend{display:flex;flex-wrap:wrap;gap:.35rem .9rem;font-size:.78rem;margin:.35rem 0 .2rem;
 color:color-mix(in srgb,currentColor 78%,transparent)}
.wp-legend span{display:inline-flex;align-items:center;gap:.35rem}
.wp-sw{width:.95rem;height:.7rem;border-radius:.2rem;border:1.5px solid;display:inline-block}
.wp-sw-dash{border-style:dashed}
.wp-sw-cyl{border-radius:.45rem/.25rem}
.wp-sw-oct{clip-path:polygon(30% 0,70% 0,100% 30%,100% 70%,70% 100%,30% 100%,0 70%,0 30%)}
.wp-checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(10.5rem,1fr));gap:.5rem;margin:.3rem 0 .8rem}
.wp-check{padding:.55rem .7rem;border-radius:.55rem;border:1px solid color-mix(in srgb,currentColor 13%,transparent)}
.wp-check-h{display:flex;align-items:center;gap:.4rem;font-weight:650;font-size:.9rem}
.wp-check-m{font-size:.78rem;margin-top:.15rem;color:color-mix(in srgb,currentColor 72%,transparent)}
.wp-check-pass{border-color:color-mix(in srgb,var(--wp-good-fg) 35%,transparent)}
.wp-check-fail{border-color:color-mix(in srgb,var(--wp-bad-fg) 55%,transparent);background:var(--wp-bad-bg)}
.wp-check-warn{border-color:color-mix(in srgb,var(--wp-warn-fg) 45%,transparent)}
.wp-ico{display:inline-flex;align-items:center;justify-content:center;width:1.15rem;height:1.15rem;border-radius:50%;
 font-size:.72rem;font-weight:800;flex:none}
.wp-ico-good{background:var(--wp-good-bg);color:var(--wp-good-fg)}
.wp-ico-bad{background:var(--wp-bad-bg);color:var(--wp-bad-fg)}
.wp-ico-warn{background:var(--wp-warn-bg);color:var(--wp-warn-fg)}
.wp-ico-gray{background:var(--wp-gray-bg);color:var(--wp-gray-fg)}
.wp-doc{white-space:pre-wrap;font-family:__MONO__;font-size:.84rem;line-height:1.55;padding:1rem 1.1rem;
 border-radius:.6rem;border:1px solid color-mix(in srgb,currentColor 13%,transparent);
 background:color-mix(in srgb,currentColor 2.5%,transparent);max-height:34rem;overflow:auto;scrollbar-width:thin}
.wp-doc mark.wp-flag{background:var(--wp-bad-bg);color:inherit;border-bottom:2px solid var(--wp-bad-fg);
 padding:0 .1rem;border-radius:.15rem}
.wp-doc mark.wp-flag-warn{background:var(--wp-warn-bg);border-bottom-color:var(--wp-warn-fg)}
.wp-quote{border-left:3px solid color-mix(in srgb,currentColor 25%,transparent);padding:.35rem .8rem;margin:.3rem 0 .7rem;
 font-size:.9rem;background:color-mix(in srgb,currentColor 2.5%,transparent);border-radius:0 .4rem .4rem 0}
.wp-quote-label{font-size:.72rem;font-weight:600;color:color-mix(in srgb,currentColor 72%,transparent);
 text-transform:uppercase;letter-spacing:.05em}
.wp-row{display:flex;flex-wrap:wrap;align-items:baseline;gap:.2rem .6rem}
.wp-row-title{font-weight:650;font-size:1rem}
.wp-big{font-size:1.35rem;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.2}
.wp-note{font-size:.8rem;color:color-mix(in srgb,currentColor 72%,transparent)}
.wp-callout{padding:.6rem .85rem;border-radius:.55rem;font-size:.88rem;margin:.2rem 0 .8rem;
 border:1px solid color-mix(in srgb,var(--wp-accent) 35%,transparent);background:var(--wp-accent-bg)}
.wp-callout-warn{border-color:color-mix(in srgb,var(--wp-warn-fg) 40%,transparent);background:var(--wp-warn-bg)}
.wp-skel{border-radius:.6rem;margin:.3rem 0 1rem;background:linear-gradient(90deg,
 color-mix(in srgb,currentColor 5%,transparent) 25%,color-mix(in srgb,currentColor 10%,transparent) 37%,
 color-mix(in srgb,currentColor 5%,transparent) 63%);background-size:400% 100%;animation:wp-shimmer 1.4s ease infinite}
@keyframes wp-shimmer{0%{background-position:100% 50%}100%{background-position:0 50%}}
@media (prefers-reduced-motion:reduce){.wp-skel{animation:none}}
.wp-foot{font-size:.74rem;color:color-mix(in srgb,currentColor 72%,transparent);margin-top:.4rem;line-height:1.45}
@media (max-width:640px){.wp-strip{grid-template-columns:1fr}.wp-strip-mid{text-align:left}}
"""


def css(dark: bool | None = None) -> str:
    """Full <style> block. Chip colours derive from the inherited text colour, so they need no theme detection;
    ``dark`` only selects the series swatch steps (unknown -> light steps, which read on both themes)."""
    body = _CSS.replace("__MONO__", MONO)
    tokens = f":root{{{_tone_vars(bool(dark))}}}"
    return "<style>" + tokens + " ".join(line.strip() for line in body.strip().splitlines()) + "</style>"


def detect_dark() -> bool | None:
    """Best-effort theme detection: ``st.context.theme`` (1.46+), then ``theme.base`` config; None if unknown."""
    try:
        import streamlit as st

        ctx_theme = getattr(getattr(st, "context", None), "theme", None)
        kind = None
        if ctx_theme is not None:
            kind = getattr(ctx_theme, "type", None)
            if kind is None and hasattr(ctx_theme, "get"):
                kind = ctx_theme.get("type")
        if kind in ("dark", "light"):
            return kind == "dark"
        base = st.get_option("theme.base")
        if base in ("dark", "light"):
            return base == "dark"
    except Exception:  # noqa: BLE001 - theme detection must never break a page
        return None
    return None


def inject_css(dark: bool | None = None) -> None:
    """Inject the stylesheet (call once per script run, before any other output)."""
    import streamlit as st

    st.markdown(css(dark), unsafe_allow_html=True)
