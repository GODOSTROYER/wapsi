"""Small pure HTML builders (chips, tiles, tables, empty states).

Output is a single line (no indentation, no blank lines) so ``st.markdown(..., unsafe_allow_html=True)`` treats it
as a raw HTML block. Every dynamic string is escaped here; callers pass plain text.
"""

from __future__ import annotations

import html as _html
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ui.components.theme import STATUS_ICONS

STATUS_TONES = {"good", "warn", "bad"}


def esc(text: object) -> str:
    """HTML-escape. Newlines become ``&#10;`` so a blank line can never end the raw-HTML block early
    (renders as a space normally, as a line break inside ``white-space: pre-wrap``)."""
    out = _html.escape("" if text is None else str(text), quote=True)
    return out.replace("\r\n", "\n").replace("\n", "&#10;")


def chip(text: str, tone: str = "gray", *, title: str | None = None, dot: bool = False) -> str:
    """Pill with text. Status tones carry an icon so colour never works alone."""
    tip = f' title="{esc(title)}"' if title else ""
    icon = ""
    if tone in STATUS_TONES:
        icon = f'<span aria-hidden="true">{STATUS_ICONS[tone]}</span>'
    elif dot:
        icon = f'<span class="wp-chip-dot" style="background:var(--wp-s-{esc(tone)},currentColor)"></span>'
    style = f"color:var(--wp-{esc(tone)}-fg);background:var(--wp-{esc(tone)}-bg)"
    return f'<span class="wp-chip" style="{style}"{tip}>{icon}{esc(text)}</span>'


def chips(items: Iterable[str]) -> str:
    return f'<div class="wp-chips">{"".join(items)}</div>'


def badge(text: str, *, title: str | None = None) -> str:
    tip = f' title="{esc(title)}"' if title else ""
    return f'<span class="wp-badge"{tip}>{esc(text)}</span>'


def icon(tone: str) -> str:
    glyph = {"good": "✓", "bad": "✕", "warn": "!", "gray": "–"}.get(tone, "•")
    return f'<span class="wp-ico wp-ico-{esc(tone)}" aria-hidden="true">{glyph}</span>'


@dataclass
class Tile:
    label: str
    value: str
    note: str = ""
    accent: bool = False
    title: str = ""
    swatch: str | None = None  # series tone shown as a dot before the label


def tiles(items: Sequence[Tile], min_width_rem: float | None = None) -> str:
    style = f' style="grid-template-columns:repeat(auto-fit,minmax({min_width_rem}rem,1fr))"' if min_width_rem else ""
    out = []
    for t in items:
        cls = "wp-tile wp-tile-accent" if t.accent else "wp-tile"
        tip = f' title="{esc(t.title)}"' if t.title else ""
        sw = (f'<span class="wp-chip-dot" style="background:var(--wp-s-{esc(t.swatch)})"></span>'
              if t.swatch else "")
        note = f'<div class="wp-tile-note">{esc(t.note)}</div>' if t.note else ""
        # long figures (e.g. "172.8k / 1.8k") get a smaller display size so they never truncate
        vcls = "wp-tile-value wp-tile-value-long" if len(t.value) > 9 else "wp-tile-value"
        out.append(f'<div class="{cls}"{tip}><div class="wp-tile-label">{sw}{esc(t.label)}</div>'
                   f'<div class="{vcls}">{esc(t.value)}</div>{note}</div>')
    return f'<div class="wp-tiles"{style}>{"".join(out)}</div>'


def page_head(title: str, subtitle: str = "", eyebrow: str = "") -> str:
    eb = f'<div class="wp-eyebrow">{esc(eyebrow)}</div>' if eyebrow else ""
    sub = f"<p>{esc(subtitle)}</p>" if subtitle else ""
    return f'<div class="wp-head">{eb}<h1>{esc(title)}</h1>{sub}</div>'


def section(title: str) -> str:
    return f'<div class="wp-section">{esc(title)}</div>'


def empty_state(title: str, body: str = "", glyph: str = "₹", code: str = "") -> str:
    code_html = f'<p style="margin-top:.5rem"><code>{esc(code)}</code></p>' if code else ""
    return (f'<div class="wp-empty" role="status"><div class="wp-empty-icon" aria-hidden="true">{esc(glyph)}</div>'
            f"<h3>{esc(title)}</h3><p>{esc(body)}</p>{code_html}</div>")


def callout(text: str, tone: str = "accent", *, strong: str = "") -> str:
    cls = "wp-callout wp-callout-warn" if tone == "warn" else "wp-callout"
    lead = f"<b>{esc(strong)}</b> " if strong else ""
    return f'<div class="{cls}" role="note">{lead}{esc(text)}</div>'


