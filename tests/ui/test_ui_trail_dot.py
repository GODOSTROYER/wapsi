from __future__ import annotations

import re

import pytest

from ui.components.theme import accent
from ui.components.trail_dot import (
    MAX_PEN,
    MIN_PEN,
    action_text,
    build_trail_dot,
    exit_node_id,
    legend_html,
    legend_items,
    penwidth,
    q,
)
from wapsi.fixtures import showcase as sc


def _node_line(dot: str, node_id: str) -> str:
    matches = [ln for ln in dot.splitlines() if ln.strip().startswith(f'"{node_id}" [')]
    assert len(matches) == 1, (node_id, matches)
    return matches[0]


def _edge_line(dot: str, txn_id: str) -> str:
    matches = [ln for ln in dot.splitlines() if f'id="edge-{txn_id}"' in ln]
    assert len(matches) == 1, (txn_id, matches)
    return matches[0]


def _attr(line: str, name: str) -> str | None:
    m = re.search(rf'\b{name}=("(?:[^"\\]|\\.)*"|[\d.]+)', line)
    if not m:
        return None
    return m.group(1).strip('"')


@pytest.fixture
def dot1(bundles):
    b = bundles[0]
    return build_trail_dot(b.trace, b.plan, classifications=b.classifications)


def test_dot_is_a_balanced_lr_digraph(dot1):
    assert dot1.startswith("digraph wapsi_trail {")
    assert dot1.rstrip().endswith("}")
    assert dot1.count("{") == dot1.count("}")
    assert 'rankdir="LR"' in dot1


def test_contains_every_showcase_account(dot1):
    for acct in sc.SHOWCASE_1["layers"]:
        _node_line(dot1, acct)


def test_victim_node_and_layer_ranks(dot1):
    victim = _node_line(dot1, sc.V1)
    assert "Victim" in _attr(victim, "label")
    assert f'{{ rank=same; "{sc.V1}"; }}' in dot1
    layer1 = next(ln for ln in dot1.splitlines() if "rank=same" in ln and f'"{sc.A}"' in ln)
    assert f'"{sc.B}"' not in layer1  # B is layer 2
    layer2 = next(ln for ln in dot1.splitlines() if "rank=same" in ln and f'"{sc.B}"' in ln)
    for acct in (sc.L, sc.C):
        assert f'"{acct}"' in layer2


@pytest.mark.parametrize(
    ("txn", "label"),
    [("TX-001", "₹4.80 L"), ("TX-002", "₹2.88 L"), ("TX-003", "₹4,800"), ("TX-004", "₹48,000"),
     ("TX-007", "₹1,152"), ("TX-008", "₹19,200"), ("TX-009", "₹2.40 L"), ("TX-010", "₹1.92 L"),
     ("TX-011", "₹48,000")],
)
def test_edge_labels_are_short_tainted_amounts(dot1, txn, label):
    assert _attr(_edge_line(dot1, txn), "xlabel") == label


def test_edge_penwidth_scales_with_tainted(dot1):
    widths = {t: float(_attr(_edge_line(dot1, t), "penwidth")) for t in ("TX-001", "TX-002", "TX-007")}
    assert widths["TX-001"] == MAX_PEN
    assert widths["TX-001"] > widths["TX-002"] > widths["TX-007"] >= MIN_PEN
    assert penwidth(0, 0) == MIN_PEN
    assert penwidth(240000, 480000) == pytest.approx(3.5)


def test_pool_is_a_cylinder_and_merchant_a_tab(dot1):
    assert _attr(_node_line(dot1, sc.P), "shape") == "cylinder"
    assert _attr(_node_line(dot1, sc.K), "shape") == "tab"
    assert _attr(_node_line(dot1, sc.A), "shape") == "box"


def test_other_bank_nodes_are_dashed(dot1):
    for acct in (sc.C, sc.E):
        assert "dashed" in _attr(_node_line(dot1, acct), "style")
    for acct in (sc.A, sc.B, sc.D, sc.L, sc.K, sc.P, sc.V1):
        assert "dashed" not in _attr(_node_line(dot1, acct), "style")
    # E's own ATM withdrawal was learned through a CFCFRMS response
    assert _attr(_edge_line(dot1, "TX-011"), "style") == "dashed"


