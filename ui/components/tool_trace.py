"""Agent trace panel: every tool call the ADK agent made on a case, in order, with what checked it.

A vertical timeline per agent run: the officer's message and the input guard's verdict, each model turn (thin
row: model, latency, tokens when reported, the tools it asked for), each tool call (step number, tool chip,
redacted arguments, result summary, latency bar, guard / maker-checker / verifier badges, expandable raw JSON) and
the reply (output screening, reply grounding). A summary row on top: tool calls, time in tools, model turns,
tokens, blocked calls, guard checks, verifier verdicts.

Traces come from the case's hash-chained audit log (``agent_trace`` events written by the ADK edition's
``ToolTracePlugin``); with none recorded, the showcase case shows the recorded offline run (scripted model, no API
key), labelled as such. Builders are pure (HTML strings) so they are testable without Streamlit.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from ui.components import html as h
from ui.components.formatting import ROLE_TITLES, humanize, ist
from ui.components.theme import MONO
from wapsi.agenttrace import (
    LIVE_SOURCES,
    SOURCE_LABELS,
    TRACE_ACTION,
    AgentTrace,
    GuardVerdict,
    TraceStep,
    VerifierVerdict,
    traces_from_audit,
)
from wapsi.agenttrace.demo import live_recorded_traces, recorded_traces
from wapsi.agenttrace.model import FAILED_STATUSES, STOPPED_STATUSES
from wapsi.models import CaseBundle

ORIGIN_AUDIT, ORIGIN_RECORDING, ORIGIN_NONE = "audit", "recording", "none"
ORIGIN_LIVE_RECORDING = "live_recording"  # a recorded live run (Gemma 4 on AMD), replayed
OFFLINE_ENDPOINT = "adk-endpoint-offline"  # the AMD edition's fake OpenAI-compatible endpoint (no network)
ORIGIN_AUDIT_UNSEALED = "audit_unsealed"  # in the audit log, but the store did not seal it into the hash chain
Masker = Callable[[str], str]

STAGE_LABELS = {
    "input": "Input screen",
    "model_request": "Request screen",
    "tool_args": "Argument screen",
    "tool_policy": "Policy",
    "tool_result": "Complaint text",
    "output": "Output screen",
}
SOURCE_NAMES = {
    "model_armor": "Model Armor",
    "fallback": "RulesGuard (Model Armor fallback)",
    "RulesGuard": "RulesGuard",
    "ModelArmorGuard": "Model Armor",
    "maker_checker": "Maker-checker",
    "tool": "Tool",
}
ACTION_TONES = {"passed": "good", "rewritten": "warn", "blocked": "warn", "refused": "warn", "quoted": "warn",
                "logged": "warn", "not_screened": "gray"}
ACTION_WORDS = {"passed": "passed", "rewritten": "rewritten as untrusted data", "blocked": "blocked",
                "refused": "refused", "quoted": "quoted, not followed", "logged": "flagged (logged)",
                "not_screened": "not screened"}
VERIFIER_NAMES = {"verifier": "Verifier", "policy_check": "Policy check", "drafts_verified": "Drafts verified",
                  "re_verification": "Re-verified", "reply_grounding": "Reply grounding"}
KIND_LABELS = {"input": "Officer", "model": "Model", "tool": "Tool", "output": "Reply"}

CSS = """
.wp-tt-run{margin:.3rem 0 1.4rem}
.wp-tt-head{display:flex;flex-wrap:wrap;align-items:center;gap:.35rem .6rem;margin:.1rem 0 .6rem}
.wp-tt-runno{font-size:.74rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
 color:color-mix(in srgb,currentColor 65%,transparent)}
.wp-tt-prompt{font-weight:650;font-size:.98rem;overflow-wrap:anywhere}
.wp-tt-line{position:relative;padding-left:2.1rem}
.wp-tt-line:before{content:"";position:absolute;left:.74rem;top:.4rem;bottom:.6rem;width:2px;
 background:color-mix(in srgb,currentColor 14%,transparent)}