def kv(pairs: Iterable[tuple[str, str]], *, mono_keys: Iterable[str] = ()) -> str:
    mono = set(mono_keys)
    cells = []
    for k, v in pairs:
        cls = "v wp-mono" if k in mono else "v"
        cells.append(f'<div><div class="k">{esc(k)}</div><div class="{cls}">{esc(v)}</div></div>')
    return f'<div class="wp-kv">{"".join(cells)}</div>'


def bullet_list(items: Iterable[str]) -> str:
    lis = "".join(f"<li>{esc(i)}</li>" for i in items)
    return f'<ul class="wp-list">{lis}</ul>' if lis else ""


def quote(text: str, label: str = "") -> str:
    lab = f'<div class="wp-quote-label">{esc(label)}</div>' if label else ""
    return f'<div class="wp-quote">{lab}{esc(text)}</div>'


def skeleton(height_rem: float = 12.0) -> str:
    return f'<div class="wp-skel" style="height:{height_rem}rem" aria-busy="true" aria-label="Loading"></div>'


def mono(text: str) -> str:
    return f'<span class="wp-mono">{esc(text)}</span>'


def cite(cid: str, title: str = "") -> str:
    tip = f' title="{esc(title)}"' if title else ""
    return f'<span class="wp-cite"{tip}>{esc(cid)}</span>'


@dataclass
class Cell:
    """Table cell. ``html`` is trusted markup built by this module; ``text`` is escaped."""

    text: str = ""
    html: str | None = None
    right: bool = False
    title: str = ""
    nowrap: bool = False

    def render(self) -> str:
        classes = (["r"] if self.right else []) + (["nw"] if self.nowrap else [])
        cls = f' class="{" ".join(classes)}"' if classes else ""
        tip = f' title="{esc(self.title)}"' if self.title else ""
        body = self.html if self.html is not None else esc(self.text)
        return f"<td{cls}{tip}>{body}</td>"


@dataclass
class Row:
    cells: list[Cell]
    sub: str | None = None  # trusted HTML for a full-width detail line under the row
    selected: bool = False
    extra_classes: list[str] = field(default_factory=list)


def table(headers: Sequence[tuple[str, bool]], rows: Sequence[Row], *, caption: str = "") -> str:
    """headers: [(label, right_aligned)]."""
    right_cls = ' class="r"'
    ths = "".join(f'<th{right_cls if right else ""} scope="col">{esc(h)}</th>' for h, right in headers)
    body = []
    for r in rows:
        cls = " ".join((["wp-sel"] if r.selected else []) + r.extra_classes)
        cls_attr = f' class="{cls}"' if cls else ""
        body.append(f"<tr{cls_attr}>{''.join(c.render() for c in r.cells)}</tr>")
        if r.sub:
            body.append(f'<tr class="wp-sub"><td colspan="{len(headers)}">{r.sub}</td></tr>')
    cap = f'<caption style="caption-side:bottom;text-align:left;padding:.4rem .7rem" class="wp-note">{esc(caption)}</caption>' if caption else ""
    return (f'<div class="wp-table-wrap"><table class="wp-table">{cap}<thead><tr>{ths}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def highlight_spans(text: str, spans: Iterable[str], warn_spans: Iterable[str] = ()) -> str:
    """Escape ``text``; wrap failed-check spans in <mark class="wp-flag"> and warning spans in
    <mark class="wp-flag wp-flag-warn">. Failures win when a span is in both lists."""
    bad = {s for s in spans if s and s.strip()}
    warn = {s for s in warn_spans if s and s.strip()} - bad
    every = sorted(bad | warn, key=len, reverse=True)
    if not every:
        return esc(text)
    pattern = re.compile("|".join(re.escape(s) for s in every))
    out, last = [], 0
    for m in pattern.finditer(text):
        out.append(esc(text[last:m.start()]))
        if m.group(0) in bad:
            out.append(f'<mark class="wp-flag" title="Failed a check">{esc(m.group(0))}</mark>')
        else:
            out.append(f'<mark class="wp-flag wp-flag-warn" title="Check warning">{esc(m.group(0))}</mark>')
        last = m.end()
    out.append(esc(text[last:]))
    return "".join(out)


def document(text: str, flagged_spans: Iterable[str] = (), warn_spans: Iterable[str] = ()) -> str:
    return (f'<div class="wp-doc" tabindex="0" role="document">'
            f"{highlight_spans(text, flagged_spans, warn_spans)}</div>")
