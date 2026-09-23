"""TraceResult + HoldPlan -> Graphviz DOT for ``st.graphviz_chart`` (rendered client-side; no graphviz package).

Visual grammar
- layers run left to right (one rank per layer; the victim is layer 0)
- edge label = victim money carried (short Indian format); edge width grows with it
- node colour = recommended disposition (legend via :func:`legend_items`); every node also states its
  recommendation in text, so colour is never the only cue
- other-bank accounts are dashed; edges learned through CFCFRMS responses are dashed
- pool / nodal / escrow accounts are cylinders; merchants are tabs; ATM / crypto exits are octagon terminals
- the selected account gets a heavy accent outline
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ui.components.formatting import (
    DISPOSITION_LABELS,
    channel_label,
    humanize,
    inr,
    inr_short,
    ist,
    ist_clock,
)
from ui.components.html import esc
from ui.components.theme import DISPOSITION_TONE, accent, series_hex
from wapsi.models import (
    INTERMEDIARY_ACCOUNT_TYPES,
    AccountState,
    AccountType,
    Channel,
    ClassificationResult,
    Disposition,
    HoldDecision,
    HoldPlan,
    Role,
    TerminalKind,
    TraceResult,
    TrailEdge,
)

MIN_PEN = 1.0
MAX_PEN = 6.0
EXIT_CHANNELS = {Channel.ATM: "ATM cash-out", Channel.CRYPTO: "Crypto exit"}
NAME_MAX = 18
NL = "\n"

# Short role words for node headers (the inspector uses the full labels).
GRAPH_ROLE: dict[Role, str] = {
    Role.VICTIM: "Victim",
    Role.MULE: "Mule",
    Role.INNOCENT_HOLDER: "Innocent?",
    Role.MERCHANT: "Merchant",
    Role.INTERMEDIARY: "Pool / nodal",
    Role.LEGIT: "Legitimate",
    Role.UNKNOWN: "Unclassified",
}

_SURFACE = {False: "#ffffff", True: "#0e1117"}
_TEXT = {False: "#1b1d23", True: "#eef0f4"}
_SUBTLE = {False: "#4a4f5a", True: "#c3c7d1"}
_EDGE = {False: "#8a8f9c", True: "#7a8191"}


def q(text: object) -> str:
    """Quote a DOT string (ids, labels, tooltips)."""
    s = str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "\\n")
    return f'"{s}"'


def exit_node_id(edge: TrailEdge) -> str:
    return f"exit::{edge.txn_id}"


def penwidth(tainted: float, max_tainted: float) -> float:
    if max_tainted <= 0:
        return MIN_PEN
    return round(MIN_PEN + (MAX_PEN - MIN_PEN) * max(0.0, tainted) / max_tainted, 2)


def _attrs(**kw: object) -> str:
    return "[" + " ".join(f"{k}={v if isinstance(v, (int, float)) else q(v)}" for k, v in kw.items()) + "]"


def _is_pool(st: AccountState) -> bool:
    return st.terminal == TerminalKind.NODAL_POOL_ESCROW or st.account_type in INTERMEDIARY_ACCOUNT_TYPES


def _is_merchant(st: AccountState) -> bool:
    return st.terminal == TerminalKind.MERCHANT or st.account_type == AccountType.MERCHANT


def _role_of(acct: str, st: AccountState, dec: HoldDecision | None,
             classifications: dict[str, ClassificationResult]) -> Role:
    if st.layer == 0:
        return Role.VICTIM
    if dec is not None and dec.role != Role.UNKNOWN:
        return dec.role
    c = classifications.get(acct)
    return c.role if c is not None else Role.UNKNOWN


def _clip(text: str, n: int = NAME_MAX) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def action_text(dec: HoldDecision | None, st: AccountState) -> str:
    """Recommendation shown inside the node (may contain a line break)."""
    if dec is None:
        return f"Victim money {inr_short(st.tainted_now_total)}" if st.tainted_now_total else "No recommendation"
    d = dec.disposition
    fwd = NL + f"Fwd hold {inr_short(dec.forward_hold_amount)}" if dec.forward_hold_amount else ""
    if d == Disposition.HOLD_AMOUNT:
        return f"Hold {inr_short(dec.hold_amount)}{fwd}"
    if d == Disposition.HOLD_AMOUNT_NO_ESCALATION:
        return f"Hold {inr_short(dec.hold_amount)}" + NL + "no escalation"
    if d == Disposition.HOLD_IN_POOL:
        return f"Hold in pool {inr_short(dec.hold_amount)}"
    if d == Disposition.FORWARD_HOLD:
        return f"Forward hold {inr_short(dec.forward_hold_amount)}"
    if d == Disposition.NO_HOLD_UPDATE_ONLY:
        return f"No hold · {inr_short(dec.tainted_amount)}"
    if d == Disposition.HOLD_AMOUNT_INTERMEDIARY:
        return f"Hold {inr_short(dec.hold_amount)}" + NL + "no suspension"
    if d == Disposition.OUTBOUND_CFCFRMS_REQUEST:
        return f"Request {inr_short(dec.tainted_amount)}"
    if d == Disposition.CASHED_OUT:
        return "Cashed out"
    if dec.hold_amount:
        return f"Hold {inr_short(dec.hold_amount)}"
    return DISPOSITION_LABELS.get(d, humanize(d.value))


def build_trail_dot(
    trace: TraceResult,
    plan: HoldPlan | None = None,
    *,
    classifications: dict[str, ClassificationResult] | None = None,
    selected: str | None = None,
    dark: bool = False,
    names: dict[str, str] | None = None,
    bank_names: dict[str, str] | None = None,
) -> str:
    """Build the DOT source. ``names`` maps account_id -> display name (already masked for auditors)."""
    classifications = classifications or {}
    names = names or {}
    bank_names = bank_names or {}
    decisions = {d.account_id: d for d in (plan.decisions if plan else [])}
    text, subtle, edge_col = _TEXT[dark], _SUBTLE[dark], _EDGE[dark]
    alpha = "40" if dark else "26"
    sel_col = accent(dark)

    lines = [
        "digraph wapsi_trail {",
        f"  graph {_attrs(rankdir='LR', bgcolor=_SURFACE[dark], pad='0.15', nodesep='0.16', ranksep='0.5', fontname='Helvetica', newrank='true', forcelabels='true', tooltip='Money trail')};",
        f"  node {_attrs(fontname='Helvetica', fontsize=11, fontcolor=text, margin='0.1,0.05', penwidth=1.4)};",
        f"  edge {_attrs(fontname='Helvetica', fontsize=10, fontcolor=subtle, color=edge_col, arrowsize=0.7)};",
    ]

    ranks: dict[int, list[str]] = defaultdict(list)
    sent_by_victim: dict[str, float] = defaultdict(float)
    for e in trace.edges:
        if e.depth == 1 and e.from_account:
            sent_by_victim[e.from_account] += e.tainted_total

    for acct, st in sorted(trace.accounts.items(), key=lambda kv: (kv[1].layer, kv[0])):
        dec = decisions.get(acct)
        role = _role_of(acct, st, dec, classifications)
        is_victim = st.layer == 0
        if is_victim:
            tone, shape = "ink", "box"
            head = "Victim"
            action = f"Reported {inr_short(sent_by_victim.get(acct, 0.0))}"
        else:
            tone = DISPOSITION_TONE.get(dec.disposition, "gray") if dec else "gray"
            shape = "cylinder" if _is_pool(st) else ("tab" if _is_merchant(st) else "box")
            head = f"{GRAPH_ROLE.get(role, role.value)} · L{st.layer}"
            action = action_text(dec, st)
        lab_lines = [head, acct]
        if acct in names:
            lab_lines.insert(1, _clip(names[acct]))
        lab_lines.append(action)
        if dec is not None and dec.requires_human:
            lab_lines.append("Human decision")
        if not st.is_home_bank and not is_victim:
            lab_lines.append(f"{st.bank_id} · other bank")

        colour = series_hex(tone, dark)
        styles = ["filled"]
        if shape == "box":
            styles.append("rounded")
        if not st.is_home_bank and not is_victim:
            styles.append("dashed")
        is_sel = acct == selected
        if is_victim:
            fill = colour + ("33" if dark else "1f")
        else:
            fill = colour + ("66" if is_sel else alpha)
        attrs = {
            "label": "\n".join(lab_lines),
            "shape": shape,
            "style": ",".join(styles),
            "color": sel_col if is_sel else colour,
            "fillcolor": fill,
            "penwidth": 3.2 if is_sel else (1.8 if is_victim else 1.4),
            "tooltip": (f"{acct} · {bank_names.get(st.bank_id, st.bank_id)} · balance {inr(st.balance_now)}"
                        f" · victim money now {inr(st.tainted_now_total)}"),
            "id": f"node-{acct}",
        }
        lines.append(f"  {q(acct)} {_attrs(**attrs)};")
        ranks[st.layer].append(acct)

    red = series_hex("red", dark)
    max_tainted = max((e.tainted_total for e in trace.edges), default=0.0)
    for e in trace.edges:
        if not e.from_account:
            continue
        dst = e.to_account
        if dst is None:
            dst = exit_node_id(e)
            kind = EXIT_CHANNELS.get(e.channel, f"{channel_label(e.channel)} exit")
            lines.append(
                f"  {q(dst)} "
                + _attrs(label=f"{kind}\n{ist_clock(e.ts)} IST", shape="doubleoctagon", style="filled",
                         color=red, fillcolor=red + alpha, fontsize=10, margin="0.06,0.04",
                         tooltip=f"{kind} · {ist(e.ts)} · UTR {e.utr} · {inr(e.amount)} withdrawn",
                         id=f"node-{dst}")
                + ";"
            )
            ranks[e.depth].append(dst)
        elif dst not in trace.accounts:
            lines.append(f"  {q(dst)} {_attrs(label=dst, shape='box', style='rounded,dashed', color=edge_col)};")
            ranks[e.depth].append(dst)
        edge_attrs: dict[str, object] = {
            "xlabel": inr_short(e.tainted_total),
            "penwidth": penwidth(e.tainted_total, max_tainted),
            "tooltip": (f"{e.txn_id} · {channel_label(e.channel)} · UTR {e.utr} · {ist(e.ts)} · "
                        f"{inr(e.amount)} sent, {inr(e.tainted_total)} victim money"),
            "id": f"edge-{e.txn_id}",
        }
        if e.to_account is None:
            edge_attrs["color"] = red
        if e.via_consortium:
            edge_attrs["style"] = "dashed"
        if selected and selected in (e.from_account, e.to_account):
            edge_attrs["color"] = sel_col
            edge_attrs["fontcolor"] = text
        lines.append(f"  {q(e.from_account)} -> {q(dst)} {_attrs(**edge_attrs)};")

    for layer in sorted(ranks):
        members = "; ".join(q(n) for n in dict.fromkeys(ranks[layer]))
        lines.append(f"  {{ rank=same; {members}; }}")
    lines.append("}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- legend


@dataclass(frozen=True)
class LegendItem:
    label: str
    tone: str
    kind: str = "box"  # box | dashed | cylinder | exit | victim


def legend_items(trace: TraceResult, plan: HoldPlan | None) -> list[LegendItem]:
    items = [LegendItem("Victim", "ink", "victim")]
    seen: set[Disposition] = set()
    for d in plan.decisions if plan else []:
        if d.account_id in trace.accounts and d.disposition not in seen:
            seen.add(d.disposition)
            st = trace.accounts[d.account_id]
            kind = "cylinder" if _is_pool(st) else "box"
            items.append(LegendItem(DISPOSITION_LABELS.get(d.disposition, humanize(d.disposition.value)),
                                    DISPOSITION_TONE.get(d.disposition, "gray"), kind))
    if any(e.to_account is None and e.from_account for e in trace.edges):
        items.append(LegendItem("Cash / crypto exit", "red", "exit"))
    if any(not s.is_home_bank and s.layer > 0 for s in trace.accounts.values()):
        items.append(LegendItem("Other bank", "gray", "dashed"))
    return items


def legend_html(items: list[LegendItem], dark: bool = False) -> str:
    parts = []
    for it in items:
        col = series_hex(it.tone, dark)
        cls = {"dashed": "wp-sw wp-sw-dash", "cylinder": "wp-sw wp-sw-cyl", "exit": "wp-sw wp-sw-oct"}.get(
            it.kind, "wp-sw")
        fill = col if it.kind == "exit" else col + ("40" if dark else "26")
        style = f"border-color:{col};background:{fill}"
        if it.kind == "dashed":
            style = f"border-color:{series_hex('gray', dark)};background:transparent"
        parts.append(f'<span><i class="{cls}" style="{style}"></i>{esc(it.label)}</span>')
    parts.append('<span class="wp-note">Line width = victim money carried · dashed line = learned via CFCFRMS</span>')
    return f'<div class="wp-legend" aria-label="Legend">{"".join(parts)}</div>'
