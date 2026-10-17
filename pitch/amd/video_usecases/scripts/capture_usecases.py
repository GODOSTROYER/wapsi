"""Extra app screens for the use-case video (the pitch video's shots in pitch/amd/video/shots are reused as is).

Start the app first (recorded run, no live endpoint), as in pitch/amd/video/scripts/capture_ui.py, then:
    uv run --with playwright python pitch/amd/video_usecases/scripts/capture_usecases.py [name ...]
Names: lea, exception, fingerprint, clocks.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "video" / "scripts"))
import capture_ui as cu  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

cu.OUT = HERE.parent / "shots"


def fill(pg, label: str, value: str) -> None:
    pg.get_by_label(label, exact=True).fill(value)
    pg.wait_for_timeout(250)


def main() -> int:
    cu.OUT.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[1:])

    def want(n: str) -> bool:
        return not only or n in only

    with sync_playwright() as p:
        b = p.chromium.launch()
        if want("lea"):
            # a police request with gaps that asks for the whole account: the check limits the hold and drafts a letter
            pg = cu.open_page(b, "hold-plan")
            pg.get_by_text("Check a hold request", exact=True).click()
            pg.wait_for_timeout(800)
            fill(pg, "NCRP acknowledgement no.", "31009260000101")
            fill(pg, "Requesting officer", "Inspector R. Kulkarni")
            fill(pg, "Account number", "HB01-1000000001")
            fill(pg, "Jurisdiction (police station, district, state)", "Cyber PS, Pune City, Maharashtra")
            pg.get_by_label("Amount to hold (₹)").fill("200000")
            pg.keyboard.press("Tab")
            pg.wait_for_timeout(400)
            pg.get_by_text("The request asks for action on the whole account").click()
            pg.wait_for_timeout(400)
            pg.get_by_role("button", name="Check request").click()
            cu.settle(pg, 4000)
            pg.add_style_tag(content=cu.HIDE_CHROME)
            cu.tall_shot(pg, "lea_check", 4200)
            pg.close()
        if want("exception"):
            pg = cu.open_page(b, "hold-plan")
            pg.get_by_text("Request an exceptional account-level hold", exact=True).click()
            pg.wait_for_timeout(800)
            try:
                cu.pick_select(pg, "Recorded reasons (at least one)", "Fraudster")
            except Exception as e:  # noqa: BLE001
                print("reason select failed:", e)
            cu.tall_shot(pg, "exception", 4200)
            pg.close()
        if want("fingerprint"):
            # the supervisor approves the hold plan; the audit log records the plan's fingerprint with the decision
            pg = cu.open_page(b, "approvals", role="supervisor")
            pg.get_by_role("button", name="Approve", exact=True).first.click()
            cu.settle(pg, 4000)
            pg.add_style_tag(content=cu.HIDE_CHROME)
            pg.get_by_text("Audit", exact=True).first.click()
            cu.settle(pg, 4000)
            pg.add_style_tag(content=cu.HIDE_CHROME)
            cu.tall_shot(pg, "audit_fingerprint", 2400)
            pg.close()
        for name, slug in (("clocks", "clocks"),):
            if want(name):
                pg = cu.open_page(b, slug)
                cu.tall_shot(pg, name, 3200)
                pg.close()
        b.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
