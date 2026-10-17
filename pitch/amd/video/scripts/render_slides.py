"""Render every <section> of slides.html to slides/<id>.png at 1920x1080.

    uv run --with playwright python pitch/amd/video/scripts/render_slides.py [slides.html] [out dir]

Defaults: this folder's slides.html into pitch/amd/video/slides (another video passes its own, e.g.
pitch/amd/video_usecases/slides_usecases.html pitch/amd/video_usecases/slides).
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
HTML = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "slides.html"
OUT = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else HERE.parent / "slides"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1920, "height": 1080})
        pg.goto(HTML.as_uri())
        pg.wait_for_timeout(500)
        for sid in pg.evaluate("window.SLIDES"):
            pg.evaluate(f"show('{sid}')")
            pg.wait_for_timeout(150)
            pg.screenshot(path=str(OUT / f"{sid}.png"))
            print("slide", sid)
        b.close()


if __name__ == "__main__":
    main()
