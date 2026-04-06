from __future__ import annotations

import shutil
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


APP_URL = "http://127.0.0.1:8787/"
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "demo-output"
RAW_DIR = OUTPUT_DIR / "raw-videos"
VIEWPORT = {"width": 1440, "height": 1200}
VIDEO_SIZE = {"width": 1440, "height": 1200}
DEMO_TWEET_URL = "https://x.com/kimmonismus/status/2041267465355919603"
DEMO_REPLY_GUIDANCE = (
    "Keep it concise and analytical. Focus on the builder takeaway and avoid hype."
)
DEMO_ORIGINAL_PROMPT = (
    "Write a standalone post about why model pricing shifts matter less than workflow "
    "design when teams choose AI tools."
)


def slow_scroll(page: Page, start: int, end: int, step: int = 60, delay: float = 0.06) -> None:
    direction = 1 if end >= start else -1
    step = abs(step) * direction
    position = start
    page.evaluate("(y) => window.scrollTo(0, y)", start)
    while (direction > 0 and position < end) or (direction < 0 and position > end):
        position += step
        if direction > 0:
            position = min(position, end)
        else:
            position = max(position, end)
        page.evaluate("(y) => window.scrollTo(0, y)", position)
        page.wait_for_timeout(int(delay * 1000))


def goto_dashboard(page: Page) -> None:
    page.goto(APP_URL, wait_until="networkidle")
    page.wait_for_timeout(1200)


def prepare_output_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)


def save_recording(page: Page, target_name: str) -> None:
    video = page.video
    if video is None:
        raise RuntimeError("Video recording was not created.")
    temp_path = Path(video.path())
    target_path = RAW_DIR / target_name
    if target_path.exists():
        target_path.unlink()
    shutil.move(str(temp_path), str(target_path))


def record_flow(browser, name: str, flow) -> Path:
    context = browser.new_context(
        viewport=VIEWPORT,
        record_video_dir=str(RAW_DIR),
        record_video_size=VIDEO_SIZE,
        color_scheme="dark",
    )
    page = context.new_page()
    goto_dashboard(page)
    flow(page)
    page.wait_for_timeout(1200)
    context.close()
    save_recording(page, f"{name}.webm")
    return RAW_DIR / f"{name}.webm"


def flow_full_page_scroll(page: Page) -> None:
    page.wait_for_timeout(800)
    max_scroll = page.evaluate(
        "() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight) - window.innerHeight"
    )
    slow_scroll(page, 0, int(max_scroll), step=36, delay=0.05)
    page.wait_for_timeout(800)


def flow_generate_tweet(page: Page) -> None:
    summary = page.locator("summary").filter(has_text="Direct Reply")
    summary.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    summary.click()
    page.wait_for_timeout(700)
    page.get_by_label("Tweet Link").click()
    page.get_by_label("Tweet Link").press_sequentially(DEMO_TWEET_URL, delay=35)
    page.wait_for_timeout(300)
    guidance = page.get_by_label("Draft Brief").first
    guidance.click()
    guidance.press_sequentially(DEMO_REPLY_GUIDANCE, delay=18)
    page.wait_for_timeout(500)
    page.get_by_role("button", name="Draft Reply").first.click(no_wait_after=True)
    page.wait_for_timeout(4500)


def flow_original_prompt(page: Page) -> None:
    section = page.locator("#latest-drafts").locator("xpath=preceding-sibling::section[1]")
    section.scroll_into_view_if_needed()
    page.wait_for_timeout(700)
    topic_input = page.locator("textarea[name='topic']")
    topic_input.click()
    topic_input.press_sequentially(DEMO_ORIGINAL_PROMPT, delay=16)
    page.wait_for_timeout(500)
    page.get_by_role("button", name="Generate Selected Drafts").click(no_wait_after=True)
    page.wait_for_timeout(5000)


def flow_adaptive_voice(page: Page) -> None:
    section = page.locator("#voice-review")
    section.scroll_into_view_if_needed()
    page.wait_for_timeout(1000)
    details = page.locator("#voice-review details").filter(has_text="Raw diff")
    if details.count():
        details.locator("summary").click()
        page.wait_for_timeout(700)
    top = page.evaluate(
        """() => {
            const el = document.querySelector('#voice-review');
            if (!el) return 0;
            const rect = el.getBoundingClientRect();
            return Math.max(0, window.scrollY + rect.top - 80);
        }"""
    )
    bottom = page.evaluate(
        """() => {
            const el = document.querySelector('#archive-voice');
            if (!el) return 0;
            const rect = el.getBoundingClientRect();
            return Math.max(0, window.scrollY + rect.bottom - window.innerHeight + 120);
        }"""
    )
    slow_scroll(page, int(top), int(bottom), step=30, delay=0.06)
    page.wait_for_timeout(900)


def main() -> None:
    prepare_output_dirs()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            flows = [
                ("full-page-scroll", flow_full_page_scroll),
                ("generate-tweet", flow_generate_tweet),
                ("original-prompt", flow_original_prompt),
                ("adaptive-voice", flow_adaptive_voice),
            ]
            for name, flow in flows:
                path = record_flow(browser, name, flow)
                print(path)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
