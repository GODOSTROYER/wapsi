"""One set of environment names across the pieces: the pipeline, the eval harness, the agent, voice intake, the
demo container, the AMD kickoff scripts and the example env files.

Each piece was built on its own branch. These tests pin what makes them one product: the same three variables
point every model call at one endpoint, the deployer states "this runs on AMD" once, one ordered model list serves
every surface that fails over, and no example file invents a spelling of its own.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

TF = "https://developer.amd.com.cn/radeon/api/v1"
OTHER = "https://gpu.example.org/v1"
ENDPOINT = ("WAPSI_LLM_BASE_URL", "WAPSI_LLM_API_KEY", "WAPSI_LLM_MODEL")


def _env(**extra: str) -> dict[str, str]:
    return {"WAPSI_LLM_BASE_URL": TF, "WAPSI_LLM_API_KEY": "test-key", "WAPSI_LLM_MODEL": "Gemma-4-31B-IT", **extra}


# --------------------------------------------------------------------------- the three variables


def test_every_piece_reads_the_same_three_endpoint_variables(monkeypatch):
    from wapsi.adapters import llm_env
    from wapsi.adapters.llm_openai_compat import API_KEY_ENV
    from wapsi.intake import backends

    assert (llm_env.BASE_URL_ENV, API_KEY_ENV, llm_env.MODEL_ENV) == ENDPOINT == llm_env.ENDPOINT_ENVS
    assert (backends.LLM_BASE_URL_ENV, backends.LLM_API_KEY_ENV, backends.LLM_MODEL_ENV) == ENDPOINT

    # the pipeline / harness / demo adapter
    cfg = llm_env.endpoint_config(_env())
    assert (cfg.base_url, cfg.model, cfg.has_key, cfg.json_mode) == (TF, "Gemma-4-31B-IT", True, "json_object")
    # the agent
    from editions.shared.endpoint import AgentEndpoint

    agent = AgentEndpoint.from_env(_env())
    assert agent is not None and (agent.base_url, agent.model, agent.api_key) == (TF, "Gemma-4-31B-IT", "test-key")
    # the AMD edition app
    from editions.amd.app.settings import load_settings

    amd = load_settings(_env())
    assert (amd.llm_base_url, amd.llm_model) == (TF, "Gemma-4-31B-IT")
    # the kickoff's metered client
    sys.path.insert(0, str(ROOT / "editions" / "amd" / "kickoff"))
    import endpoint as kickoff_endpoint

    assert kickoff_endpoint.KEY_ENVS[0] == "WAPSI_LLM_API_KEY" and kickoff_endpoint.api_key(_env()) == "test-key"
    assert kickoff_endpoint.default_json_mode(TF, {}) == "json_object"
    # voice intake: the same endpoint structures the transcript
    for name, value in _env().items():
        monkeypatch.setenv(name, value)
    assert backends.llm_provider(TF) == ("AMD Token Factory", True)


def test_a_provider_endpoint_never_gets_a_strict_json_schema_by_default():
    """The Token Factory answers HTTP 400 to ``json_schema``; a stack's own vLLM keeps guided decoding."""
    from editions.amd.app.settings import default_json_mode, load_settings
    from wapsi.adapters.llm_env import endpoint_config

    assert load_settings(_env()).llm_json_mode == "json_object" == endpoint_config(_env()).json_mode
    assert load_settings({}).llm_json_mode == "json_schema"  # the compose stack: http://llm:8000/v1
    assert default_json_mode("http://127.0.0.1:8000/v1") == "json_schema"
    assert default_json_mode(OTHER) == "json_object"
    assert load_settings(_env(WAPSI_LLM_JSON_MODE="json_schema")).llm_json_mode == "json_schema"  # said explicitly


# --------------------------------------------------------------------------- "on AMD" is stated once


