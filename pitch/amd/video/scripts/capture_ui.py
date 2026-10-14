"""Capture the Wapsi officer app (recorded/offline mode) as tall 1920-wide PNGs for the demo video.

Start the app first (recorded run, no live endpoint):
    WAPSI_UI_LLM=auto uv run streamlit run ui/app.py --server.headless true --server.port 8602
Then:
    uv run --with playwright python pitch/amd/video/scripts/capture_ui.py
Each shot is the full page height so the video can scroll through it (see build_video.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE = "http://localhost:8602/"
CASE = "CASE-31009260000101"
OUT = Path(__file__).resolve().parents[1] / "shots"
W = 1920

HIDE_CHROME = """
[data-testid=stHeader], [data-testid=stToolbar], [data-testid=stDecoration], [data-testid=stStatusWidget] {display:none !important;}
"""


def settle(pg: Page, ms: int = 2500) -> None:
    pg.wait_for_timeout(1200)
    try:
        pg.wait_for_selector("[data-testid=stStatusWidget]", state="detached", timeout=60000)
    except Exception:  # noqa: BLE001
        pass
    pg.wait_for_timeout(ms)


def open_page(browser, slug: str, role: str = "analyst", height: int = 1080) -> Page:
    pg = browser.new_page(viewport={"width": W, "height": height}, device_scale_factor=1)
    pg.goto(f"{BASE}?page={slug}&case={CASE}&role={role}")
    pg.wait_for_selector("[data-testid=stMain]", timeout=90000)
    settle(pg, 6000)
    pg.add_style_tag(content=HIDE_CHROME)
    return pg


def tall_shot(pg: Page, name: str, max_h: int = 4200) -> None:
    h = pg.evaluate(
        "() => Math.max(...[...document.querySelectorAll('[data-testid=stMain], [data-testid=stMainBlockContainer], section, body')]"
        ".map(e => e.scrollHeight))"
    )
    h = int(min(max(h, 1080), max_h))
    pg.set_viewport_size({"width": W, "height": h})
    settle(pg, 2500)
    pg.screenshot(path=str(OUT / f"{name}.png"))
    print(f"{name}.png  {W}x{h}")
    pg.set_viewport_size({"width": W, "height": 1080})


def pick_select(pg: Page, label: str, text: str) -> None:
    box = pg.get_by_role("combobox", name=label)
    box.click()
    pg.wait_for_timeout(600)
    pg.keyboard.type(text, delay=40)
    pg.wait_for_timeout(800)
    pg.keyboard.press("Enter")
    settle(pg, 2500)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[1:])

    def want(n: str) -> bool:
        return not only or n in only

    with sync_playwright() as p:
        b = p.chromium.launch()
        if want("inbox"):
            pg = open_page(b, "inbox")
            tall_shot(pg, "inbox", 1400)
            pg.close()
        if want("case"):
            pg = open_page(b, "case-room")
            tall_shot(pg, "case_trail")
            try:
                pick_select(pg, "Inspect account", "HB01-2000000001")
                tall_shot(pg, "case_landlord")
            except Exception as e:  # noqa: BLE001
                print("landlord inspector failed:", e)
            try:
                pg.get_by_role("tab").filter(has_text="Agent trace").first.click()
                settle(pg, 3000)
                tall_shot(pg, "agent_trace")
            except Exception as e:  # noqa: BLE001
                print("agent trace failed:", e)
            pg.close()
        if want("hold"):
            pg = open_page(b, "hold-plan")
            tall_shot(pg, "hold_plan")
            pg.close()
        if want("drafts"):
            pg = open_page(b, "drafts-verification")
            tall_shot(pg, "drafts_hold")
            try:
                pg.get_by_text("STR", exact=True).first.click()
                settle(pg, 3000)
                tall_shot(pg, "drafts_str")
            except Exception as e:  # noqa: BLE001
                print("STR tab failed:", e)
            pg.close()
        if want("voice"):
            pg = open_page(b, "voice-intake")
            try:
                pick_select(pg, "Sample voice note", "Fatima")
            except Exception as e:  # noqa: BLE001
                print("sample select failed:", e)
            tall_shot(pg, "voice_pick", 2000)
            try:
                pg.get_by_role("button", name="Transcribe and check").click()
                settle(pg, 8000)
            except Exception as e:  # noqa: BLE001
                print("transcribe failed:", e)
            tall_shot(pg, "voice_result")
            pg.close()
        if want("approvals"):
            pg = open_page(b, "approvals", role="analyst")
            tall_shot(pg, "approvals_analyst", 2400)
            pg.close()
            pg = open_page(b, "approvals", role="supervisor")
            tall_shot(pg, "approvals_supervisor", 2400)
            pg.close()
        if want("audit"):
            pg = open_page(b, "audit")
            try:
                pg.get_by_role("button", name="Verify chain").click()
                settle(pg, 3000)
            except Exception as e:  # noqa: BLE001
                print("verify chain failed:", e)
            tall_shot(pg, "audit", 2400)
            pg.close()
        b.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
