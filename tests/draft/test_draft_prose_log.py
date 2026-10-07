"""Adapter backlog: OpenAICompatLLM extra fields and token usage, public prose feedback, draft_case(prose_log=...),
and FakeLLM paragraphs long enough for the task word ranges."""

from __future__ import annotations

import json
import re

import httpx
import pytest

from wapsi.adapters.llm_fake import FakeLLM
from wapsi.adapters.llm_openai_compat import OpenAICompatLLM, TokenUsage
from wapsi.draft import prose
from wapsi.draft.drafter import draft_case
from wapsi.draft.render import PLACEHOLDER_RE

MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
# the task instructions' ranges (and editions/amd/lora/common.py TASK_WORDS); placeholders count as one word
TASK_WORDS = {"str_grounds": (60, 140), "edd_rationale": (50, 120), "case_summary": (50, 120)}


def _words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*", PLACEHOLDER_RE.sub(" X ", text)))


def _client(responses, **kw):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=responses[min(len(seen), len(responses)) - 1])

    llm = OpenAICompatLLM("http://llm.local/v1", "m", transport=httpx.MockTransport(handler), **kw)
    return llm, seen


def _reply(text="ok", usage=None):
    body = {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}
    if usage is not None:
        body["usage"] = usage
    return body


# --------------------------------------------------------------------------- OpenAICompatLLM


def test_extra_request_fields_are_merged_under_the_client_fields():
    llm, seen = _client(
        [_reply()],
        extra_body={"chat_template_kwargs": {"enable_thinking": False}, "seed": 7, "temperature": 1.0},
    )
    assert llm.complete(MSGS, temperature=0.0, max_tokens=20, extra_body={"seed": 8, "top_p": 0.9}) == "ok"
    body = seen[0]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert (body["seed"], body["top_p"]) == (8, 0.9)  # per-call extras win over the constructor's
    assert (body["model"], body["messages"], body["temperature"], body["max_tokens"]) == ("m", MSGS, 0.0, 20)
    llm.complete(MSGS)
    assert seen[1]["seed"] == 7 and "top_p" not in seen[1]  # per-call extras do not stick


@pytest.mark.parametrize("field", ["model", "messages", "stream"])
def test_reserved_fields_cannot_be_extras(field):
    with pytest.raises(ValueError, match=field):
        OpenAICompatLLM("http://x", "m", extra_body={field: 1})
    llm, _ = _client([_reply()])
    with pytest.raises(ValueError, match=field):
        llm.complete(MSGS, extra_body={field: 1})


def test_token_usage_per_call_and_running_total():
    llm, _ = _client(
        [
            _reply("a", {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}),
            _reply("b", {"prompt_tokens": 50, "completion_tokens": 5}),  # no total: prompt + completion
            _reply("c"),  # server sent no usage
        ]
    )
    assert llm.usage == TokenUsage() and llm.last_usage is None
    text, usage = llm.complete_with_usage(MSGS)
    assert text == "a" and usage == TokenUsage(100, 20, 120, 1) and llm.last_usage == usage
    assert llm.complete(MSGS) == "b" and llm.last_usage == TokenUsage(50, 5, 55, 1)
    assert llm.complete_with_usage(MSGS) == ("c", None) and llm.last_usage is None
    assert llm.usage == TokenUsage(150, 25, 175, 2)
    assert llm.usage.as_dict() == {
        "prompt_tokens": 150,
        "completion_tokens": 25,
        "total_tokens": 175,
        "calls": 2,
    }
    assert llm.reset_usage() == TokenUsage(150, 25, 175, 2) and llm.usage == TokenUsage()


def test_malformed_usage_counts_as_zero():
    assert TokenUsage.from_response(
        {"usage": {"prompt_tokens": "many", "completion_tokens": True}}
    ) == TokenUsage(0, 0, 0, 1)
    assert TokenUsage.from_response({"usage": None}) is None


# --------------------------------------------------------------------------- prose feedback and prose_log


def test_feedback_is_public_and_the_old_name_still_works():
    problems = prose.validate_prose("Lost 5 lakh rupees.", {}, [])
    msg = prose.feedback(problems)
    assert prose._feedback is prose.feedback  # noqa: SLF001 - the kept alias
    assert msg.startswith("Your answer broke these rules:") and '{"text": "..."}' in msg


def test_draft_case_can_return_prose_requests_and_responses(bundle2, ledger):
    llm = FakeLLM()
    plain = draft_case(bundle2, ledger, FakeLLM())
    log: list[prose.ProseExchange] = []
    drafts = draft_case(bundle2, ledger, llm, prose_log=log)
    assert [(d.draft_id, d.body, d.structured) for d in drafts] == [
        (d.draft_id, d.body, d.structured) for d in plain
    ]
    with_prose = {d.draft_id: d for d in drafts if "llm_attempts" in d.structured}
    assert with_prose and {ex.draft_id for ex in log} == set(with_prose)
    assert sum(len(ex.calls) for ex in log) == len(llm.calls)
    for ex in log:
        d = with_prose[ex.draft_id]
        assert ex.kind == d.kind.value and ex.account_id == d.account_id
        assert (
            ex.request == ex.calls[0].messages[:2] and json.loads(ex.request[1]["content"])["task"] == ex.task
        )
        assert ex.offered and set(PLACEHOLDER_RE.findall(ex.outcome.skeleton)) <= set(ex.offered)
        assert not ex.outcome.fallback and ex.accepted is ex.calls[-1] and ex.accepted.issues == []
        assert " ".join(prose.parse_response(ex.accepted.response).split()) == ex.outcome.skeleton
        assert ex.outcome.skeleton in d.structured["skeleton"]["body"]


def test_prose_log_records_failed_calls_and_retries(bundle2, ledger):
    class Flaky:
        name = "flaky"

        def __init__(self):
            self.n = 0

        def complete(self, messages, **kw):
            self.n += 1
            if self.n % 2:
                raise RuntimeError("down")
            return '{"text": "Lost 5 lakh rupees."}'

    log: list[prose.ProseExchange] = []
    draft_case(bundle2, ledger, Flaky(), prose_log=log)
    assert log and all(ex.outcome.fallback and ex.accepted is None for ex in log)
    first, second = log[0].calls
    assert (first.response, first.error) == (None, "RuntimeError")
    assert second.response and second.error is None and "raw_digits" in second.issues


# --------------------------------------------------------------------------- FakeLLM lengths


@pytest.mark.parametrize("which", ["bundle1", "bundle2"])
def test_fake_llm_paragraphs_fit_the_task_word_ranges(request, ledger, which):
    log: list[prose.ProseExchange] = []
    draft_case(request.getfixturevalue(which), ledger, FakeLLM(), prose_log=log)
    tasks = {ex.task for ex in log}
    assert "case_summary" in tasks
    for ex in log:
        lo, hi = TASK_WORDS[ex.task]
        assert not ex.outcome.fallback, ex.outcome.issues
        assert lo <= _words(ex.outcome.skeleton) <= hi, (
            ex.task,
            _words(ex.outcome.skeleton),
            ex.outcome.skeleton,
        )
