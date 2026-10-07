"""Capture the officer-guide screenshots (docs/assets/screenshots/) by driving the Streamlit app with Playwright.

The script starts its own headless Streamlit server, walks the showcase case through every screen in one browser
session (so the supervisor's approvals are still there when the Clocks and Audit screens are captured), saves
1440x900 PNGs, and stops the server again.

    uv run --with playwright python -m playwright install chromium     # once (skip if Chrome or Edge is installed)
    uv run --with playwright python scripts/capture_screenshots.py
    uv run --with playwright python scripts/capture_screenshots.py --now 2026-09-23T18:00:00+05:30

Options: ``--port`` (default 8799), ``--out`` (default docs/assets/screenshots), ``--case`` (default the
single-victim showcase case; for another case the innocent-holder shot, 04, is skipped), ``--now`` (sets WAPSI_NOW
for the server, freezing the clock the UI judges deadlines against; the engine still stamps real times),
``--headed`` (watch the browser), ``--reuse`` (use a server already running on the port).

Browser: Playwright's bundled Chromium is tried first; if it is missing or a different build, the installed
Google Chrome and then Microsoft Edge are used. All data shown is synthetic.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

try:
    from playwright.sync_api import Browser, Locator, Page, Playwright, sync_playwright
    from playwright.sync_api import Error as PlaywrightError
except ImportError:  # pragma: no cover - explained to the user
    sys.exit(
        "Playwright is not installed. Run: uv run --with playwright python scripts/capture_screenshots.py"
    )

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "docs" / "assets" / "screenshots"
SHOWCASE_CASE = "CASE-31009260000101"  # Showcase 1: Meera Joshi, ₹4,80,000 (src/wapsi/fixtures/showcase.py)
TWO_VICTIM_CASE = "CASE-31109260000102"  # Showcase 2: two victims, one mule account, Process 2
INNOCENT_HOLDER = "HB01-2000000001"  # the landlord who received ₹5,000 rent (SOP Para 5(xii))
VIEWPORT = {"width": 1440, "height": 900}

# True while Streamlit is still running the script or a spinner / stale element is on screen.
_BUSY_JS = """() => !!(document.querySelector('[data-testid="stStatusWidget"]')
  || document.querySelector('[data-testid="stSpinner"]')
  || document.querySelector('[data-stale="true"]')
  || document.querySelector('[data-testid="stAppSkeleton"]'))"""

# Scroll ``el`` to the top of its scrolling container, leaving ``pad`` pixels of context above it.
_SCROLL_JS = """(el, pad) => {
  el.scrollIntoView({block: 'start'});
  let p = el.parentElement;
  while (p && !(p.scrollHeight > p.clientHeight && getComputedStyle(p).overflowY !== 'visible')) p = p.parentElement;
  (p || document.scrollingElement).scrollBy(0, -pad);
}"""

_SCROLL_TOP_JS = """() => document.querySelectorAll('*').forEach(e => {
  if (e.scrollTop > 0 && getComputedStyle(e).overflowY !== 'visible') e.scrollTop = 0; })"""


# --------------------------------------------------------------------------- server


def healthy(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/_stcore/health", timeout=2) as r:
            return r.status == 200 and r.read().strip() == b"ok"
    except (urllib.error.URLError, OSError):
        return False


def start_server(port: int, now: str | None) -> subprocess.Popen:
    env = dict(os.environ, WAPSI_UI_SOURCE="pipeline", PYTHONIOENCODING="utf-8")
    env.pop("WAPSI_UI_RAISE", None)
    env.pop("WAPSI_EVAL_REPORT", None)
    if now:
        env["WAPSI_NOW"] = now
    else:
        env.pop("WAPSI_NOW", None)
    cmd = [
        "uv",
        "run",
        "streamlit",
        "run",
        "ui/app.py",
        "--server.headless",
        "true",
        "--server.port",
        str(port),
        "--server.fileWatcherType",
        "none",
        "--browser.gatherUsageStats",
        "false",
    ]
    kwargs: dict = {"cwd": ROOT, "env": env, "stdout": subprocess.DEVNULL, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    print("Starting:", " ".join(cmd))
    return subprocess.Popen(cmd, **kwargs)


def stop_server(proc: subprocess.Popen) -> None:
    """Stop ``uv run streamlit`` and the Python process it spawned."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_healthy(base: str, proc: subprocess.Popen | None, timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if healthy(base):
            return
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"Streamlit exited early with code {proc.returncode}")
        time.sleep(1)
    raise TimeoutError(f"Streamlit did not become healthy at {base} within {timeout:.0f}s")


