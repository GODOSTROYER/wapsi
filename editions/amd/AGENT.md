# The officer agent on AMD: Google ADK + Gemma 4, served on AMD

The AMD edition runs the same `wapsi_officer` agent as the Google Cloud edition: Google's open-source
[Agent Development Kit](https://google.github.io/adk-docs/) (ADK), the ten officer tools, the guard, audit and
trace plugins. The model is an **open Google model, Gemma 4, served on AMD** behind an OpenAI-compatible endpoint.
No Google Cloud service is involved at run time: no BigQuery, no Firestore, no Model Armor, no Gemini API.

```
officer message
   │  input guard (RulesGuard, or rules + verifier LLM)        editions/shared/plugins.py
   ▼
ADK LlmAgent "wapsi_officer" ── LiteLlm (openai/<model>) ──► OpenAI-compatible endpoint on AMD
   │                             editions/shared/endpoint_model.py      AMD Token Factory (Gemma-4-31B-IT)
   │  one tool call per turn                                            vLLM-ROCm on an MI300X droplet
   ▼                                                                    vLLM in a GPU notebook
officer tools (editions/shared/tools.py)
   open_case · trace_money · hold_plan · verification_report · draft_documents · explain_account
   approve_draft · search_sop · case_clocks · list_cases
   │  ParquetLedger · FileCaseStore · LocalDocSearch · heuristic / GNN scorer     (editions/amd/app/services.py)
   ▼
reply ── grounding check: every ₹ amount and account number must come from a tool result
   │
   ▼
AgentTrace ── sealed into the case's hash-chained audit log (FileCaseStore.append_audit)
```

The agent never computes money. Amounts come from the pipeline, already formatted; the model quotes them. If the
model's reply contains a rupee amount or account number that no tool returned, the reply is **not shown**: the
officer gets a summary built only from the tool results, and the trace records the failed check. (This is not
hypothetical. In the first live run on 2026-10-14, Gemma 4 merged two trail totals into one line, ₹19,200.00 +
₹1,152.00 = ₹20,352.00. That one figure, of the 18 in the reply, was in no tool result, so the reply was replaced.
The instruction now tells the model to keep each total on its own line, and the next live run passed.)

## Run it

Offline (no endpoint, no key): the real ADK + LiteLLM client stack against a deterministic fake OpenAI-compatible
server in the same process.

```bash
uv sync --extra agent --extra amd-app --extra ui --extra ml --extra docs     # or: uv sync --all-extras
uv run python -m editions.amd.app.agent_demo --case 31009260000101           # tool-by-tool trace + verified outcome
uv run python -m editions.amd.app.agent_demo --case 31009260000101 --demo    # + explain, refused approval, injection
uv run python -m editions.amd.app.agent_demo --case 31009260000101 --fake-tools reject   # JSON tool protocol
```

Live, on Gemma 4 served on AMD (AMD Token Factory is OpenAI-compatible; the key comes from your shell and is never
printed, logged or stored):

```bash
export WAPSI_LLM_BASE_URL=https://developer.amd.com.cn/radeon/api/v1
export WAPSI_LLM_MODEL=Gemma-4-31B-IT
read -rs WAPSI_LLM_API_KEY && export WAPSI_LLM_API_KEY
export WAPSI_AGENT_MIN_INTERVAL_S=4          # shared limit: 20 requests/min per team
uv run python -m editions.amd.app.agent_demo --case 31009260000101
```

API and UI (demo mode shown; drop `WAPSI_DEMO_MODE=1` to draft on the endpoint too):

```bash
WAPSI_DEMO_MODE=1 WAPSI_STATE_DIR=.wapsi-state uv run uvicorn editions.amd.app.api:app --port 8080
curl -s http://127.0.0.1:8080/agent
curl -s -X POST http://127.0.0.1:8080/agent/run -H 'content-type: application/json' \
     -d '{"message": "Open case 31009260000101", "user": "asha"}'
curl -s 'http://127.0.0.1:8080/agent/traces?case_id=CASE-31009260000101'

WAPSI_DEMO_MODE=1 WAPSI_STATE_DIR=.wapsi-state uv run streamlit run editions/amd/app/ui_app.py
# Case Room → "Agent trace" tab → "Ask the agent"
```

`WAPSI_DEMO_MODE=1 WAPSI_AGENT_BACKEND=endpoint` (with the three `WAPSI_LLM_*` variables) gives a live agent while
draft prose stays on the offline `FakeLLM`: one endpoint request per model turn, the cheapest live demo.

| Endpoint | What it does |
|---|---|
| `GET /agent` | backend, model, serving platform, tool mode, probe result, usage counters. No secrets. |
| `POST /agent/run` | `{"message", "user", "role", "case_id", "session", "include_trace"}` → reply, reply source, tool sequence, verdict summary, trace. Needs the bearer token when `WAPSI_API_TOKEN` is set. A role other than `analyst` is honoured only for a token-authenticated caller (or in demo mode). |
| `GET /agent/traces` | `?case_id=`: the runs sealed into that case's audit log plus the hash-chain check; without it, this process's recent runs. |

## Backends and settings

`WAPSI_AGENT_BACKEND`: `auto` (default: the endpoint, or the offline fake endpoint when `WAPSI_DEMO_MODE=1`),
`endpoint`, `offline`.

| Variable | Default | Meaning |
|---|---|---|
| `WAPSI_LLM_BASE_URL` / `WAPSI_LLM_API_KEY` / `WAPSI_LLM_MODEL` | the edition's LLM settings | the endpoint the agent (and the drafter) uses |
| `WAPSI_AGENT_BASE_URL` / `WAPSI_AGENT_API_KEY` / `WAPSI_AGENT_MODEL` | the three above | a different endpoint or model for the agent only |
| `WAPSI_AGENT_TOOL_MODE` | `auto` | `auto` = capability probe; `native`; `json` |
| `WAPSI_AGENT_TOOL_RESULT_ROLE` | `auto` | `auto` = what the probe found; `tool`; `tool_responses` |
| `WAPSI_AGENT_FALLBACK_MODELS` | the other models of `WAPSI_LLM_MODELS` | other model ids on the same endpoint, used in order while the configured model is not served or returns unusable output; runs are labelled with the model that answered. Unset: the list voice intake fails over along (`WAPSI_LLM_MODELS`, else `WAPSI_LLM_FALLBACK_MODELS`) when the agent runs on the drafting endpoint; with none of the three set there is no fallback |
| `WAPSI_AGENT_MAX_TOOL_CALLS` / `WAPSI_AGENT_MAX_LLM_CALLS` | `12` / `24` | bounds per officer message |
| `WAPSI_AGENT_MIN_INTERVAL_S` | `0` | pause between model calls (shared rate limits) |
| `WAPSI_AGENT_TIMEOUT_S`, `WAPSI_AGENT_MAX_TOKENS`, `WAPSI_AGENT_TEMPERATURE`, `WAPSI_AGENT_RETRIES`, `WAPSI_AGENT_PARALLEL_TOOLS`, `WAPSI_AGENT_JSON_OBJECT`, `WAPSI_AGENT_EXTRA_BODY`, `WAPSI_AGENT_PROVIDER_LABEL` | see `editions/shared/endpoint.py` | request shape |

## Tool calling: native when the endpoint has it, a strict JSON protocol when it does not

On first use the model layer probes the endpoint (`editions/shared/probe.py`, two small requests, cached for a day
in `<WAPSI_STATE_DIR>/agent_tool_probe.json`):

1. Does a request with `tools` come back with an OpenAI `tool_calls` entry?
2. Does the model use the tool result, and under which message role? ADK's `LiteLlm` sends tool results as
   `role: "tool_responses"` for model ids that match `gemma-4`; an OpenAI gateway expects `role: "tool"`. The probe
   tries both and the agent then sends the one that worked.

| Probe result | Agent behaviour |
|---|---|
| tool call returned, result understood | **native**: tools go out as OpenAI `tools`, one call per turn (`parallel_tool_calls: false`) |
| HTTP 4xx about tools, or a text answer | **json**: the strict JSON tool protocol below |
| leaked control tokens, runaway repetition, 401, 429, 5xx, model not served | not decided: nothing is cached, the officer gets a plain notice, the next message probes again (or moves to `WAPSI_AGENT_FALLBACK_MODELS`) |

**Strict JSON tool protocol** (`editions/shared/json_protocol.py`): the tool declarations go into the system
message as JSON Schemas; the model must answer with exactly one JSON object per turn, `{"tool": "<name>", "args":
{...}}` or `{"final": "<reply>"}` (`response_format: json_object` when the endpoint supports it). The tool must be
a declared one, the arguments must satisfy its schema (required keys, types, no extra keys), one call per turn, at
most `WAPSI_AGENT_MAX_TOOL_CALLS` per officer message. An answer that breaks the protocol gets one repair request
that names the problem; after that the turn ends with a notice and nothing is executed. Prose is never accepted as
a reply in this mode.

### Serving Gemma 4 with native tool calls on your own AMD GPU (vLLM-ROCm)

Token Factory needs nothing: its `Gemma-4-31B-IT` returns native tool calls. For your own vLLM server (the MI300X
droplet or a GPU notebook) the flags are:

```bash
vllm serve google/gemma-4-31B-it --served-model-name wapsi-llm \
  --enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4 \
  --attention-backend TRITON_ATTN --max-model-len 16384
```

`serving/start_llm.sh` adds them when `VLLM_ENABLE_TOOLS=1`:

```bash
VLLM_ENABLE_TOOLS=1 bash editions/amd/serving/start_llm.sh print gemma4-base    # show the command
VLLM_ENABLE_TOOLS=1 bash editions/amd/serving/start_llm.sh serve gemma4-base    # run it (container or notebook shell)
# docker compose: set VLLM_ENABLE_TOOLS=1 in editions/amd/.env, then `docker compose up -d`
```

Overrides: `VLLM_TOOL_CALL_PARSER`, `VLLM_REASONING_PARSER` (`none` omits it), `VLLM_CHAT_TEMPLATE` (the vLLM
Gemma 4 recipe uses `--chat-template examples/tool_chat_template_gemma4.jinja`; leave it empty to use the model's
own template). Without the flags vLLM answers `"auto" tool choice requires --enable-auto-tool-choice and
--tool-call-parser to be set`; the probe reads that as "no native tool calls" and the agent uses the JSON protocol,
so it works either way. A LoRA-merged checkpoint that has lost its tool-call format is handled the same way.

Then point the app at it: `WAPSI_LLM_BASE_URL=http://127.0.0.1:8000/v1 WAPSI_LLM_MODEL=wapsi-llm`
(`http://llm:8000/v1` inside the compose network, which is the default).

## What was measured

**AMD Token Factory, `Gemma-4-31B-IT`, measured 2026-10-14.** These are requests answered by AMD's hosted
endpoint (the provider states the models run on AMD GPUs). They are not MI300X droplet benchmarks, and the latencies
include network time from the developer's laptop and whatever load the shared endpoint had.

| Run | Model calls | Errors | Latency per call (mean / p50 / max) | Tokens in / out | Cost reported by the endpoint |
|---|---|---|---|---|---|
| Showcase case, "Open case 31009260000101": 5 tool calls, 6 model turns, reply grounded | 6 | 0 | 4.69 s / 3.09 s / 14.59 s | 45,468 / 867 | $0.000752 |
| Same case, first run (reply replaced by the tool-result summary, see above) | 6 | 0 | 6.09 s / 4.96 s / 17.07 s | 45,072 / 739 | $0.000778 |
| Demo script, 4 officer messages (open, explain, analyst approval, injected instruction) | 12 | 0 | 12.73 s / 4.20 s / 83.95 s | 171,975 / 1,718 | $0.001049 |
| JSON tool protocol forced (`WAPSI_AGENT_TOOL_MODE=json`), "Show the hold plan." | 2 | 0 | 9.03 s mean | 6,529 / 881 | $0.000247 |

- Capability probe: native tool calls, tool results understood with `role: "tool"`, 2 requests, 2.6 s for the
  first.
- Draft prose in these runs came from the offline `FakeLLM`; only the agent's model turns went to the endpoint.
- The slowest call is always the final reply: the prompt then holds every tool result (about 18,000 tokens).
- Demo script: the analyst's approval was refused by maker-checker before the tool ran; the message with an
  injected instruction was rewritten by the input guard and the model searched the SOP without following it.
- Every run ended VERIFIED: plan policy check passed, 16 of 16 drafts verified, audit chain intact with the agent
  runs sealed in it.

**The same day, earlier (roughly 00:40 to 01:10 UTC by the developer's clock), the shared `Gemma-4-31B-IT`
deployment was degraded and then withdrawn for a while.** Plain and tool requests came back as
`<|tool_call>//1//////…` with `finish_reason: length`; later the endpoint answered HTTP 400 `Requested model
Gemma-4-31B-IT not supported` and `GET /models` did not list it. It was back by 01:15 UTC. The agent's handling of
both states comes from that hour: unusable output is
refused instead of being shown or parsed, a withdrawn model is reported as such (with the ids the endpoint does
serve), and `WAPSI_AGENT_FALLBACK_MODELS` lets a deployment keep working on another AMD-served model, labelled
honestly, until the configured one is back. The fallback path is covered by offline tests; it was not exercised
live because Gemma returned first.

Offline runs print stand-in numbers (the fake endpoint counts characters) and say so. No GPU measurement is claimed
anywhere in this file.

## Safety properties, and where each is enforced

| Property | Where |
|---|---|
| Untrusted text is quoted, never obeyed | input guard rewrites flagged officer text; free-text tool arguments are screened and blocked; complaint text reaches the model fenced and sanitized (`editions/shared/plugins.py`, `tools.py`) |
| Only a supervisor approves | role comes from a signed session grant, checked by the guard plugin and again by `approve_draft`; `wapsi.pipeline.run.approve` re-verifies the draft |
| Verification before drafts | `draft_documents` refuses until `verification_report` was shown in the session |
| The agent never computes money | reply-grounding check; ungrounded replies are replaced by the tool-result summary (`editions/shared/runtime.py`) |
| Bounded | `WAPSI_AGENT_MAX_LLM_CALLS` per run (ADK `RunConfig`), `WAPSI_AGENT_MAX_TOOL_CALLS` per message in JSON mode, one repair per turn |
| Auditable | every run (model turns, tool calls, guard and verifier verdicts, redacted arguments) is an `agent_trace` event sealed into the case's hash chain; `GET /agent/traces?case_id=` re-verifies it |
| No secrets in outputs | the key lives in LiteLLM's private arguments; `describe()`, `repr`, traces, notices and errors are scrubbed (`AgentEndpoint.scrub`) |

## Code map

| Path | What |
|---|---|
| `editions/shared/` | edition-neutral agent code: `tools`, `plugins`, `tracing`, `identity`, `instructions`, `scripted`, `context`, plus `endpoint`, `endpoint_model`, `probe`, `json_protocol`, `fake_openai`, `runtime` |
| `editions/amd/app/agent.py` | AMD wiring: backend choice, `build_app`, `build_runtime`, `get_runtime`, `ask`, `agent_status` |
| `editions/amd/app/agent_demo.py` | the CLI |
| `editions/amd/app/api.py` | `GET /agent`, `POST /agent/run`, `GET /agent/traces` |
| `editions/amd/app/ui_source.py`, `ui/components/agent_chat.py` | "Ask the agent" in the Case Room |
| `editions/gcp/app/{tools,plugins,tracing,identity,demo_model}.py` | the Google Cloud edition's names for the shared modules (same objects) |
| `tests/amd/test_amd_agent_*.py` | endpoint settings, probe, JSON protocol, wire round trips, runtime, API, CLI, UI |
