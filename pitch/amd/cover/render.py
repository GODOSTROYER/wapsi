"""Render the Wapsi cover and social card from cover.html with Playwright.

Run from the repository root:
    uv run --with playwright python -m playwright install chromium   # once
    uv run --with playwright python pitch/amd/cover/render.py
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PAGE = (HERE / "cover.html").as_uri()

# (variant, stage width, stage height, device scale factor, output file)
SHOTS = [
    ("cover", 1920, 1080, 1.0, "wapsi_cover_1920x1080.png"),
    ("cover", 1920, 1080, 2 / 3, "wapsi_cover_1280x720.png"),
    ("social", 1920, 1008, 0.625, "wapsi_social_1200x630.png"),
]


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for variant, w, h, scale, name in SHOTS:
            page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=scale)
            page.goto(f"{PAGE}?v={variant}")
            page.wait_for_timeout(300)
            page.locator("#stage").screenshot(path=str(HERE / name))
            page.close()
            print("wrote", HERE / name)
        browser.close()


if __name__ == "__main__":
    main()