# --------------------------------------------------------------------------- browser helpers


def launch(pw: Playwright, headed: bool) -> Browser:
    attempts: list[tuple[str, dict]] = [
        ("bundled Chromium", {}),
        ("Google Chrome", {"channel": "chrome"}),
        ("Microsoft Edge", {"channel": "msedge"}),
    ]
    errors = []
    for label, kw in attempts:
        try:
            browser = pw.chromium.launch(headless=not headed, **kw)
        except PlaywrightError as exc:
            errors.append(f"{label}: {str(exc).splitlines()[0]}")
            continue
        print(f"Browser: {label} {browser.version}")
        return browser
    raise RuntimeError(
        "No browser could be launched.\n  "
        + "\n  ".join(errors)
        + "\nInstall one with: uv run --with playwright python -m playwright install chromium"
    )


def settle(page: Page, *, expect: str | None = None, timeout: float = 180.0, quiet: float = 1.0) -> None:
    """Wait until the app has finished rerunning and charts have drawn."""
    deadline = time.monotonic() + timeout
    page.wait_for_timeout(300)
    if expect:
        page.get_by_text(expect, exact=False).first.wait_for(state="visible", timeout=timeout * 1000)
    idle_since = None
    while time.monotonic() < deadline:
        if page.evaluate(_BUSY_JS):
            idle_since = None
        elif idle_since is None:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since >= quiet:
            break
        page.wait_for_timeout(200)
    else:
        raise TimeoutError("The app kept running for too long")
    for selector in ('[data-testid="stGraphVizChart"]', ".js-plotly-plot"):
        if page.locator(selector).count():
            inner = "svg" if "GraphViz" in selector else ".main-svg"
            page.locator(f"{selector} {inner}").first.wait_for(state="visible", timeout=30_000)
    page.wait_for_timeout(600)  # let fonts, transitions and chart animations finish


def keyed(page: Page, key: str) -> Locator:
    """The element container Streamlit renders for the widget with ``key`` (class ``st-key-<key>``)."""
    return page.locator(f".st-key-{key}")


def go_to(page: Page, label: str, expect: str) -> None:
    """Pick a page in the sidebar 'Go to' list (labels may carry counts, e.g. 'Approvals · 17 pending')."""
    keyed(page, "page").locator("label").filter(has_text=re.compile(rf"^\s*{re.escape(label)}")).first.click()
    settle(page, expect=expect)
    page.evaluate(_SCROLL_TOP_JS)


def choose(page: Page, key: str, option: str | re.Pattern[str]) -> None:
    """Open the selectbox with ``key`` and click the option whose text matches.

    Streamlit >= 1.60 renders a combobox with an 'Open' button; older releases a BaseWeb select."""
    box = keyed(page, key)
    opener = box.locator('button[aria-label="Open"], [data-baseweb="select"]').first
    opener.click()
    page.get_by_role("option").filter(has_text=option).first.click()
    settle(page)


def switch_role(page: Page, title: str) -> None:
    choose(page, "role", re.compile(rf"^{re.escape(title)}$"))


def scroll_to(page: Page, text: str, pad: int = 16) -> None:
    """Scroll the first visible element whose text is exactly ``text`` near the top of the viewport."""
    page.get_by_text(text, exact=True).filter(visible=True).first.evaluate(_SCROLL_JS, pad)
    page.wait_for_timeout(500)


# --------------------------------------------------------------------------- the walk-through


