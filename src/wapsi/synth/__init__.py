"""Synthetic Indian banking world, independent ground truth and golden set.

- ``generate_world(WorldConfig(size="demo", seed=7))`` -> dict of DataFrames (docs/data-contract.md)
- ``wapsi.synth.export``: Parquet layout (``write_frames``, ``load_frames``, ``export_ledger``)
- ``wapsi.synth.reference``: exact pro-rata taint + AGENTS.md disposition rules (ground truth)
- ``wapsi.synth.golden``: ``GoldenCase`` JSONL + SUMMARY.md (see data/golden/README.md)
- CLI: ``uv run python -m wapsi.synth --size demo --seed 7 --out data/generated/demo``
"""

from wapsi.synth.world import WorldConfig, build_world, generate_world

__all__ = ["WorldConfig", "build_world", "generate_world"]
