"""Ask the agent: a chat box in the Case Room for editions whose data source can run the ADK officer agent.

The AMD edition's data source has ``ask_agent`` / ``agent_status`` (Google ADK on Gemma 4 served on AMD, or the
offline fake endpoint in demo mode); the box does not render for a data source without them. Each message goes
through the agent's guard and tools; the reply shown is the model's only when every amount and account number in it
was returned by a tool, otherwise it is the summary built from the tool results (the page says which). Every run
lands in the agent trace below and in the case's hash-chained audit log.

Builders are pure (HTML strings) so they are testable without Streamlit. The reply is rendered as Markdown without
raw HTML, and through the viewer's mask.
"""

from __future__ import annotations

from typing import Any

from ui.components import html as h
from wapsi.models import CaseBundle

K_HISTORY = "_agent_chat"  # {case_id: [message, ...]}
K_PENDING = "_agent_chat_pending"
K_INPUT = "agent_chat_text"
MAX_SHOWN = 8
MAX_QUESTION = 600

SOURCE_NOTES = {
    "model": ("Reply grounded", "good", "Every rupee amount and account number in this reply was returned by a tool."),
    "tool_results": ("Model reply withheld", "warn",
                     "The model's reply quoted a value no tool returned, so this summary was built from the tool "
                     "results instead."),
    "notice": ("Notice", "warn", "The agent could not complete this request."),
}


def wait_note(current: tuple[float, str] | None) -> str | None:
    """'Waiting for the endpoint's rate limit (42 s)…' while a pause is in progress (``RATE_WAITS.current()``)."""
    if not current or current[0] < 1:
        return None
    seconds, why = current
    return f"Waiting for {why} ({seconds:.0f} s)… Nothing is lost: the agent continues on its own."


def _ask_with_wait_notes(ask: Any, poll_s: float = 0.5) -> Any:
    """Run ``ask()`` in a worker thread and, while it runs, show any rate-limit wait the endpoint clients announce,
    so a 60-second ``Retry-After`` does not look like a frozen page."""
    import concurrent.futures

    import streamlit as st

    try:
        from wapsi.adapters.llm_resilience import RATE_WAITS
    except ImportError:  # pragma: no cover - the package always has it
        return ask()
    slot = st.empty()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ask)
        shown = None
        while True:
            try:
                return future.result(timeout=poll_s)
            except concurrent.futures.TimeoutError:
                note = wait_note(RATE_WAITS.current())
                if note != shown:
                    if note:
                        slot.caption(note)
                    else:
                        slot.empty()
                    shown = note
            finally:
                if future.done():
                    slot.empty()


def agent_capable(ds: object) -> bool:
    return callable(getattr(ds, "ask_agent", None)) and callable(getattr(ds, "agent_status", None))


def tool_mode_label(mode: str | None) -> str:
    mode = str(mode or "")
    if mode.startswith("native"):
        return "native tool calls"
    if mode.startswith("json"):
        return "JSON tool protocol"
    return "tool mode: probed on first use"


def status_html(status: dict[str, Any]) -> str:
    """One line of chips: framework, model, where it is served, tool mode (or why the agent is unavailable)."""
    if not status.get("available"):
        return h.chips([h.chip("Agent unavailable", "warn", title=str(status.get("reason") or ""))])
    offline = status.get("backend") == "offline"
    chips = [h.chip("Google ADK agent", "accent", title="wapsi_officer: the same tools, guard and trace plugins "
                                                       "as the Google Cloud edition")]
    if offline:
        chips.append(h.chip("Offline: scripted planner behind a fake OpenAI-compatible endpoint", "gray",
                            title="No network and no key. Set WAPSI_LLM_BASE_URL (and WAPSI_DEMO_MODE=0 or "
                                  "WAPSI_AGENT_BACKEND=endpoint) to run Gemma 4 on AMD."))
    else:
        chips.append(h.chip(str(status.get("model") or "model"), "gray", title="Model id on the endpoint"))
        chips.append(h.chip(str(status.get("provider") or "OpenAI-compatible endpoint"), "gray",
                            title=str(status.get("base_url") or "")))
        if not status.get("api_key_set"):
            chips.append(h.chip("No API key set", "gray", title="WAPSI_LLM_API_KEY is empty (fine for a local vLLM)"))
        fallback = status.get("fallback")
        if isinstance(fallback, dict) and fallback.get("using"):  # the configured model is not usable right now
            chips.append(h.chip(f"Fallback model in use (configured: {fallback.get('configured_model')})", "warn",
                                title=str(fallback.get("reason") or "")))
    chips.append(h.chip(tool_mode_label(status.get("tool_mode")), "gray",
                        title="Chosen by a capability probe: OpenAI tool calls when the endpoint returns them, "
                              "else a strict one-call-per-turn JSON protocol"))
    return h.chips(chips)