def _display(path: Path) -> str:
    """Repo-relative path for messages; the full path when ``--out`` points outside the repository."""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def capture(page: Page, base: str, case_id: str, out: Path) -> list[Path]:
    saved: list[Path] = []

    def shot(name: str, prepare: Callable[[], None] | None = None) -> None:
        if prepare is not None:
            prepare()
        path = out / f"{name}.png"
        page.screenshot(path=str(path))
        saved.append(path)
        print(f"  saved {_display(path)}")

    # Analyst (maker): inbox -> case room -> hold plan -> drafts
    page.goto(f"{base}/?page=inbox&case={case_id}&role=analyst")
    settle(page, expect="Hold notices waiting for you")
    shot("01-inbox-analyst")

    keyed(page, f"open_{case_id}").locator("button").first.click()
    settle(page, expect="Money trail")
    shot("02-case-room-trail-analyst")
    shot("03-case-room-inspector-analyst", lambda: scroll_to(page, "Account inspector"))

    if case_id == SHOWCASE_CASE:  # the landlord is only on the single-victim showcase trail
        choose(page, "inspect_account", INNOCENT_HOLDER)
        shot(
            "04-case-room-innocent-holder-analyst",
            lambda: scroll_to(page, "Victim money now, per complaint", 190),
        )
    else:
        print(
            f"  skipped 04-case-room-innocent-holder-analyst ({INNOCENT_HOLDER} is not on {case_id}'s trail)"
        )

    go_to(page, "Hold Plan", "Hold only the victim's money")
    shot("05-hold-plan-analyst")
    shot("06-hold-plan-table-analyst", lambda: scroll_to(page, "Recommendations"))

    go_to(page, "Drafts & Verification", "Review each draft after its checks")
    shot("07-drafts-verification-analyst")
    shot("08-drafts-hold-instruction-analyst", lambda: scroll_to(page, "Hold to be placed", 24))
    page.get_by_role("tab", name=re.compile(r"^STR")).first.click()
    settle(page)
    page.evaluate(_SCROLL_TOP_JS)
    shot("09-drafts-str-analyst")

    # Supervisor (checker): approve everything that passed its checks, then see actions and clocks
    switch_role(page, "Supervisor")
    go_to(page, "Approvals", "Approve, return or reject each item")
    shot("10-approvals-supervisor")

    keyed(page, "bulk_confirm").locator("label").first.click()
    settle(page)
    keyed(page, "bulk_approve").locator("button").first.click()
    settle(page, expect="Each decision is recorded in the audit trail")
    page.evaluate(_SCROLL_TOP_JS)
    shot("11-approvals-approved-supervisor")
    shot("12-approvals-actions-supervisor", lambda: scroll_to(page, "Actions queued by approvals"))

    go_to(page, "Clocks", "Deadlines that start once holds are placed")
    shot("13-clocks-supervisor")
    shot("14-clocks-table-supervisor", lambda: scroll_to(page, "Due or lapsing in the next 15 days"))

    go_to(page, "KPIs", "How Wapsi scores on the golden set")
    shot("15-kpis")

    # Auditor: read-only, personal details masked
    switch_role(page, "Auditor")
    go_to(page, "Case Room", "Money trail")
    shot("16-case-room-auditor-masked")
    go_to(page, "Audit", "Every view, decision and role change")
    shot("17-audit-auditor-masked")

    # Deep link (a fresh session): the two-victim showcase case, Process 2 with pro-rata shares and an s.106 flag
    page.goto(f"{base}/?page=hold-plan&case={TWO_VICTIM_CASE}&role=analyst")
    settle(page, expect="Hold only the victim's money")
    shot(
        "18-hold-plan-two-victims-analyst",
        lambda: scroll_to(page, "Pro-rata share per victim (SOP Para 5(xiii))", 150),
    )
    return saved


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8799, help="Streamlit server port (default 8799)")
    ap.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="output folder (default docs/assets/screenshots)"
    )
    ap.add_argument("--case", default=SHOWCASE_CASE, help=f"case to walk through (default {SHOWCASE_CASE})")
    ap.add_argument("--now", default=None, help="ISO 8601 time for WAPSI_NOW (default: the real clock)")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--reuse", action="store_true", help="use a Streamlit server already running on --port")
    args = ap.parse_args(argv)

    base = f"http://localhost:{args.port}"
    out = args.out if args.out.is_absolute() else (Path.cwd() / args.out)
    out.mkdir(parents=True, exist_ok=True)

    proc = None
    if healthy(base):
        if not args.reuse:
            print(f"Port {args.port} is already serving an app. Stop it, pick --port, or pass --reuse.")
            return 2
        print(f"Reusing the server at {base}")
    else:
        proc = start_server(args.port, args.now)
    try:
        wait_healthy(base, proc)
        with sync_playwright() as pw:
            browser = launch(pw, args.headed)
            try:
                context = browser.new_context(
                    viewport=VIEWPORT,
                    device_scale_factor=1,
                    color_scheme="light",
                    locale="en-IN",
                    timezone_id="Asia/Kolkata",
                )
                page = context.new_page()
                page.set_default_timeout(60_000)
                saved = capture(page, base, args.case, out)
            finally:
                browser.close()
    finally:
        if proc is not None:
            stop_server(proc)
            print("Server stopped.")
    print(f"{len(saved)} screenshots in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
