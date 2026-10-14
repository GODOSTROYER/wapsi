"""What the public demo needs before its first page: defaults, and the synthetic demo world.

Used by ``deploy/demo/app.py`` (every start) and by the image build (``python deploy/demo/prepare.py``). Nothing
here needs a key or a network: the world is generated from a fixed seed, and all of it is synthetic.

| Variable | Default here | Meaning |
|---|---|---|
| ``WAPSI_UI_LLM`` | ``auto`` | live OpenAI-compatible endpoint when ``WAPSI_LLM_BASE_URL`` / ``WAPSI_LLM_API_KEY`` / ``WAPSI_LLM_MODEL`` are set, else the recorded run, else the offline stub (``ui.data_source.prose_llm``) |
| ``WAPSI_NOW`` | ``2026-09-16T11:00:00+05:30`` | the demo's clock: six days after the showcase fraud, so the SOP and STR clocks show a live case instead of long-expired ones (``""`` = the wall clock) |
| ``WAPSI_DEMO_BUILD_WORLD`` | ``1`` | generate ``data/generated/demo`` (seed 7, 39 cases, about 2 MB, a few seconds) when it is missing, so the sidebar's Data switch offers it. ``0`` = showcase cases only |
| ``WAPSI_UI_MAX_CASES`` | ``12`` (the app's own default) | demo-world cases processed at start-up; lower it on a small host |
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEMO_SEED = 7
DEMO_NOW = "2026-09-16T11:00:00+05:30"
BUILD_WORLD_ENV = "WAPSI_DEMO_BUILD_WORLD"
DEFAULTS = {"WAPSI_UI_LLM": "auto", "WAPSI_NOW": DEMO_NOW}
PRESENT, BUILT, SKIPPED, FAILED = "present", "built", "skipped", "failed"

log = logging.getLogger("wapsi.demo")
_lock = threading.Lock()


def add_paths(root: Path = ROOT) -> None:
    """Make ``ui`` and ``wapsi`` importable from a plain checkout (no install step on Streamlit Community Cloud)."""
    for p in (root, root / "src"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def apply_defaults(env: dict[str, str] | None = None) -> dict[str, str]:
    """Set the demo's defaults where the deployment did not set its own; returns what was set."""
    env = os.environ if env is None else env  # type: ignore[assignment]
    applied = {}
    for key, value in DEFAULTS.items():
        if key not in env:
            env[key] = value
            applied[key] = value
    return applied


def world_dir(root: Path = ROOT) -> Path:
    return root / "data" / "generated" / "demo"


def ensure_demo_world(root: Path = ROOT, *, seed: int = DEMO_SEED, enabled: bool | None = None) -> str:
    """Generate the synthetic demo world when it is missing. Idempotent and safe to call from several sessions at
    once (one lock per process; the world is written to a temporary folder and moved into place). Returns
    ``present`` / ``built`` / ``skipped`` (switched off) / ``failed`` (logged; the app then shows the showcase cases
    only)."""
    out = world_dir(root)
    if (out / "transactions.parquet").exists():
        return PRESENT
    if enabled is None:
        enabled = os.environ.get(BUILD_WORLD_ENV, "1").strip().lower() not in ("0", "false", "no", "off")
    if not enabled:
        return SKIPPED
    with _lock:
        if (out / "transactions.parquet").exists():
            return PRESENT
        tmp = out.with_name(f".demo-{os.getpid()}.tmp")
        try:
            import shutil

            add_paths(root)
            from wapsi.synth.export import write_frames
            from wapsi.synth.golden import write_golden
            from wapsi.synth.world import WorldConfig, build_world

            shutil.rmtree(tmp, ignore_errors=True)
            frames, cases = build_world(WorldConfig(name="demo", size="demo", seed=seed))
            write_frames(frames, tmp)
            write_golden(frames, cases, tmp / "golden", "demo")  # the app reads its case list from here
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(tmp, out)
            except OSError:  # another process finished first
                shutil.rmtree(tmp, ignore_errors=True)
                if not (out / "transactions.parquet").exists():
                    raise
            return BUILT
        except Exception:  # noqa: BLE001 - the demo still works on the showcase cases
            log.exception("could not generate the demo world at %s", out)
            return FAILED


def main(argv: list[str] | None = None) -> int:
    """Image build step: generate the demo world now, so no visitor waits for it."""
    add_paths()
    status = ensure_demo_world(enabled=True)
    print(f"demo world: {status} ({world_dir()})")
    return 1 if status == FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