.wp-tt-step{position:relative;margin:0 0 .55rem}
.wp-tt-num{position:absolute;left:-2.1rem;top:.42rem;width:1.55rem;height:1.55rem;border-radius:50%;display:flex;
 align-items:center;justify-content:center;font-size:.72rem;font-weight:800;color:#fff;background:var(--wp-accent)}
.wp-tt-num.stop{background:var(--wp-warn-fg)}
.wp-tt-num.fail{background:var(--wp-bad-fg)}
.wp-tt-dot{position:absolute;left:-1.72rem;top:.52rem;width:.8rem;height:.8rem;border-radius:50%;
 background:var(--wp-s-gray)}
.wp-tt-dot.input{background:var(--wp-s-teal)}
.wp-tt-dot.output{background:var(--wp-s-violet)}
.wp-tt-dot.stop{background:var(--wp-warn-fg)}
.wp-tt-card{border:1px solid color-mix(in srgb,currentColor 13%,transparent);border-radius:.6rem;
 padding:.55rem .75rem;background:color-mix(in srgb,currentColor 2.5%,transparent);min-width:0}
.wp-tt-card.stop{border-color:color-mix(in srgb,var(--wp-warn-fg) 55%,transparent);background:var(--wp-warn-bg)}
.wp-tt-card.fail{border-color:color-mix(in srgb,var(--wp-bad-fg) 55%,transparent);background:var(--wp-bad-bg)}
.wp-tt-top{display:flex;flex-wrap:wrap;align-items:center;gap:.35rem .55rem}
.wp-tt-kind{font-size:.66rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;
 color:color-mix(in srgb,currentColor 62%,transparent)}
.wp-tt-tool{font-family:__MONO__;font-weight:700;font-size:.86rem;padding:.08rem .5rem;border-radius:.35rem;
 background:var(--wp-accent-bg);color:var(--wp-accent-fg)}
.wp-tt-agent{font-size:.76rem;color:color-mix(in srgb,currentColor 62%,transparent)}
.wp-tt-lat{margin-left:auto;display:flex;align-items:center;gap:.45rem;font-size:.78rem;
 font-variant-numeric:tabular-nums;white-space:nowrap}
.wp-tt-bar{width:6.5rem;height:.42rem;border-radius:999px;background:color-mix(in srgb,currentColor 10%,transparent);
 overflow:hidden;display:inline-block}
.wp-tt-bar i{display:block;height:100%;border-radius:999px;background:var(--wp-accent)}
.wp-tt-bar.stop i{background:var(--wp-warn-fg)}
.wp-tt-bar.fail i{background:var(--wp-bad-fg)}
.wp-tt-args{display:flex;flex-wrap:wrap;gap:.2rem .9rem;margin:.4rem 0 .05rem;font-size:.8rem}
.wp-tt-args .k{color:color-mix(in srgb,currentColor 62%,transparent);margin-right:.25rem}
.wp-tt-args .v{font-family:__MONO__;overflow-wrap:anywhere}
.wp-tt-res{font-size:.87rem;margin:.3rem 0 .05rem;overflow-wrap:anywhere}
.wp-tt-res b{font-weight:650}
.wp-tt-badges{display:flex;flex-wrap:wrap;gap:.3rem;margin-top:.4rem}
.wp-tt-badges .wp-chip{white-space:normal}
.wp-tt-raw{margin-top:.4rem;font-size:.78rem}
.wp-tt-raw summary{cursor:pointer;color:color-mix(in srgb,currentColor 68%,transparent);width:max-content}
.wp-tt-pre{white-space:pre-wrap;font-family:__MONO__;font-size:.73rem;line-height:1.45;max-height:20rem;overflow:auto;
 padding:.5rem .65rem;margin:.3rem 0 0;border-radius:.4rem;background:color-mix(in srgb,currentColor 4.5%,transparent)}
.wp-tt-mini{display:flex;flex-wrap:wrap;align-items:center;gap:.25rem .55rem;font-size:.8rem;padding:.3rem 0 .1rem;
 color:color-mix(in srgb,currentColor 75%,transparent)}
.wp-tt-mini .wp-mono{font-size:.78rem}
@media (max-width:640px){.wp-tt-lat{margin-left:0}.wp-tt-bar{width:4rem}}
"""


def css() -> str:
    return "<style>" + " ".join(line.strip() for line in CSS.replace("__MONO__", MONO).strip().splitlines()) + "</style>"


# --------------------------------------------------------------------------- data


def case_traces(bundle: CaseBundle | None) -> tuple[list[AgentTrace], str]:
    """The case's agent runs from its audit log, else the recorded live run on Gemma 4 on AMD (showcase), else the
    recorded offline run (showcase), else nothing."""
    if bundle is None:
        return [], ORIGIN_NONE
    cid = bundle.case.case_id
    traces = traces_from_audit(bundle.audit, case_id=cid)
    if traces:
        return traces, ORIGIN_AUDIT
    live = live_recorded_traces(cid)
    if live:
        return live, ORIGIN_LIVE_RECORDING
    recorded = recorded_traces(cid)
    if recorded:
        return recorded, ORIGIN_RECORDING
    return [], ORIGIN_NONE


def sealed_trace_ids(bundle: CaseBundle | None) -> set[str]:
    """Trace ids whose ``agent_trace`` event carries a hash-chain seal (stores that do not seal yet, e.g.
    Firestore today, leave it unset: the panel must not claim the chain covers those runs)."""
    if bundle is None:
        return set()
    return {e.object_id for e in bundle.audit if e.action == TRACE_ACTION and e.hash}


def trace_origin(trace: AgentTrace, origin: str, sealed: set[str]) -> str:
    if origin == ORIGIN_AUDIT and trace.trace_id not in sealed:
        return ORIGIN_AUDIT_UNSEALED
    return origin


# --------------------------------------------------------------------------- formatting


def fmt_ms(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1:
        return "<1 ms"
    if ms < 1000:
        return f"{ms:.0f} ms"
    return f"{ms / 1000:.2f} s"


def fmt_tokens(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def source_name(source: str) -> str:
    return SOURCE_NAMES.get(source, humanize(source) if source else "Guard")


def status_tone(step: TraceStep) -> str:
    if step.status in FAILED_STATUSES:
        return "bad"
    if step.stopped or step.status == "flagged":
        return "warn"
    if step.status in {"ok", "approved"}:
        return "good"
    return "gray"


def status_text(step: TraceStep) -> str:
    if step.status == "blocked":
        # a guard (Model Armor / RulesGuard) screened the arguments, or the tool's own rule stopped the call
        # (e.g. draft_documents before verification_report has been shown)
        by_guard = any(g.stopped and g.source not in {"tool", "maker_checker"} for g in step.guards)
        return "Blocked by guard" if by_guard else "Blocked by tool rule"
    if step.status == "refused":
        policy = next((g for g in step.guards if g.stage == "tool_policy" and g.stopped), None)
        return "Refused · maker-checker" if policy and policy.source == "maker_checker" else "Refused"
    if step.status == "flagged":
        return "Rewritten by guard" if step.kind == "input" else "Check failed"
    if step.status == "truncated":
        return "Left out"
    return {"ok": "OK", "approved": "Approved", "exception": "Raised an error", "error": "Error",
            "unfinished": "Unfinished", "running": "Running"}.get(step.status, humanize(step.status))


def guard_chip(v: GuardVerdict) -> str:
    stage = STAGE_LABELS.get(v.stage, humanize(v.stage))
    if v.stage == "tool_policy" and v.source == "maker_checker":
        text = f"Maker-checker · {ACTION_WORDS.get(v.action, v.action)}"
    elif v.stage == "tool_policy" and v.source == "tool":
        text = f"Tool rule · {ACTION_WORDS.get(v.action, v.action)}"
    elif v.stage == "tool_result":
        text = f"{stage} · {ACTION_WORDS.get(v.action, v.action)}"
    else:
        text = f"{stage} · {source_name(v.source)} · {ACTION_WORDS.get(v.action, v.action)}"
    tip = v.detail + (f" ({', '.join(v.categories)})" if v.categories else "")
    return h.chip(text, ACTION_TONES.get(v.action, "warn" if v.flagged else "gray"), title=tip or None)


def verifier_chip(v: VerifierVerdict) -> str:
    name = VERIFIER_NAMES.get(v.name, humanize(v.name))
    if v.checked:
        text = (f"{name} · {v.checked - v.failed}/{v.checked} passed" if v.name != "reply_grounding"
                else f"{name} · {v.checked - v.failed}/{v.checked} values traced to tools")
    else:
        text = f"{name} · {'passed' if v.passed else 'failed'}"
    return h.chip(text, "good" if v.passed else "bad", title=v.detail or None)


def _args_html(args: dict, mask: Masker) -> str:
    if not args:
        return ""
    cells = []
    for k, v in [(k, v) for k, v in args.items() if v not in (None, "", [], {})][:6]:
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        elif isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False)
        text = mask(str(v))
        text = text if len(text) <= 120 else text[:119] + "…"
        cells.append(f'<span><span class="k">{h.esc(k)}</span><span class="v">{h.esc(text)}</span></span>')
    return f'<div class="wp-tt-args">{"".join(cells)}</div>'


def _raw(step: TraceStep, mask: Masker = str) -> str:
    raw = mask(json.dumps(step.model_dump(mode="json", exclude_none=True), ensure_ascii=False, indent=2))
    return (f'<details class="wp-tt-raw"><summary>Raw JSON · step {step.seq}</summary>'
            f'<div class="wp-tt-pre" role="region" aria-label="Raw JSON of step {step.seq}" tabindex="0">'
            f"{h.esc(raw)}</div></details>")


def _badges(step: TraceStep) -> str:
    items = [guard_chip(g) for g in step.guards]
    if step.verifier is not None:
        items.append(verifier_chip(step.verifier))
    return f'<div class="wp-tt-badges">{"".join(items)}</div>' if items else ""


def _latency(step: TraceStep, max_ms: float) -> str:
    if step.latency_ms is None:
        return ""
    width = 0.0 if max_ms <= 0 else max(2.0, min(100.0, step.latency_ms / max_ms * 100.0))
    cls = " stop" if step.stopped else (" fail" if step.failed else "")
    return (f'<span class="wp-tt-lat" title="Latency {h.esc(fmt_ms(step.latency_ms))}">'
            f'<span class="wp-tt-bar{cls}" aria-hidden="true"><i style="width:{width:.1f}%"></i></span>'
            f"{h.esc(fmt_ms(step.latency_ms))}</span>")


def tool_step_html(step: TraceStep, number: int, max_ms: float, mask: Masker = str) -> str:
    cls = " stop" if step.stopped else (" fail" if step.failed else "")
    agent = f'<span class="wp-tt-agent">{h.esc(step.agent)}</span>' if step.agent else ""
    return (
        f'<div class="wp-tt-step" data-kind="tool" data-status="{h.esc(step.status)}">'
        f'<div class="wp-tt-num{cls}" aria-label="Tool call {number}">{number}</div>'
        f'<div class="wp-tt-card{cls}"><div class="wp-tt-top"><span class="wp-tt-kind">Tool call</span>'
        f'<span class="wp-tt-tool">{h.esc(step.name)}</span>{agent}'
        f"{h.chip(status_text(step), status_tone(step))}{_latency(step, max_ms)}</div>"
        f"{_args_html(step.args, mask)}"
        f'<div class="wp-tt-res"><b>Result</b> · {h.esc(mask(step.summary or "—"))}</div>'
        f"{_badges(step)}{_raw(step, mask)}</div></div>"
    )


def model_step_html(step: TraceStep, mask: Masker = str) -> str:
    tokens = ""
    if step.tokens_in is not None or step.tokens_out is not None:
        tokens = h.chip(f"{fmt_tokens(step.tokens_in)} in · {fmt_tokens(step.tokens_out)} out", "gray",
                        title="Tokens reported by the model")
    asks = (" → " + ", ".join(step.calls)) if step.calls else " → reply"
    stop = " stop" if step.failed else ""
    guards = "".join(guard_chip(g) for g in step.guards)
    waited = (h.chip(f"waited {fmt_ms(step.waited_ms)} for the rate limit", "gray",
                     title="Paused before the request so the shared endpoint's limit is respected; not model time")
              if step.waited_ms and step.waited_ms >= 50 else "")
    return (
        f'<div class="wp-tt-step" data-kind="model"><span class="wp-tt-dot{stop}" aria-hidden="true"></span>'
        f'<div class="wp-tt-mini"><span class="wp-tt-kind">Model</span><span class="wp-mono">{h.esc(step.name)}</span>'
        f"<span>{h.esc(step.agent)}{h.esc(asks)}</span><span>{h.esc(fmt_ms(step.latency_ms))}</span>{tokens}"
        f"{waited}{guards}{'' if not step.failed else h.chip(status_text(step), 'bad')}</div></div>"
    )


def io_step_html(step: TraceStep, mask: Masker = str) -> str:
    kind = step.kind
    cls = " stop" if step.stopped or step.status == "flagged" else ""
    label = {"input": "Officer asked", "output": "Agent replied"}.get(kind, "Note")
    status = h.chip(status_text(step), status_tone(step)) if step.status != "ok" else ""
    if kind == "note":
        return (f'<div class="wp-tt-step" data-kind="note"><span class="wp-tt-dot" aria-hidden="true"></span>'
                f'<div class="wp-tt-mini"><span class="wp-tt-kind">{label}</span>'
                f"<span>{h.esc(mask(step.summary or '—'))}</span></div></div>")
    return (
        f'<div class="wp-tt-step" data-kind="{h.esc(kind)}"><span class="wp-tt-dot {h.esc(kind)}{cls}" '
        f'aria-hidden="true"></span><div class="wp-tt-mini"><span class="wp-tt-kind">{label}</span>'
        f"<span>“{h.esc(mask(step.summary or '—'))}”</span>{status}</div>{_badges(step)}</div>"
    )


def bar_scale(traces: Sequence[AgentTrace]) -> float:
    """The latency that fills a bar: the slowest tool call shown (at least 50 ms, so sub-millisecond calls such
    as guard-blocked ones never draw a full bar)."""
    return max([50.0] + [s.latency_ms or 0.0 for t in traces for s in t.tool_steps()])


def timeline_html(trace: AgentTrace, *, tools_only: bool = False, mask: Masker = str,
                  max_ms: float | None = None) -> str:
    max_ms = bar_scale([trace]) if max_ms is None else max_ms
    out, n = [], 0
    for step in trace.steps:
        if step.kind == "tool":
            n += 1
            out.append(tool_step_html(step, n, max_ms, mask))
        elif tools_only:
            continue
        elif step.kind == "model":
            out.append(model_step_html(step, mask))
        else:
            out.append(io_step_html(step, mask))
    if not out:
        out.append('<div class="wp-tt-mini">No steps recorded.</div>')
    return f'<div class="wp-tt-line">{"".join(out)}</div>'


def run_header_html(trace: AgentTrace, number: int, *, origin: str = ORIGIN_AUDIT, mask: Masker = str) -> str:
    s = trace.summary()
    role = ROLE_TITLES.get(trace.role, humanize(trace.role)) if trace.role else ""
    who = " · ".join(x for x in (mask(trace.user), role) if x)
    source = SOURCE_LABELS.get(trace.source, humanize(trace.source))
    chips = [h.chip(source, "accent" if trace.source in LIVE_SOURCES else "gray",
                    title=f"{trace.model or 'model not recorded'} · app {trace.app or '—'}")]
    if trace.provider:  # an OpenAI-compatible endpoint: say which model answered and where it is served
        model = trace.model.removeprefix("openai/")
        chips.append(h.chip(" · ".join(x for x in (model, trace.provider) if x), "gray",
                            title="Model id on the endpoint · serving platform"))
    if origin == ORIGIN_AUDIT:
        chips.append(h.chip("In the case's hash-chained audit log", "good",
                            title="Recorded as an agent_trace audit event; the audit page re-verifies the chain"))
    elif origin == ORIGIN_LIVE_RECORDING:
        chips.append(h.chip(f"Recorded {ist(trace.started_at).split(' ')[0]}, replayed", "gray",
                            title="A live run on Gemma 4 served on AMD, recorded and replayed here; not a run in "
                                  "this deployment"))
    elif origin == ORIGIN_AUDIT_UNSEALED:
        chips.append(h.chip("In the case's audit log", "gray",
                            title="Recorded as an agent_trace audit event; this case store does not seal events "
                                  "into the hash chain yet"))
    if s.blocked:
        chips.append(h.chip(f"{s.blocked} blocked", "warn"))
    if s.errors:
        chips.append(h.chip(f"{s.errors} error{'s' if s.errors != 1 else ''}", "bad"))
    prompt = mask(trace.prompt) if trace.prompt else "(no officer message recorded)"
    return (f'<div class="wp-tt-head"><span class="wp-tt-runno">Run {number}</span>'
            f'<span class="wp-tt-prompt">“{h.esc(prompt)}”</span></div>'
            f'<div class="wp-tt-head"><span class="wp-note">{h.esc(who)}{" · " if who else ""}'
            f"{h.esc(ist(trace.started_at))} · {s.tool_calls} tool call{'s' if s.tool_calls != 1 else ''} · "
            f"{h.esc(fmt_ms(s.wall_ms))} end to end</span>{h.chips(chips)}</div>")


def run_html(trace: AgentTrace, number: int, *, origin: str = ORIGIN_AUDIT, tools_only: bool = False,
             mask: Masker = str, max_ms: float | None = None) -> str:
    return (f'<section class="wp-tt-run" aria-label="Agent run {number}">'
            f"{run_header_html(trace, number, origin=origin, mask=mask)}"
            f"{timeline_html(trace, tools_only=tools_only, mask=mask, max_ms=max_ms)}</section>")


def summary_html(traces: Sequence[AgentTrace]) -> str:
    sums = [t.summary() for t in traces]
    calls = sum(s.tool_calls for s in sums)
    tool_ms = sum(s.tool_latency_ms for s in sums)
    turns = sum(s.model_turns for s in sums)
    tin = [s.tokens_in for s in sums if s.tokens_in is not None]
    tout = [s.tokens_out for s in sums if s.tokens_out is not None]
    blocked = sum(s.blocked for s in sums)
    checks = sum(s.guard_checks for s in sums)
    flags = sum(s.guard_flags for s in sums)
    vpass = sum(s.verifier_passed for s in sums)
    vfail = sum(s.verifier_failed for s in sums)
    tokens = f"{fmt_tokens(sum(tin))} / {fmt_tokens(sum(tout))}" if tin or tout else "—"
    return h.tiles([
        h.Tile("Tool calls", str(calls), f"in {len(traces)} agent run{'s' if len(traces) != 1 else ''}", accent=True),
        h.Tile("Time in tools", fmt_ms(tool_ms), f"model turns: {turns}", title=f"{tool_ms:.1f} ms"),
        h.Tile("Tokens in / out", tokens,
               ("character-count stand-ins (offline fake endpoint)" if all(t.source == OFFLINE_ENDPOINT for t in traces)
                else "as reported by the model") if tin or tout else "not reported (scripted model)", swatch="gray"),
        h.Tile("Blocked calls", str(blocked), "guard, maker-checker or tool rule",
               swatch="orange" if blocked else None),
        h.Tile("Guard checks", str(checks), f"{flags} flagged", swatch="teal"),
        h.Tile("Verifier verdicts", f"{vpass} ✓ · {vfail} ✕", "plan, drafts, approvals, reply",
               swatch="red" if vfail else "teal"),
    ], min_width_rem=8.5)


def empty_html() -> str:
    title, body, glyph, code = _empty_args()
    return h.empty_state(title, body, glyph=glyph, code=code)


# --------------------------------------------------------------------------- Streamlit


def render_agent_trace(ctx: object) -> None:
    """The Agent trace panel for ``ctx.bundle`` (``ui.views.common.Ctx``)."""
    import streamlit as st

    from ui.views.common import md

    bundle: CaseBundle | None = getattr(ctx, "bundle", None)
    viewer = getattr(ctx, "viewer", None)
    if getattr(viewer, "masked", False) and callable(getattr(ctx, "pii", None)):
        # look the case's names and mobiles up once, not once per trace fragment (ledger lookups)
        names, mobiles = ctx.pii(bundle)  # type: ignore[attr-defined]

        def mask(text: str) -> str:
            return viewer.text(text, names, mobiles) if text else text  # type: ignore[union-attr]
    else:
        mask_fn = getattr(ctx, "mask", None)

        def mask(text: str) -> str:
            return mask_fn(text, bundle) if callable(mask_fn) and text else text

    traces, origin = case_traces(bundle)
    md(css())
    if not traces:
        md(empty_html())
        return
    lead = ("Every tool call the agent made on this case, in order, with the safety checks around it. Account "
            "numbers, phones and names are redacted in the trace.")
    if origin == ORIGIN_LIVE_RECORDING:
        when = ist(traces[0].started_at).split(" ")[0] if traces else ""
        md(h.callout(f"No agent run is recorded on this case in this deployment yet, so this replays a live run "
                     f"recorded on {when}: the real ADK app, tools and guards, with Gemma 4 served on AMD GPUs "
                     "(AMD Token Factory) choosing every tool call and writing the draft prose. Model latencies are "
                     "the requests to that endpoint from a laptop in India; waits for its rate limit are shown "
                     "apart. With WAPSI_LLM_BASE_URL set, live runs appear here.",
                     strong="Recorded live run, replayed."))
    elif origin == ORIGIN_RECORDING:
        md(h.callout("No agent run is recorded on this case yet, so this is the recorded offline run: the real ADK "
                     "app, tools and guards, driven by a scripted model instead of Gemma 4 on AMD (no API key). "
                     "Latencies were measured on a laptop CPU. With WAPSI_LLM_BASE_URL set (Gemma 4 on AMD, or any "
                     "OpenAI-compatible endpoint), live runs appear here.",
                     strong="Recorded offline run."))
    else:
        md(f'<div class="wp-note" style="margin:-.2rem 0 .6rem">{h.esc(lead)}</div>')
    md(summary_html(traces))
    c1, c2 = st.columns([1, 1])
    with c1:
        tools_only = st.toggle("Tool calls only", value=False, key="agent_trace_tools_only",
                               help="Hide the officer message, model turns and the reply.")
    with c2:
        newest_first = st.toggle("Newest run first", value=False, key="agent_trace_newest")
    numbered = list(enumerate(traces, start=1))
    if newest_first:
        numbered.reverse()
    scale = bar_scale(traces)  # one latency scale for every run on the page
    sealed = sealed_trace_ids(bundle)
    md("".join(run_html(t, n, origin=trace_origin(t, origin, sealed), tools_only=tools_only, mask=mask,
                        max_ms=scale) for n, t in numbered))


def _empty_args() -> tuple[str, str, str, str]:
    return ("No agent runs on this case yet",
            "When an officer asks the Wapsi agent (Google ADK on Gemini, or on Gemma 4 served on AMD) about this "
            "case, every tool call it makes "
            "appears here in order — arguments, result, latency and the guard and verifier verdicts around it — "
            "and is recorded in the case's audit log. On the showcase data, case CASE-31009260000101 shows a "
            "recorded offline run.",
            "⋯", "")


def tab_label(bundle: CaseBundle | None) -> str:
    traces, _ = case_traces(bundle)
    n = sum(len(t.tool_steps()) for t in traces)
    return f"Agent trace · {n}" if n else "Agent trace"


__all__ = [
    "ORIGIN_AUDIT",
    "ORIGIN_AUDIT_UNSEALED",
    "ORIGIN_LIVE_RECORDING",
    "ORIGIN_NONE",
    "ORIGIN_RECORDING",
    "STOPPED_STATUSES",
    "case_traces",
    "css",
    "empty_html",
    "fmt_ms",
    "guard_chip",
    "model_step_html",
    "render_agent_trace",
    "run_html",
    "sealed_trace_ids",
    "summary_html",
    "tab_label",
    "timeline_html",
    "tool_step_html",
    "trace_origin",
    "verifier_chip",
]
