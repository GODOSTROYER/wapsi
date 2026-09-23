"""Wapsi AMD edition: pipeline + API + Streamlit UI against vLLM-ROCm on one MI300X.

Modules:
    settings    environment -> ``AmdSettings``
    services    adapters (ParquetLedger, FileCaseStore, OpenAICompatLLM / FakeLLM, guard, GNN scorer)
    api         FastAPI: POST /cases/{ids}/process, GET /cases, GET /cases/{case_id}, GET /healthz, GET /metrics
    file_store  JSON-file ``CaseStore`` (atomic writes)
    gnn_client  ``Scorer`` over the GNN scoring service (POST /score) with heuristic fallback
    judge       ``Guard`` that adds an independent verifier LLM to the rules guard
    summary     compact, JSON-safe case summaries shared by the API, smoke test and benchmarks
    ui_app      Streamlit entry point (shared ``ui/app.py`` on the case store)
"""
