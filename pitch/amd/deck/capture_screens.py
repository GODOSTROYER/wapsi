"""Capture Streamlit screens for the deck (slide 4).

Start the app first, in recorded mode (no live endpoint), from the repo root:
    WAPSI_UI_LLM=auto uv run streamlit run ui/app.py --server.headless true --server.port 8601
Then:
    uv run --with playwright python pitch/amd/deck/capture_screens.py
Stop the server afterwards. Screens land in pitch/amd/deck/screens/.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8601"
OUT = Path(__file__).resolve().parent / "screens"
OUT.mkdir(exist_ok=True)

SHOTS = [
    ("inbox", "inbox", None),
    ("case_room", "case-room", None),
    ("agent_trace", "case-room", "Agent trace"),
    ("hold_plan", "hold-plan", None),
    ("drafts", "drafts-verification", None),
    ("voice_intake", "voice-intake", None),
]


def settle(page) -> None:
    page.wait_for_selector("[data-testid='stApp']", timeout=60_000)
    time.sleep(2)
    for _ in range(120):  # wait for Streamlit to finish running the script
        if page.locator("[data-testid='stStatusWidget']").count() == 0:
            break
        time.sleep(0.5)
    time.sleep(2.5)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, slug, tab in SHOTS:
            ctx = browser.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=1,
                                      color_scheme="light")
            page = ctx.new_page()
            page.goto(f"{BASE}/?page={slug}&role=analyst", wait_until="domcontentloaded")
            settle(page)
            if tab:
                loc = page.get_by_role("tab", name=tab)
                if loc.count() == 0:
                    loc = page.locator("button[role='tab']", has_text=tab)
                if loc.count():
                    loc.first.click()
                    settle(page)
            page.screenshot(path=str(OUT / f"{name}.png"))
            print("saved", OUT / f"{name}.png")
            ctx.close()
        browser.close()


if __name__ == "__main__":
    main()
