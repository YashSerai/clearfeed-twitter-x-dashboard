from __future__ import annotations

from pathlib import Path
import re

from playwright.sync_api import sync_playwright


EXTRACT_ARTICLE_JS = r"""
() => {
  const root = document.querySelector('article, main, [role="main"]') || document.body;
  const parts = [];
  for (const node of root.querySelectorAll('h1,h2,h3,p,li,blockquote,pre,code')) {
    const text = (node.innerText || node.textContent || '').trim();
    if (!text) continue;
    parts.push(text);
  }
  return parts.join('\n\n');
}
"""

EXTRACT_TWEET_CONTEXT_JS = r"""
() => {
  const rows = [];
  for (const article of document.querySelectorAll('article[data-testid="tweet"]')) {
    const timeEl = article.querySelector('time');
    const tweetAnchor = timeEl ? timeEl.closest('a[href*="/status/"]') : article.querySelector('a[href*="/status/"]');
    const href = tweetAnchor ? tweetAnchor.getAttribute('href') : null;
    const userNameRoot = article.querySelector('div[data-testid="User-Name"]');
    const nameSpans = userNameRoot ? Array.from(userNameRoot.querySelectorAll('span')).map((node) => (node.innerText || '').trim()).filter(Boolean) : [];
    const textRoot = article.querySelector('div[data-testid="tweetText"]');
    const text = textRoot ? textRoot.innerText.trim() : '';
    if (!href && !text) continue;
    rows.push({
      href,
      author: nameSpans.join(' | '),
      text,
      datetime: timeEl ? timeEl.getAttribute('datetime') : null
    });
  }
  return rows.slice(0, 6);
}
"""


class ArticleExpander:
    def __init__(self, char_limit: int, storage_state_path: Path, headless: bool):
        self.char_limit = char_limit
        self.storage_state_path = storage_state_path
        self.headless = headless

    def expand(self, url: str) -> str:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.headless)
            context = browser.new_context(storage_state=str(self.storage_state_path))
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1500)
                text = page.evaluate(EXTRACT_ARTICLE_JS)
            finally:
                page.close()
                context.close()
                browser.close()
        return text[: self.char_limit].strip()

    def expand_tweet_context(self, url: str) -> str:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.headless)
            context = browser.new_context(storage_state=str(self.storage_state_path))
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1800)
                self._expand_conversation(page)
                rows = page.evaluate(EXTRACT_TWEET_CONTEXT_JS)
            finally:
                page.close()
                context.close()
                browser.close()
        parts: list[str] = []
        for index, row in enumerate(rows, start=1):
            author = (row.get("author") or "").strip()
            text = (row.get("text") or "").strip()
            href = (row.get("href") or "").strip()
            when = (row.get("datetime") or "").strip()
            if not (author or text or href):
                continue
            parts.append(
                f"Tweet context {index}\n"
                f"author: {author or 'unknown'}\n"
                f"time: {when or 'unknown'}\n"
                f"url: {href or 'unknown'}\n"
                f"text: {text or '[no text]'}"
            )
        joined = "\n\n".join(parts)
        return joined[: self.char_limit].strip()

    def _expand_conversation(self, page) -> None:
        # X often lazy-loads parent tweets / collapsed replies only after small scrolls and explicit clicks.
        for _ in range(3):
            page.mouse.wheel(0, -1200)
            page.wait_for_timeout(900)
        for _ in range(4):
            clicked = self._click_visible_conversation_controls(page)
            page.mouse.wheel(0, 1600)
            page.wait_for_timeout(900)
            page.mouse.wheel(0, -600)
            page.wait_for_timeout(700)
            if not clicked:
                continue
            page.wait_for_timeout(1200)

    def _click_visible_conversation_controls(self, page) -> bool:
        patterns = [
            re.compile(r"show more repl", re.I),
            re.compile(r"show additional repl", re.I),
            re.compile(r"show this thread", re.I),
            re.compile(r"show replies", re.I),
            re.compile(r"more repl", re.I),
        ]
        clicked_any = False
        for pattern in patterns:
            locator = page.get_by_text(pattern)
            count = min(locator.count(), 5)
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if not candidate.is_visible(timeout=500):
                        continue
                    candidate.click(timeout=2000)
                    page.wait_for_timeout(800)
                    clicked_any = True
                except Exception:
                    continue
        return clicked_any
