"""Render deck.html to WAPSI_DECK.pdf (one 1920x1080 page per slide) and PNG previews.

    uv run --with playwright python pitch/amd/deck/build_deck.py            # PDF + previews/slide-NN.png
    uv run --with playwright python pitch/amd/deck/build_deck.py --no-png   # PDF only

First time only: uv run --with playwright python -m playwright install chromium
Google Fonts load when online; offline the deck falls back to Georgia / Segoe UI / Consolas.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
SRC = HERE / "deck.html"
PDF = HERE / "WAPSI_DECK.pdf"
PREVIEWS = HERE / "previews"


def main() -> None:
    want_png = "--no-png" not in sys.argv
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(SRC.as_uri(), wait_until="networkidle", timeout=60_000)
        page.evaluate("document.fonts.ready")
        slides = page.locator("section.slide")
        n = slides.count()
        # overflow check: content must not spill past its 1920x1080 frame
        over = page.evaluate("""() => [...document.querySelectorAll('section.slide')].map((s, i) => {
            const r = s.getBoundingClientRect(); let bad = [];
            s.querySelectorAll('*').forEach(e => { const b = e.getBoundingClientRect();
              if (b.width && (b.bottom > r.bottom + 1 || b.right > r.right + 1)) bad.push(e.tagName + '.' + e.className); });
            return bad.length ? `slide ${i + 1}: ${bad.slice(0, 4).join(', ')}` : null; }).filter(Boolean)""")
        for line in over:
            print("WARN overflow", line)
        if want_png:
            PREVIEWS.mkdir(exist_ok=True)
            for i in range(n):
                slides.nth(i).screenshot(path=str(PREVIEWS / f"slide-{i + 1:02d}.png"))
        page.emulate_media(media="print")
        page.pdf(path=str(PDF), width="1920px", height="1080px", print_background=True,
                 margin={"top": "0", "right": "0", "bottom": "0", "left": "0"}, prefer_css_page_size=True)
        browser.close()
    print(f"{n} slides -> {PDF}")


if __name__ == "__main__":
    main()