def suggestions(bundle: CaseBundle | None) -> list[str]:
    """Starter questions for the case in view (each maps to tools; none asks the model for a figure)."""
    if bundle is None:
        return []
    out = [f"Open case {' '.join(bundle.case.ncrp_ids)}"]
    first = sorted(a for a, s in (bundle.trace.accounts.items() if bundle.trace else []) if s.layer == 1)
    if first:
        out.append(f"Why is {first[0]} held in {bundle.case.case_id}? Explain the account.")
    out += ["Show the deadlines for this case.", "Search the SOP for the forward hold rule."]
    return out


def reply_meta_html(meta: dict[str, Any]) -> str:
    """Chips under an agent reply: how it was checked, which tools ran, how long it took."""
    label, tone, tip = SOURCE_NOTES.get(str(meta.get("reply_source")), SOURCE_NOTES["notice"])
    if meta.get("reply_source") == "model" and meta.get("grounded") is None:
        label, tone, tip = "Reply (no figures to check)", "gray", "The reply quotes no amounts or account numbers."
    chips = [h.chip(label, tone, title=meta.get("grounding_detail") or tip)]
    tools = [str(t) for t in (meta.get("tools") or [])]
    if tools:
        chips.append(h.chip(f"{len(tools)} tool call{'s' if len(tools) != 1 else ''}: " + " → ".join(tools[:6])
                            + (" …" if len(tools) > 6 else ""), "gray"))
    if meta.get("blocked"):
        chips.append(h.chip(f"{meta['blocked']} blocked", "warn", title="Stopped by the guard, maker-checker or a "
                                                                        "tool rule; see the trace below"))
    if meta.get("wall_ms") is not None:
        ms = float(meta["wall_ms"])
        chips.append(h.chip(f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms:.0f} ms", "gray", title="End to end"))
    return h.chips(chips)


def _meta(result: Any) -> dict[str, Any]:
    return {"reply_source": getattr(result, "reply_source", "notice"), "grounded": getattr(result, "grounded", None),
            "grounding_detail": getattr(result, "grounding_detail", ""), "tools": list(getattr(result, "tools", [])),
            "blocked": getattr(result, "blocked", 0), "wall_ms": getattr(result, "wall_ms", None)}


def _queue(text: str) -> None:
    import streamlit as st

    st.session_state[K_PENDING] = text


def render_agent_chat(ctx: object) -> None:
    """The chat box for ``ctx.bundle`` (``ui.views.common.Ctx``); nothing is drawn without an agent."""
    import streamlit as st

    from ui.views.common import md

    ds = getattr(ctx, "ds", None)
    bundle: CaseBundle | None = getattr(ctx, "bundle", None)
    if bundle is None or not agent_capable(ds):
        return
    viewer = getattr(ctx, "viewer", None)
    case_id = bundle.case.case_id
    status = ds.agent_status()  # type: ignore[union-attr]
    md(h.section("Ask the agent"))
    md(status_html(status))
    if not status.get("available"):
        md(h.callout(str(status.get("reason") or "The agent is not available in this deployment."), "warn",
                     strong="Agent unavailable."))
        return
    if not getattr(viewer, "can_run", True):
        md(h.callout("Auditors have read-only access: the recorded agent runs are shown below.",
                     strong="Read-only."))
        return

    history: list[dict[str, Any]] = st.session_state.setdefault(K_HISTORY, {}).setdefault(case_id, [])
    mask = getattr(ctx, "mask", None)
    for msg in history[-MAX_SHOWN:]:
        with st.chat_message("user" if msg["who"] == "officer" else "assistant"):
            text = mask(msg["text"], bundle) if callable(mask) else msg["text"]
            st.markdown(text)  # Markdown only: no raw HTML from the model
            if msg["who"] == "agent":
                md(reply_meta_html(msg["meta"]))

    if not history:
        cols = st.columns(2)
        for i, text in enumerate(suggestions(bundle)):
            cols[i % 2].button(text, key=f"agent_suggest_{i}", on_click=_queue, args=(text,),
                               help="Send this to the agent")
    with st.form("agent_chat_form", clear_on_submit=True, border=False):
        typed = st.text_input("Ask the agent about this case", key=K_INPUT, max_chars=MAX_QUESTION,
                              placeholder="e.g. Why is this account held? What is due this week?",
                              label_visibility="collapsed")
        sent = st.form_submit_button("Ask the agent", type="primary")
    question = (st.session_state.pop(K_PENDING, None) or (typed if sent else "") or "").strip()
    if not question:
        return
    history.append({"who": "officer", "text": question})
    with st.spinner("The agent is working: model turns, tool calls and checks…"):
        out = _ask_with_wait_notes(lambda: ds.ask_agent(case_id, question, actor=viewer.actor,  # type: ignore
                                                        role=viewer.role))
    result = getattr(out, "data", None)
    history.append({"who": "agent", "text": out.message,
                    "meta": _meta(result) if result is not None else {"reply_source": "notice"}})
    del history[:-40]
    st.rerun()


__all__ = ["agent_capable", "render_agent_chat", "reply_meta_html", "status_html", "suggestions", "tool_mode_label",
           "wait_note"]
