from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from dateutil import parser as date_parser
from playwright.sync_api import BrowserContext, sync_playwright

from .config import AppConfig
from .types import ScrapedPost, SourceConfig

TWEET_URL_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:mobile\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})/status/(\d+)",
    re.IGNORECASE,
)

EXTRACT_TWEETS_JS = r"""
(options) => {
  const opts = options || {};
  const limit = Number.isFinite(opts.limit) ? opts.limit : 80;
  const parseCount = (value) => {
    if (!value) return 0;
    const cleaned = value.replace(/,/g, '').trim().toLowerCase();
    const match = cleaned.match(/([\d.]+)([km])?/);
    if (!match) return 0;
    const num = parseFloat(match[1]);
    if (Number.isNaN(num)) return 0;
    if (match[2] === 'k') return Math.round(num * 1000);
    if (match[2] === 'm') return Math.round(num * 1000000);
    return Math.round(num);
  };

  const readMetric = (article, selectors) => {
    for (const selector of selectors) {
      const button = article.querySelector(selector);
      if (!button) continue;
      const text = (button.innerText || button.textContent || '').trim();
      const value = parseCount(text);
      if (value) return value;
      const aria = (button.getAttribute('aria-label') || '').trim();
      const ariaValue = parseCount(aria);
      if (ariaValue) return ariaValue;
    }
    return 0;
  };

  const readViews = (article) => {
    const analyticsLink = article.querySelector('a[href*="/analytics"]');
    if (analyticsLink) {
      const aria = (analyticsLink.getAttribute('aria-label') || '').trim();
      const text = (analyticsLink.innerText || analyticsLink.textContent || '').trim();
      const ariaValue = parseCount(aria);
      if (ariaValue) return ariaValue;
      const textValue = parseCount(text);
      if (textValue) return textValue;
    }

    for (const el of article.querySelectorAll('[aria-label*="views"], [aria-label*="Views"]')) {
      const aria = (el.getAttribute('aria-label') || '').trim();
      const value = parseCount(aria);
      if (value) return value;
    }

    return 0;
  };

  const items = [];
  let feedPosition = 0;
  for (const article of document.querySelectorAll('article[data-testid="tweet"]')) {
    const articleText = (article.innerText || article.textContent || '').trim();
    if (!articleText) continue;
    if (/\bPromoted\b/i.test(articleText)) continue;
    const timeEl = article.querySelector('time');
    const tweetAnchor = timeEl ? timeEl.closest('a[href*="/status/"]') : article.querySelector('a[href*="/status/"]');
    const href = tweetAnchor ? tweetAnchor.getAttribute('href') : null;
    if (!href || !href.includes('/status/')) continue;

    const match = href.match(/^\/([^/]+)\/status\/(\d+)/);
    if (!match) continue;

    const handle = match[1];
    const tweetId = match[2];
    const userNameRoot = article.querySelector('div[data-testid="User-Name"]');
    const nameSpan = userNameRoot ? userNameRoot.querySelector('span') : null;
    const textRoot = article.querySelector('div[data-testid="tweetText"]');
    const text = textRoot ? textRoot.innerText.trim() : '';
    const socialContextRoot = article.querySelector('[data-testid="socialContext"]');
    const socialContext = socialContextRoot ? socialContextRoot.innerText.trim() : '';
    const isReply = /replying to @/i.test(articleText);
    const replyMentions = [];

    let linkedUrl = null;
    const mediaUrls = [];
    for (const anchor of article.querySelectorAll('a[href]')) {
      const rawHref = anchor.getAttribute('href');
      if (!rawHref) continue;
      const handleMatch = rawHref.match(/^\/([A-Za-z0-9_]{1,15})$/);
      if (handleMatch) {
        const replyHandle = handleMatch[1].toLowerCase();
        if (!replyMentions.includes(replyHandle)) {
          replyMentions.push(replyHandle);
        }
      }
      if (rawHref.startsWith('/')) continue;
      if (rawHref.includes('x.com') || rawHref.includes('twitter.com') || rawHref.includes('t.co')) continue;
      linkedUrl = rawHref;
      break;
    }

    for (const img of article.querySelectorAll('img[src]')) {
      const src = img.getAttribute('src') || '';
      if (!src) continue;
      if (!src.includes('pbs.twimg.com/media')) continue;
      if (!mediaUrls.includes(src)) {
        mediaUrls.push(src);
      }
    }

    items.push({
      tweet_id: tweetId,
      handle,
      name: nameSpan ? nameSpan.innerText.trim() : handle,
      text,
      datetime: timeEl ? timeEl.getAttribute('datetime') : null,
      href,
      linked_url: linkedUrl,
      media_urls: mediaUrls,
      social_context: socialContext || null,
      is_reply: isReply,
      reply_mentions: replyMentions,
      feed_position: feedPosition,
      metrics: {
        reply_count: readMetric(article, ['button[data-testid="reply"]']),
        repost_count: readMetric(article, ['button[data-testid="retweet"]', 'button[data-testid="unretweet"]']),
        like_count: readMetric(article, ['button[data-testid="like"]', 'button[data-testid="unlike"]']),
        view_count: readViews(article),
        quote_count: 0
      }
    });
    feedPosition += 1;
    if (items.length >= limit) break;
  }
  return items;
}
"""