@pytest.mark.parametrize("statement", [{"WAPSI_LLM_PLATFORM": "AMD"}, {"WAPSI_LLM_ON_AMD": "1"}])
def test_one_statement_of_the_platform_reaches_the_chip_voice_and_the_agent(statement):
    from editions.shared.endpoint import AgentEndpoint
    from wapsi.adapters import llm_env
    from wapsi.intake import backends

    env = {"WAPSI_LLM_BASE_URL": OTHER, "WAPSI_LLM_MODEL": "wapsi-llm", **statement}
    assert llm_env.declared_platform(env) == "AMD" and llm_env.on_amd(OTHER, env)
    assert llm_env.endpoint_config(env).served_name == "wapsi-llm on AMD @ gpu.example.org"  # the draft chip
    assert backends.llm_provider(OTHER, env)[1] is True  # voice intake's "on AMD"
    assert AgentEndpoint.from_env(env).provider == "OpenAI-compatible endpoint (gpu.example.org), on AMD as declared"


def test_nothing_is_called_amd_without_a_statement_or_an_amd_host():
    from editions.shared.endpoint import AgentEndpoint
    from wapsi.adapters import llm_env
    from wapsi.intake import backends

    env = {"WAPSI_LLM_BASE_URL": OTHER, "WAPSI_LLM_MODEL": "wapsi-llm"}
    assert llm_env.declared_platform(env) is None and not llm_env.on_amd(OTHER, env)
    assert llm_env.endpoint_config(env).served_name == "wapsi-llm @ gpu.example.org"
    assert backends.llm_provider(OTHER, env)[1] is False
    assert "AMD" not in AgentEndpoint.from_env(env).provider
    # AMD's own host says it itself
    assert llm_env.on_amd(TF, {}) and AgentEndpoint.from_env(_env()).provider == "AMD Token Factory"


def test_the_amd_app_names_what_really_serves_the_drafting_model(monkeypatch):
    """A draft written through the Token Factory is never labelled as this stack's vLLM-ROCm server."""
    from editions.amd.app.services import llm_name
    from wapsi.adapters import llm_env
    from wapsi.adapters.llm_env import parse_served

    monkeypatch.delenv("WAPSI_LLM_PLATFORM", raising=False)
    # a server of one's own: "on AMD" only when confirmed, never guessed from a loopback or bare host name
    monkeypatch.setattr(llm_env, "rocm_visible", lambda: False)
    assert llm_name("http://127.0.0.1:8000/v1", "wapsi-llm") == "vllm:wapsi-llm"  # a laptop vLLM / Ollama
    assert llm_name("http://llm:8000/v1", "wapsi-llm") == "vllm:wapsi-llm"
    assert parse_served("vllm:wapsi-llm").platform is None and "AMD" not in parse_served("vllm:wapsi-llm").label
    assert llm_name("http://llm:8000/v1", "wapsi-llm", {"WAPSI_LLM_PLATFORM": "AMD"}) == "vllm-rocm:wapsi-llm"
    monkeypatch.setattr(llm_env, "rocm_visible", lambda: True)  # an AMD GPU notebook or droplet
    assert llm_name("http://127.0.0.1:8000/v1", "wapsi-llm") == "vllm-rocm:wapsi-llm"
    monkeypatch.setattr(llm_env, "rocm_visible", lambda: False)
    name = llm_name(TF, "Gemma-4-31B-IT")
    assert name == "Gemma-4-31B-IT on AMD @ developer.amd.com.cn"
    assert parse_served(name).label == "Gemma 4 on AMD via developer.amd.com.cn"
    assert llm_name(OTHER, "wapsi-llm") == "wapsi-llm @ gpu.example.org"
    monkeypatch.setenv("WAPSI_LLM_PLATFORM", "AMD")
    assert llm_name(OTHER, "wapsi-llm") == "wapsi-llm on AMD @ gpu.example.org"


# --------------------------------------------------------------------------- one ordered model list


