"""Render every <section> of slides.html to slides/<id>.png at 1920x1080.

    uv run --with playwright python pitch/amd/video/scripts/render_slides.py
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "slides"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1920, "height": 1080})
        pg.goto((HERE / "slides.html").as_uri())
        pg.wait_for_timeout(500)
        for sid in pg.evaluate("window.SLIDES"):
            pg.evaluate(f"show('{sid}')")
            pg.wait_for_timeout(150)
            pg.screenshot(path=str(OUT / f"{sid}.png"))
            print("slide", sid)
        b.close()


if __name__ == "__main__":
    main()