EXTRACT_SINGLE_TWEET_JS = r"""
(options) => {
  const opts = options || {};
  const targetTweetId = String(opts.targetTweetId || '').trim();
  const parseCount = (value) => {
    if (!value) return 0;
    const cleaned = value.replace(/,/g, '').trim().toLowerCase();
    const match = cleaned.match(/([\d.]+)([km])?/);
    if (!match) return 0;
    const num = parseFloat(match[1]);
    if (Number.isNaN(num)) return 0;
    if (match[2] === 'k') return Math.round(num * 1000);
    if (match[2] === 'm') return Math.round(num * 1000000);
    return Math.round(num);
  };

  const readMetric = (article, selectors) => {
    for (const selector of selectors) {
      const button = article.querySelector(selector);
      if (!button) continue;
      const text = (button.innerText || button.textContent || '').trim();
      const value = parseCount(text);
      if (value) return value;
      const aria = (button.getAttribute('aria-label') || '').trim();
      const ariaValue = parseCount(aria);
      if (ariaValue) return ariaValue;
    }
    return 0;
  };

  const readViews = (article) => {
    const analyticsLink = article.querySelector('a[href*="/analytics"]');
    if (analyticsLink) {
      const aria = (analyticsLink.getAttribute('aria-label') || '').trim();
      const text = (analyticsLink.innerText || analyticsLink.textContent || '').trim();
      const ariaValue = parseCount(aria);
      if (ariaValue) return ariaValue;
      const textValue = parseCount(text);
      if (textValue) return textValue;
    }

    for (const el of article.querySelectorAll('[aria-label*="views"], [aria-label*="Views"]')) {
      const aria = (el.getAttribute('aria-label') || '').trim();
      const value = parseCount(aria);
      if (value) return value;
    }

    return 0;
  };

  const extractItem = (article) => {
    const articleText = (article.innerText || article.textContent || '').trim();
    if (!articleText) return null;
    if (/\bPromoted\b/i.test(articleText)) return null;
    const timeEl = article.querySelector('time');
    const tweetAnchor = timeEl ? timeEl.closest('a[href*="/status/"]') : article.querySelector('a[href*="/status/"]');
    const href = tweetAnchor ? tweetAnchor.getAttribute('href') : null;
    if (!href || !href.includes('/status/')) return null;

    const match = href.match(/^\/([^/]+)\/status\/(\d+)/);
    if (!match) return null;

    const handle = match[1];
    const tweetId = match[2];
    if (targetTweetId && tweetId !== targetTweetId) return null;

    const userNameRoot = article.querySelector('div[data-testid="User-Name"]');
    const nameSpan = userNameRoot ? userNameRoot.querySelector('span') : null;
    const textRoot = article.querySelector('div[data-testid="tweetText"]');
    const text = textRoot ? textRoot.innerText.trim() : '';
    const socialContextRoot = article.querySelector('[data-testid="socialContext"]');
    const socialContext = socialContextRoot ? socialContextRoot.innerText.trim() : '';
    const isReply = /replying to @/i.test(articleText);
    const replyMentions = [];

    let linkedUrl = null;
    const mediaUrls = [];
    for (const anchor of article.querySelectorAll('a[href]')) {
      const rawHref = anchor.getAttribute('href');
      if (!rawHref) continue;
      const handleMatch = rawHref.match(/^\/([A-Za-z0-9_]{1,15})$/);
      if (handleMatch) {
        const replyHandle = handleMatch[1].toLowerCase();
        if (!replyMentions.includes(replyHandle)) {
          replyMentions.push(replyHandle);
        }
      }
      if (rawHref.startsWith('/')) continue;
      if (rawHref.includes('x.com') || rawHref.includes('twitter.com') || rawHref.includes('t.co')) continue;
      linkedUrl = rawHref;
      break;
    }

    for (const img of article.querySelectorAll('img[src]')) {
      const src = img.getAttribute('src') || '';
      if (!src) continue;
      if (!src.includes('pbs.twimg.com/media')) continue;
      if (!mediaUrls.includes(src)) {
        mediaUrls.push(src);
      }
    }

    return {
      tweet_id: tweetId,
      handle,
      name: nameSpan ? nameSpan.innerText.trim() : handle,
      text,
      datetime: timeEl ? timeEl.getAttribute('datetime') : null,
      href,
      linked_url: linkedUrl,
      media_urls: mediaUrls,
      social_context: socialContext || null,
      is_reply: isReply,
      reply_mentions: replyMentions,
      feed_position: 0,
      metrics: {
        reply_count: readMetric(article, ['button[data-testid="reply"]']),
        repost_count: readMetric(article, ['button[data-testid="retweet"]', 'button[data-testid="unretweet"]']),
        like_count: readMetric(article, ['button[data-testid="like"]', 'button[data-testid="unlike"]']),
        view_count: readViews(article),
        quote_count: 0
      }
    };
  };

  for (const article of document.querySelectorAll('article[data-testid="tweet"]')) {
    const item = extractItem(article);
    if (item) return item;
  }
  return null;
}
"""


