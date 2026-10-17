"""Render the YouTube thumbnail (1280x720) from thumbnail.html: uv run --with playwright python pitch/amd/cover/render_thumbnail.py"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=2 / 3)
        for src, name in (("thumbnail.html", "wapsi_youtube_thumbnail_1280x720.png"),
                          ("thumbnail_usecases.html", "wapsi_usecases_thumbnail_1280x720.png")):
            page.goto((HERE / src).as_uri())
            page.wait_for_timeout(800)
            out = HERE / name
            page.locator("#stage").screenshot(path=str(out))
            print("wrote", out)
        browser.close()


if __name__ == "__main__":
    main()
