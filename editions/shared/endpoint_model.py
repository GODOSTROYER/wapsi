"""``EndpointLlm``: Google ADK's ``LiteLlm`` pointed at an OpenAI-compatible endpoint (Gemma 4 on AMD).

It *is* a ``LiteLlm`` (``openai/<model>`` with ``api_base`` / ``api_key``), so ADK treats it like any LiteLLM-backed
model (tool calls are paired with their results by id). On top of that it:

- finds out once, on first use, whether the endpoint returns native tool calls (``probe``; the result is cached
  in memory and, with a ``ProbeCache``, on disk). ``WAPSI_AGENT_TOOL_MODE=native|json`` skips the probe;
- in ``native`` mode sends the agent's tools as OpenAI ``tools`` (one call per turn unless
  ``WAPSI_AGENT_PARALLEL_TOOLS=1``) and tool results with the message role the probe found to work (ADK's
  ``LiteLlm`` sends ``tool_responses`` for model ids matching ``gemma-4``; OpenAI gateways expect ``tool``);
- in ``json`` mode (endpoints without tool-call support) rewrites the request for the strict JSON tool protocol
  (``json_protocol``), validates the model's answer against the tool schemas, asks for one repair, enforces the
  per-request call limit, and hands ADK an ordinary function call;
- switches from native to json on its own if the endpoint starts rejecting ``tools`` at run time;
- moves to the first usable ``WAPSI_AGENT_FALLBACK_MODELS`` entry while the configured model is not served or
  returns unusable output (a shared catalog can withdraw a model at any time), says so in ``describe()``, and
  goes back to the configured model when a later probe finds it healthy;
- refuses unusable model output (leaked control tokens, runaway repetition): the turn fails with
  ``EndpointOutputError`` and the officer gets a notice instead of garbage;
- records what the endpoint reported for every call (latency, tokens, and ``usage.cost`` where present).

Building the model makes no network call and never imports LiteLLM (that happens on the first request). The API
key lives in LiteLlm's private arguments: it is not in ``repr()`` or ``model_dump()``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.models.lite_llm import LiteLlm, LiteLLMClient
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr

from . import json_protocol as jp
from .endpoint import AgentEndpoint, EndpointStats
from .probe import (
    JSON,
    NATIVE,
    RESULT_ROLES,
    ROLE_TOOL,
    ProbeCache,
    ToolProbe,
    looks_degenerate,
    probe_tool_calling,
)

log = logging.getLogger("wapsi.agent.endpoint")

# LiteLLM downloads its model price map from GitHub at import unless told to use the bundled copy. The agent
# must not call anything but its own endpoint (on-prem deployments, offline tests).
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

REPROBE_AFTER_S = 30.0  # after a probe that could not assess the endpoint
FALLBACK_RECHECK_S = 600.0  # while on a fallback model: how often the configured model is probed again
_REJECT_STATUS = frozenset({400, 404, 422})
_MODEL_GONE = re.compile(r"model\b.{0,80}(not supported|not found|does not exist|not served|unavailable)",
                         re.IGNORECASE | re.DOTALL)


class EndpointOutputError(RuntimeError):
    """The endpoint answered, but with text no agent can use (see ``probe.looks_degenerate``)."""


def _status(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    return int(code) if isinstance(code, int) else None


def rejects_tools(exc: BaseException) -> bool:
    """The endpoint refused the ``tools`` / ``tool_choice`` parameters (not a rate limit, not an outage)."""
    return _status(exc) in _REJECT_STATUS and "tool" in str(exc).lower()


def model_not_served(exc: BaseException) -> bool:
    """The endpoint says it does not (or no longer) serve the requested model."""
    return _status(exc) in _REJECT_STATUS and bool(_MODEL_GONE.search(str(exc))) and not rejects_tools(exc)


def rejects_response_format(exc: BaseException) -> bool:
    text = str(exc).lower()
    return _status(exc) in _REJECT_STATUS and ("response_format" in text or "json" in text)


class EndpointClient(LiteLLMClient):
    """LiteLLM call seam: per-call parameters the endpoint needs, pacing, and what the endpoint reported."""

    def __init__(self, endpoint: AgentEndpoint, stats: EndpointStats) -> None:
        self.endpoint = endpoint
        self.stats = stats
        self.result_role = ROLE_TOOL if endpoint.tool_result_role == "auto" else endpoint.tool_result_role
        self._last_call = 0.0
        self._quiet = False

    async def _pace(self) -> None:
        gap = self.endpoint.min_interval_s
        if gap <= 0:
            return
        wait = self._last_call + gap - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    async def acompletion(self, model: Any, messages: Any, tools: Any, **kwargs: Any) -> Any:
        if not self._quiet:
            self._quiet = True
            try:
                import litellm

                litellm.suppress_debug_info = True  # no "Give Feedback" banners on errors
            except Exception:  # noqa: BLE001 - cosmetic
                pass
            if os.environ.get("WAPSI_AGENT_DEBUG", "").strip().lower() not in ("1", "true", "yes", "on"):
                # ADK points LiteLLM's own log handler at stdout; one INFO line per request there would corrupt
                # machine-readable output (the CLI's --json) whenever the host logs at INFO
                for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
                    logging.getLogger(name).setLevel(logging.WARNING)
        if "max_completion_tokens" in kwargs:  # vLLM and AMD Token Factory document ``max_tokens``; an agent's
            kwargs["max_tokens"] = kwargs.pop("max_completion_tokens")  # own limit wins over the endpoint default
        if tools and self.endpoint.parallel_tool_calls is not None:
            kwargs.setdefault("parallel_tool_calls", self.endpoint.parallel_tool_calls)
        # tool results go out under the role this endpoint understands (ADK picks one from the model's name)
        messages = [{**m, "role": self.result_role} if isinstance(m, dict) and m.get("role") in RESULT_ROLES
                    and m.get("role") != self.result_role else m for m in messages]
        await self._pace()
        t0 = time.perf_counter()
        self._last_call = time.monotonic()
        try:
            response = await super().acompletion(model=model, messages=messages, tools=tools, **kwargs)
        except Exception:
            self.stats.error()
            self._last_call = time.monotonic()
            raise
        self._last_call = time.monotonic()
        self.stats.record((time.perf_counter() - t0) * 1000.0, getattr(response, "usage", None))
        return response


class EndpointLlm(LiteLlm):
    """See the module docstring. Construct with ``build_endpoint_model(AgentEndpoint.from_env())``."""

    _endpoint: AgentEndpoint = PrivateAttr()
    _stats: EndpointStats = PrivateAttr()
    _mode: str | None = PrivateAttr(default=None)
    _probe: ToolProbe | None = PrivateAttr(default=None)
    _probe_failed_at: float | None = PrivateAttr(default=None)
    _fallback: dict[str, Any] | None = PrivateAttr(default=None)
    _fallback_at: float = PrivateAttr(default=0.0)
    _prober: Callable[[str], ToolProbe] | None = PrivateAttr(default=None)
    _probe_cache: ProbeCache | None = PrivateAttr(default=None)
    _json_object: bool = PrivateAttr(default=True)
    _mode_lock: Any = PrivateAttr(default=None)

    def __init__(self, endpoint: AgentEndpoint, *, client: Any | None = None,
                 prober: Callable[[str], ToolProbe] | None = None, probe_cache: ProbeCache | None = None) -> None:
        """``client``: an ``openai.AsyncOpenAI`` to send requests through (the offline fake endpoint passes one
        built on an in-process transport); ``prober(model_id)``: replaces the HTTP capability probe."""
        stats = EndpointStats()
        kwargs: dict[str, Any] = {
            "api_base": endpoint.base_url,
            "api_key": endpoint.api_key or "EMPTY",  # keyless local servers (vLLM without --api-key)
            "timeout": endpoint.timeout_s,
            "max_retries": endpoint.max_retries,
            "max_tokens": endpoint.max_tokens,
            "llm_client": EndpointClient(endpoint, stats),
        }
        if client is not None:
            kwargs["client"] = client
        if endpoint.extra_body:
            kwargs["extra_body"] = dict(endpoint.extra_body)
        super().__init__(model=endpoint.litellm_model, **kwargs)
        self._endpoint = endpoint
        self._stats = stats
        self._prober = prober
        self._probe_cache = probe_cache
        self._json_object = endpoint.json_object
        self._mode_lock = threading.Lock()

    def __repr__(self) -> str:
        return (f"EndpointLlm(model={self.model!r}, base_url={self._endpoint.base_url!r}, "
                f"tool_mode={self._mode or self._endpoint.tool_mode!r})")

    # ------------------------------------------------------------------ read-only views
    @property
    def endpoint(self) -> AgentEndpoint:
        return self._endpoint

    @property
    def stats(self) -> EndpointStats:
        return self._stats

    @property
    def tool_mode(self) -> str | None:
        """``native`` / ``json`` once known; ``None`` before the first request (mode ``auto``)."""
        return self._mode

    @property
    def probe(self) -> ToolProbe | None:
        return self._probe

    @property
    def active_model(self) -> str:
        """The model id requests go to: the configured one, or a fallback while that one is unusable."""
        return self.model.removeprefix("openai/")

    def describe(self) -> dict[str, Any]:
        return {**self._endpoint.describe(), "model": self.active_model, "configured_model": self._endpoint.model,
                "fallback": dict(self._fallback) if self._fallback else None,
                "tool_mode": self._mode or f"{self._endpoint.tool_mode} (not probed yet)",
                "tool_result_role": self.llm_client.result_role,
                "probe": self._probe.as_dict() if self._probe else None, "usage": self._stats.as_dict()}

    # ------------------------------------------------------------------ capability
    def _run_probe(self, model: str) -> ToolProbe:
        if self._prober is not None:
            return self._prober(model)
        e = self._endpoint
        return probe_tool_calling(e.base_url, model, e.api_key, timeout_s=min(30.0, e.timeout_s), default=NATIVE,
                                  pause_s=e.min_interval_s)

    def _settled(self) -> bool:
        if self._mode is None:
            return False
        return self._fallback is None or time.monotonic() - self._fallback_at < FALLBACK_RECHECK_S

    def _forget(self) -> None:
        """Drop what was learnt about the active model (it failed at run time): probe again before the next call."""
        if self._endpoint.tool_mode != "auto":
            return
        if self._probe_cache is not None:
            self._probe_cache.forget(self._endpoint.base_url, self.active_model)
        self._mode = None

    def resolve_mode(self) -> str:
        """``native`` or ``json`` for this endpoint, and which model answers; blocking HTTP in ``auto`` mode.

        The configured model is probed first, then each fallback model: the first that passes is used. A probe
        that could not assess the endpoint is not kept: the configured model with native tool calls is assumed
        and the probe is repeated later. While a fallback is active the configured model is probed again every
        ``FALLBACK_RECHECK_S`` seconds."""
        if self._settled():
            return self._mode  # type: ignore[return-value]
        with self._mode_lock:
            if self._settled():
                return self._mode  # type: ignore[return-value]
            e = self._endpoint
            if e.tool_mode in (NATIVE, JSON):
                self._mode = e.tool_mode
                self._probe = ToolProbe(mode=e.tool_mode, ok=True, reason="set by WAPSI_AGENT_TOOL_MODE",
                                        base_url=e.base_url, model=e.model, result_role=self.llm_client.result_role)
                return self._mode
            if self._probe_failed_at is not None and time.monotonic() - self._probe_failed_at < REPROBE_AFTER_S:
                return NATIVE
            first: ToolProbe | None = None
            for model in dict.fromkeys((e.model, *e.fallback_models)):
                cached = self._probe_cache.get(e.base_url, model) if self._probe_cache else None
                probe = cached or self._run_probe(model)
                first = first or probe
                if not probe.ok:
                    log.warning("agent endpoint %s: model %s is not usable now (%s)", e.base_url, model,
                                e.scrub(probe.reason))
                    continue
                self.model = f"openai/{model}"
                self._mode, self._probe, self._probe_failed_at = probe.mode, probe, None
                if e.tool_result_role == "auto" and probe.result_role in RESULT_ROLES:
                    self.llm_client.result_role = probe.result_role
                if self._probe_cache is not None and not probe.cached:
                    self._probe_cache.put(probe)
                self._fallback = None
                if model != e.model and first is not None:
                    self._fallback = {"configured_model": e.model, "using": model, "reason": e.scrub(first.reason)}
                    self._fallback_at = time.monotonic()
                    log.warning("agent endpoint %s: using fallback model %s instead of %s", e.base_url, model, e.model)
                log.info("agent endpoint %s model %s: %s tool mode (%s)", e.base_url, model, probe.mode, probe.reason)
                return self._mode
            # nothing usable right now: assume the configured model with native tool calls and probe again later
            self.model, self._mode, self._fallback = e.litellm_model, None, None
            self._probe = first
            self._probe_failed_at = time.monotonic()
            return NATIVE

    # ------------------------------------------------------------------ generation
    async def generate_content_async(self, llm_request: LlmRequest,
                                     stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        mode = self._mode if self._settled() else await asyncio.to_thread(self.resolve_mode)
        llm_request.model = self.model  # the model that answers (a fallback may have been chosen just now)
        if mode == NATIVE:
            try:
                responses = []
                async for r in super().generate_content_async(llm_request, stream=False):
                    responses.append(r)
            except Exception as exc:  # noqa: BLE001 - only one kind is handled here; the rest is re-raised
                if model_not_served(exc):
                    self._forget()  # the catalog changed under us: probe (and fall back) before the next call
                if (self._endpoint.tool_mode == NATIVE or not rejects_tools(exc)
                        or not jp.tool_specs(llm_request)):
                    raise
                log.warning("the endpoint rejected native tool calls at run time; switching to the JSON tool "
                            "protocol: %s", self._endpoint.scrub(str(exc))[:200])
                self._mode = JSON
                self._probe = ToolProbe(mode=JSON, ok=True, base_url=self._endpoint.base_url,
                                        model=self.active_model, status_code=_status(exc),
                                        reason="the endpoint rejected a tools request at run time")
            else:
                for r in responses:
                    self._check_output(r)
                    yield r
                return
        async for r in self._generate_json(llm_request):
            yield r

    def _check_output(self, response: LlmResponse) -> None:
        """Refuse text no agent can use; a response that carries a tool call is judged by the call alone."""
        parts = (response.content.parts if response.content else None) or []
        if any(p.function_call for p in parts):
            return
        bad = looks_degenerate(" ".join(p.text for p in parts if p.text and not p.thought))
        if bad:
            self._forget()  # probe again (and fall back, if configured) before the next call
            raise EndpointOutputError(f"the model endpoint returned unusable output ({bad})")

    async def _complete(self, request: LlmRequest) -> LlmResponse:
        last: LlmResponse | None = None
        async for r in super().generate_content_async(request, stream=False):
            last = r
        if last is None:
            raise RuntimeError("the endpoint returned no response")
        return last

    def _plain_request(self, llm_request: LlmRequest, specs: list[jp.ToolSpec], calls_left: int) -> LlmRequest:
        cfg = llm_request.config
        system = jp.system_text(cfg)
        block = jp.protocol_instruction(specs, calls_left=calls_left)
        config = types.GenerateContentConfig(
            system_instruction=f"{system}\n\n{block}" if system else block,
            temperature=getattr(cfg, "temperature", None),
            top_p=getattr(cfg, "top_p", None),
            http_options=(types.HttpOptions(extra_body={**self._endpoint.extra_body,
                                                        "response_format": {"type": "json_object"}})
                          if self._json_object else None),
        )
        return LlmRequest(model=llm_request.model, contents=jp.rewrite_contents(llm_request.contents), config=config)

    async def _generate_json(self, llm_request: LlmRequest) -> AsyncGenerator[LlmResponse, None]:
        specs = jp.tool_specs(llm_request)
        if not specs:  # nothing to call: an ordinary completion
            yield await self._complete(llm_request)
            return
        limit = self._endpoint.max_tool_calls
        used = jp.count_tool_results(llm_request.contents)
        plain = self._plain_request(llm_request, specs, limit - used)
        tokens_in = tokens_out = 0
        action: jp.Action = jp.ProtocolError("no answer")
        response: LlmResponse | None = None
        for attempt in range(jp.MAX_REPAIRS + 1):
            try:
                response = await self._complete(plain)
            except Exception as exc:  # noqa: BLE001
                if model_not_served(exc):
                    self._forget()  # probe (and fall back) before the next call
                    raise
                if not (self._json_object and rejects_response_format(exc)):
                    raise
                log.warning("the endpoint rejected response_format json_object; continuing without it")
                self._json_object = False
                plain = self._plain_request(llm_request, specs, limit - used)
                response = await self._complete(plain)
            usage = response.usage_metadata
            tokens_in += int(getattr(usage, "prompt_token_count", 0) or 0)
            tokens_out += int(getattr(usage, "candidates_token_count", 0) or 0)
            text = " ".join(p.text for p in ((response.content.parts if response.content else None) or [])
                            if p.text and not p.thought)
            self._check_output(response)
            action = jp.parse_action(text, specs)
            if not isinstance(action, jp.ProtocolError) or attempt == jp.MAX_REPAIRS:
                break
            plain.contents = [*plain.contents,
                              types.Content(role="model", parts=[types.Part(text=text or "(empty)")]),
                              types.Content(role="user", parts=[types.Part(text=jp.repair_message(action.message))])]
        if isinstance(action, jp.ToolCall) and used >= limit:
            part = types.Part(text=jp.call_limit_notice(limit))
        elif isinstance(action, jp.ToolCall):
            part = types.Part(function_call=types.FunctionCall(name=action.name, args=action.args))
        elif isinstance(action, jp.Final):
            part = types.Part(text=action.text)
        else:
            part = types.Part(text=jp.invalid_notice(action.message))
        out = LlmResponse(content=types.Content(role="model", parts=[part]), turn_complete=True,
                          model_version=getattr(response, "model_version", None))
        if tokens_in or tokens_out:
            out.usage_metadata = types.GenerateContentResponseUsageMetadata(
                prompt_token_count=tokens_in, candidates_token_count=tokens_out,
                total_token_count=tokens_in + tokens_out)
        yield out