class XScraper:
    def __init__(self, config: AppConfig):
        self.config = config

    def scrape_sources(self, sources: list[SourceConfig]) -> list[ScrapedPost]:
        if not self.config.storage_state_path.exists():
            raise RuntimeError(
                "Missing Playwright session state. Run scripts/capture-x-session.py after logging into X."
            )
        posts: list[ScrapedPost] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.config.playwright_headless)
            context = browser.new_context(storage_state=str(self.config.storage_state_path))
            try:
                for source in sources:
                    posts.extend(self.scrape_source(context, source))
            finally:
                context.close()
                browser.close()
        deduped: dict[str, ScrapedPost] = {}
        for post in posts:
            deduped[post.tweet_id] = post
        return list(deduped.values())

    def scrape_source(self, context: BrowserContext, source: SourceConfig) -> list[ScrapedPost]:
        if not source.url:
            return []
        page = context.new_page()
        try:
            page.goto(source.url, wait_until="domcontentloaded", timeout=self.config.worker.scrape_timeout_ms)
            page.wait_for_timeout(2500)
            scroll_rounds = 4 if source.type == "home" else 3
            for _ in range(scroll_rounds):
                page.mouse.wheel(0, 2200)
                page.wait_for_timeout(1200)
            raw_items: list[dict[str, Any]] = page.evaluate(
                EXTRACT_TWEETS_JS,
                {"limit": self.config.worker.homepage_scrape_limit if source.type == "home" else 80},
            )
        finally:
            page.close()
        return [self._scraped_post_from_item(source.key, source.url, item) for item in raw_items]

    def scrape_tweet_url(self, url: str, source_key: str = "manual_link") -> ScrapedPost:
        normalized_url = normalize_tweet_url(url)
        _, tweet_id, canonical_url = parse_tweet_url(normalized_url)
        if not self.config.storage_state_path.exists():
            raise RuntimeError(
                "Missing Playwright session state. Run scripts/capture-x-session.py after logging into X."
            )
        raw_item: dict[str, Any] | None = None
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.config.playwright_headless)
            context = browser.new_context(storage_state=str(self.config.storage_state_path))
            page = context.new_page()
            try:
                page.goto(canonical_url, wait_until="domcontentloaded", timeout=self.config.worker.scrape_timeout_ms)
                page.wait_for_selector("article[data-testid='tweet']", timeout=self.config.worker.scrape_timeout_ms)
                page.wait_for_timeout(1800)
                raw_item = page.evaluate(EXTRACT_SINGLE_TWEET_JS, {"targetTweetId": tweet_id})
                if not raw_item:
                    page.mouse.wheel(0, 1400)
                    page.wait_for_timeout(1000)
                    raw_item = page.evaluate(EXTRACT_SINGLE_TWEET_JS, {"targetTweetId": tweet_id})
            finally:
                page.close()
                context.close()
                browser.close()
        if not raw_item:
            raise RuntimeError(
                "Could not load that tweet from X. Make sure the status link is valid, public to your account, and your saved X session still works."
            )
        return self._scraped_post_from_item(source_key, canonical_url, raw_item)

    def _scraped_post_from_item(self, source_key: str, source_url: str, item: dict[str, Any]) -> ScrapedPost:
        posted_at = None
        if item.get("datetime"):
            try:
                posted_at = date_parser.isoparse(item["datetime"])
            except Exception:
                posted_at = None
        url = item["href"]
        if url.startswith("/"):
            url = f"https://x.com{url}"
        return ScrapedPost(
            tweet_id=item["tweet_id"],
            source_key=source_key,
            source_url=source_url,
            author_handle=item["handle"],
            author_name=item.get("name") or item["handle"],
            text=item.get("text", ""),
            posted_at=posted_at,
            url=url,
            linked_url=item.get("linked_url"),
            metrics=item.get("metrics", {}),
            raw=item,
        )


def normalize_tweet_id(url: str) -> str | None:
    try:
        return parse_tweet_url(url)[1]
    except ValueError:
        match = re.search(r"/status/(\d+)", url)
        return match.group(1) if match else None


def normalize_tweet_url(url: str) -> str:
    return parse_tweet_url(url)[2]


def parse_tweet_url(url: str) -> tuple[str, str, str]:
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("Tweet link is required.")
    if "://" not in raw:
        raw = f"https://{raw.lstrip('/')}"
    match = TWEET_URL_PATTERN.search(raw)
    if not match:
        raise ValueError("Paste a full X/Twitter status link, like https://x.com/handle/status/1234567890.")
    handle = match.group(1)
    tweet_id = match.group(2)
    canonical_url = f"https://x.com/{handle}/status/{tweet_id}"
    return handle, tweet_id, canonical_url
