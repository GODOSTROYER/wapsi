"""Edition-neutral agent code: the Google ADK officer agent that both editions run.

The Google Cloud edition runs it on Gemini (or Gemma on Cloud Run GPU); the AMD edition runs the same agent, tools,
guard and trace plugins on an open Google model (Gemma 4) served on AMD behind an OpenAI-compatible endpoint
(AMD Token Factory, vLLM-ROCm on an MI300X, or a notebook). Nothing in this package imports a Google Cloud client.

Modules (import them explicitly; importing the package has no side effects and does not need google-adk):
    context        the services the tools use (ledger, case store, LLM, guard, docs search, action sink)
    identity       HMAC-signed role grants in ADK session state (maker-checker)
    tools          the ten officer tools wrapping the Wapsi pipeline (amounts returned already formatted)
    plugins        input / argument / output guard, audit log, quota notice, memory, voice-upload plugins
    tracing        ``ToolTracePlugin``: every run recorded as an ``AgentTrace`` and sealed into the audit chain
    instructions   the officer agent's system instruction (one text for every model)
    scripted       offline planner (``DemoScriptedLlm`` for ADK; ``next_officer_action`` for the fake endpoint)
    endpoint       ``AgentEndpoint``: OpenAI-compatible endpoint settings from the environment (no ADK import)
    endpoint_model ``EndpointLlm``: ADK ``LiteLlm`` on that endpoint (probe, tool mode, fallback model, usage)
    probe          capability probe: does the endpoint return native tool calls?
    json_protocol  strict one-call-per-turn JSON tool protocol for endpoints without native tool calls
    fake_openai    deterministic fake OpenAI-compatible backend (httpx transport) for the offline demo and tests
    runtime        ``AgentRuntime``: one long-lived event loop, sessions, runs, verified results
"""
