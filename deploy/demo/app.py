"""The public demo: the shared officer app (``ui/app.py``), edition-neutral, AMD-first.

    uv run streamlit run deploy/demo/app.py

- **Cases.** The two hand-computed showcase cases, plus the synthetic demo world (seed 7) behind the sidebar's Data
  switch. Every browser session works on its own copy, so one visitor's approvals never reach the next; "Start
  over" in the sidebar gives a fresh copy.
- **Model.** With ``WAPSI_LLM_BASE_URL`` / ``WAPSI_LLM_API_KEY`` / ``WAPSI_LLM_MODEL`` set, prose is written live by
  that OpenAI-compatible endpoint (the AMD Token Factory, or your own vLLM on AMD) and a chip says so: "Gemma 4 on AMD
  via <host>". Without them the page says "Recorded run — live AMD endpoint not configured" and replays the
  recorded prose in ``data/recordings/demo_prose.json``. Numbers never come from the model in either mode.
- **Hosting.** ``deploy/DEMO_HOSTING.md``: Hugging Face Spaces (this file is the container's entry point),
  Streamlit Community Cloud (``deploy/streamlit_cloud/streamlit_app.py`` runs this file) and Cloud Run.

Defaults and the demo-world step are in ``deploy/demo/prepare.py``. All data is synthetic.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import prepare  # noqa: E402 - deploy/demo/prepare.py

prepare.add_paths(ROOT)
prepare.apply_defaults()
prepare.ensure_demo_world(ROOT)

runpy.run_path(str(ROOT / "ui" / "app.py"), run_name="__main__")
