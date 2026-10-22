"""Portfolio images for QuoteDesk: cover + screens of the running app.

Needs the API and UI running with processed quotes: samples r07 (Q-2026-00001, approved after
review), r12 (Q-2026-00002), r09 (Q-2026-00003) and r20 (Q-2026-00004, approved after review).

Run:  uv run --with playwright python docs/portfolio/shots.py
"""

from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "portfolio"
URL = "http://localhost:8501"
W, H = 1568, 698  # same size as the other portfolio screenshots

HIDE = """
[data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"],
[data-testid="stHeader"] {display:none !important;}
"""


def settle(page: Page, ms: int = 2500) -> None:
    page.wait_for_timeout(ms)
    page.add_style_tag(content=HIDE)


def show(page: Page, status: str, ref: str) -> None:
    page.get_by_role("tab", name="Review").click()
    page.get_by_test_id("stButtonGroup").get_by_text(status, exact=True).click()
    settle(page, 1500)
    box = page.get_by_role("combobox", name="Quote", exact=True)
    box.click()
    box.fill(ref)
    page.keyboard.press("Enter")
    settle(page)


def scroll_to(page: Page, text: str, offset: int = 0) -> None:
    page.get_by_text(text).locator("visible=true").first.scroll_into_view_if_needed()
    page.mouse.move(900, 400)
    page.mouse.wheel(0, offset)
    page.wait_for_timeout(700)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")

        cover = browser.new_page(viewport={"width": 1000, "height": 800})
        cover.goto((OUT / "cover.html").as_uri())
        cover.locator(".card").screenshot(path=str(OUT / "00-cover.png"))

        page = browser.new_context(viewport={"width": W, "height": H}, color_scheme="dark").new_page()
        page.goto(URL)
        page.wait_for_selector("text=QuoteDesk", timeout=60_000)
        settle(page, 4000)

        # 01: the drafted quote for the injection email: flagged, prices unchanged
        show(page, "needs review", "Q-2026-00002")
        page.screenshot(path=str(OUT / "01-injection-flagged.png"))

        # 02: a request with vague and unstocked items: what the reviewer sees per line
        show(page, "needs review", "Q-2026-00003")
        page.get_by_test_id("stExpander").filter(has_text="Ball valves").scroll_into_view_if_needed()
        page.mouse.move(900, 400)
        page.mouse.wheel(0, -110)
        page.wait_for_timeout(700)
        page.screenshot(path=str(OUT / "02-lines-and-flags.png"))

        # 03: a phone photo of a list, and the email it came with
        show(page, "all", "Q-2026-00004")
        page.get_by_role("tab", name="Original email").click()
        settle(page)
        page.get_by_test_id("stImage").locator("visible=true").first.scroll_into_view_if_needed()
        page.mouse.move(900, 400)
        page.mouse.wheel(0, -150)
        page.wait_for_timeout(700)
        page.screenshot(path=str(OUT / "03-original-email.png"))

        # 04: an approved quote: the email text and the PDF that went to the outbox
        show(page, "sent", "Q-2026-00004")
        page.get_by_role("tab", name="Quote PDF").click()
        settle(page, 3000)
        scroll_to(page, "Download PDF", 430)
        page.screenshot(path=str(OUT / "04-quote-pdf.png"))
        browser.close()
    print("saved to", OUT)


if __name__ == "__main__":
    main()
