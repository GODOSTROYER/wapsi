"""Wapsi AMD edition: edge-aware GraphSAGE mule scorer in pure PyTorch (ROCm-safe, device-agnostic).

Modules: ``graph`` (case subgraphs + world graph), ``model`` (EdgeSAGE, checkpoints), ``train`` (CLI),
``scorer`` (``GnnScorer`` for ``wapsi.classify.classify_trace``), ``service`` (FastAPI ``/score``), ``evaluate``
(heuristic vs XGBoost vs GNN), ``amlworld`` (event-time AMLworld pre-training). See ``README.md``.
"""