def test_one_model_list_serves_voice_the_agent_and_the_kickoff_plan():
    from editions.amd.kickoff import model_plan
    from editions.shared.endpoint import AgentEndpoint
    from wapsi.intake.llm_chain import model_names

    chain = "Gemma-4-31B-IT, DeepSeek-V4-Flash, Qwen3.8-27B"
    env = _env(WAPSI_LLM_MODELS=chain)
    assert model_names(env, "Gemma-4-31B-IT") == ["Gemma-4-31B-IT", "DeepSeek-V4-Flash", "Qwen3.8-27B"]
    assert AgentEndpoint.from_env(env).fallback_models == ("DeepSeek-V4-Flash", "Qwen3.8-27B")
    plan = model_plan.plan_endpoint(["DeepSeek-V4-Flash", "Qwen3.8-27B"],
                                    {"WAPSI_LLM_MODELS": chain, "WAPSI_ALLOW_MODEL_FALLBACK": "1"})
    assert plan["drafting"]["id"] == "DeepSeek-V4-Flash" and plan["drafting"]["candidates"][0] == "Gemma-4-31B-IT"
    # the kickoff's own spelling is read by voice and the agent too
    env = _env(WAPSI_LLM_FALLBACK_MODELS=chain)
    assert model_names(env, "Gemma-4-31B-IT")[1:] == ["DeepSeek-V4-Flash", "Qwen3.8-27B"]
    assert AgentEndpoint.from_env(env).fallback_models == ("DeepSeek-V4-Flash", "Qwen3.8-27B")


def test_the_agent_fails_over_only_when_asked_and_only_on_its_own_endpoint():
    from editions.shared.endpoint import AgentEndpoint

    assert AgentEndpoint.from_env(_env()).fallback_models == ()  # no list: no silent substitute
    own = _env(WAPSI_LLM_MODELS="Gemma-4-31B-IT,Qwen3.8-27B", WAPSI_AGENT_BASE_URL=OTHER)
    assert AgentEndpoint.from_env(own).fallback_models == ()  # another endpoint serves other ids
    explicit = _env(WAPSI_LLM_MODELS="Gemma-4-31B-IT,Qwen3.8-27B", WAPSI_AGENT_FALLBACK_MODELS="MiMo-V2.6-Flash")
    assert AgentEndpoint.from_env(explicit).fallback_models == ("MiMo-V2.6-Flash",)  # the agent's own list wins


# --------------------------------------------------------------------------- example files and docs

EXAMPLES = ("deploy/demo/.env.example", "editions/amd/.env.example", "editions/amd/kickoff/endpoint.env.example")
# spellings that would be a second name for something that already has one
FORBIDDEN = re.compile(r"\b(?:WAPSI_(?:OPENAI|ENDPOINT)_(?:BASE_URL|URL|API_KEY|KEY|MODEL)|WAPSI_LLM_(?:URL|KEY|ENDPOINT)|"
                       r"WAPSI_WHISPER_\w+|WAPSI_STT_\w+|WAPSI_VOICE_PROVIDER|AMD_API_KEY|TOKEN_FACTORY_API_KEY)\b")


def test_example_env_files_use_the_shared_names_and_hold_no_key():
    for rel in EXAMPLES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not FORBIDDEN.search(text), rel
        for line in text.splitlines():
            m = re.match(r"\s*#?\s*([A-Z][A-Z0-9_]*(?:API_KEY|_TOKEN|SIGNING_KEY))=(.*)$", line)
            assert not (m and m.group(2).strip()), f"{rel}: {m.group(1) if m else ''} has a value"
    demo = (ROOT / "deploy/demo/.env.example").read_text(encoding="utf-8")
    kickoff = (ROOT / "editions/amd/kickoff/endpoint.env.example").read_text(encoding="utf-8")
    for name in ENDPOINT:
        assert name in demo and name in kickoff, name
    amd = (ROOT / "editions/amd/.env.example").read_text(encoding="utf-8")
    for name in ("WAPSI_VOICE_BACKEND", "WAPSI_ASR_BASE_URL", "WAPSI_ASR_MODEL", "WAPSI_AGENT_BACKEND"):
        assert name in amd, name


def test_docs_and_code_agree_on_the_names():
    files = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs").glob("*.md")),
             *sorted((ROOT / "pitch").rglob("*.md")), *sorted((ROOT / "deploy").rglob("*.md")),
             *sorted((ROOT / "editions" / "amd").glob("*.md")), ROOT / "editions/amd/kickoff/README.md"]
    for path in files:
        hit = FORBIDDEN.search(path.read_text(encoding="utf-8"))
        assert not hit, f"{path.relative_to(ROOT)}: {hit.group(0) if hit else ''}"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in (*ENDPOINT, "WAPSI_VOICE_BACKEND", "WAPSI_ASR_BACKEND"):
        assert name in readme, name