def test_cash_outs_are_distinct_terminal_nodes(dot1, bundles):
    exits = [ln for ln in dot1.splitlines() if ln.strip().startswith('"exit::')]
    assert len(exits) == 3
    for ln in exits:
        assert _attr(ln, "shape") == "doubleoctagon"
        assert "ATM cash-out" in _attr(ln, "label")
    edge = next(e for e in bundles[0].trace.edges if e.txn_id == "TX-004")
    assert f'"{sc.A}" -> "{exit_node_id(edge)}"' in dot1


def test_node_colour_follows_disposition(dot1):
    hold = _attr(_node_line(dot1, sc.A), "color")
    outbound = _attr(_node_line(dot1, sc.C), "color")
    innocent = _attr(_node_line(dot1, sc.L), "color")
    merchant = _attr(_node_line(dot1, sc.K), "color")
    assert len({hold, outbound, innocent, merchant}) == 4
    assert _attr(_node_line(dot1, sc.B), "color") == hold  # same disposition, same colour
    # the pool account's amount hold shares the hold colour; its cylinder shape and label set it apart
    assert _attr(_node_line(dot1, sc.P), "color") == hold


def test_node_labels_state_the_recommendation(dot1):
    assert "Hold ₹43,200" in _attr(_node_line(dot1, sc.A), "label")
    assert "Fwd hold ₹96,000" in _attr(_node_line(dot1, sc.A), "label")
    assert "Request ₹1.44 L" in _attr(_node_line(dot1, sc.E), "label")
    assert "Hold ₹19,200" in _attr(_node_line(dot1, sc.P), "label")
    assert "no suspension" in _attr(_node_line(dot1, sc.P), "label")
    assert "No hold" in _attr(_node_line(dot1, sc.K), "label")
    assert "no escalation" in _attr(_node_line(dot1, sc.L), "label")


def test_selected_account_is_highlighted(bundles):
    b = bundles[0]
    dot = build_trail_dot(b.trace, b.plan, selected=sc.B)
    line = _node_line(dot, sc.B)
    assert float(_attr(line, "penwidth")) == 3.2
    assert _attr(line, "color") == accent(False)
    assert float(_attr(_node_line(dot, sc.A), "penwidth")) == 1.4
    assert _attr(_edge_line(dot, "TX-002"), "color") == accent(False)


def test_dark_mode_uses_dark_surface(bundles):
    b = bundles[0]
    assert 'bgcolor="#0e1117"' in build_trail_dot(b.trace, b.plan, dark=True)
    assert 'bgcolor="#ffffff"' in build_trail_dot(b.trace, b.plan, dark=False)


def test_names_are_used_as_given_so_masking_holds(bundles):
    b = bundles[0]
    dot = build_trail_dot(b.trace, b.plan, names={sc.V1: "M•••• J••••", sc.A: "R••• K•••••••"})
    assert "M•••• J••••" in dot and "R••• K•••••••" in dot
    assert "Meera" not in dot and "Ravi" not in dot


def test_showcase_two_flags_human_decision(bundles):
    b = bundles[1]
    dot = build_trail_dot(b.trace, b.plan, classifications=b.classifications)
    for acct in (sc.V2, sc.V3, sc.M):
        _node_line(dot, acct)
    assert "Human decision" in _attr(_node_line(dot, sc.M), "label")
    assert _attr(_edge_line(dot, "TX-101"), "xlabel") == "₹1.00 L"
    assert _attr(_edge_line(dot, "TX-102"), "xlabel") == "₹60,000"


def test_trace_without_plan_still_renders(bundles):
    b = bundles[0]
    dot = build_trail_dot(b.trace, None)
    assert "No recommendation" in dot or "Victim money" in dot
    assert action_text(None, b.trace.accounts[sc.A]) == "Victim money ₹43,200"


def test_legend_lists_what_is_on_screen(bundles):
    b = bundles[0]
    labels = [i.label for i in legend_items(b.trace, b.plan)]
    for expected in ("Victim", "Hold victim amount", "Hold, no escalation", "No hold, update only",
                     "Hold amount · pool/nodal account — no suspension", "Request other bank",
                     "Cash / crypto exit", "Other bank"):
        assert expected in labels
    html = legend_html(legend_items(b.trace, b.plan))
    assert "wp-sw-cyl" in html and "wp-sw-dash" in html and "wp-sw-oct" in html
    labels2 = [i.label for i in legend_items(bundles[1].trace, bundles[1].plan)]
    assert "Other bank" not in labels2


def test_quote_escapes_dot_strings():
    assert q('a"b') == '"a\\"b"'
    assert q("x\\y") == '"x\\\\y"'
    assert q("line1\nline2") == '"line1\\nline2"'
