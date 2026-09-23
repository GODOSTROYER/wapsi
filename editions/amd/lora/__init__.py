"""Wapsi AMD edition: LoRA fine-tune of an open model for Wapsi's placeholder-only prose fields.

Pipeline: verifier-filtered distillation (``dataset``) -> LoRA SFT (``train``) -> merge to a plain HF checkpoint
(``merge``) -> vLLM serving -> base-vs-tuned evaluation (``evaluate``). See ``README.md`` in this folder.

Heavy imports (torch, transformers, peft, trl) happen inside functions so that ``dataset`` and ``evaluate``
against HTTP endpoints work without the ML extras.
"""
