from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from .types import ScrapedPost, SourceConfig, WorkerSettings


def age_minutes(posted_at: datetime | None) -> float:
    if not posted_at:
        return 999.0
    delta = datetime.now(timezone.utc) - posted_at.astimezone(timezone.utc)
    return max(delta.total_seconds() / 60.0, 0.0)


def human_age(posted_at: datetime | None) -> str:
    minutes = age_minutes(posted_at)
    if minutes >= 999:
        return "unknown"
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{int(round(minutes))}m ago"
    hours = minutes / 60.0
    if hours < 24:
        whole_hours = int(hours)
        rem_minutes = int(round(minutes - whole_hours * 60))
        if rem_minutes <= 0:
            return f"{whole_hours}h ago"
        return f"{whole_hours}h {rem_minutes}m ago"
    days = int(hours // 24)
    return f"{days}d ago"


def score_breakdown(
    post: ScrapedPost,
    source: SourceConfig,
    worker: WorkerSettings,
    author_stats: dict[str, int] | None = None,
) -> dict[str, Any]:
    metrics = post.metrics or {}
    author_stats = author_stats or {}
    age = age_minutes(post.posted_at)
    max_age = float(source.max_age_minutes or 120)
    freshness = max(0.0, 1.0 - min(age, max_age) / max_age)
    weighted_engagement = (
        metrics.get("like_count", 0) * 1.0
        + metrics.get("reply_count", 0) * 2.2
        + metrics.get("repost_count", 0) * 2.0
        + metrics.get("quote_count", 0) * 2.3
        + min(metrics.get("view_count", 0) / 350.0, 36.0)
    )
    linked_bonus = 12.0 if post.linked_url else 0.0
    length_bonus = 7.0 if len(post.text.split()) >= 16 else 0.0
    text = " ".join(part for part in [post.text, post.linked_url or ""]).lower()
    launch_bonus = 8.0 if _looks_like_launch_or_release(text) else 0.0
    news_bonus = 7.0 if _looks_like_news_signal(text) else 0.0
    social_bonus = 0.0
    if str(post.raw.get("social_context") or "").strip():
        social_bonus = 9.0 if source.type == "home" else 5.0
    author_bonus = min(
        author_stats.get("priority_source_hits", 0) * 3.5
        + author_stats.get("distinct_sources", 0) * 2.0,
        16.0,
    )
    focus = _focus_scores(post, source, worker, author_stats, launch_bonus=launch_bonus, news_bonus=news_bonus)
    bucket = _bucket_for_focus(focus["niche_fit"])
    feed_bonus = _feed_bonus(post) if source.type == "home" else 0.0
    low_signal_penalty = 0.0
    if bool(post.raw.get("is_reply")) and not social_bonus and weighted_engagement < 10:
        low_signal_penalty -= 16.0
    if len(post.text.split()) < 5 and not post.linked_url:
        low_signal_penalty -= 10.0
    low_signal_penalty -= focus["off_topic_penalty"]

    if source.type == "home":
        engagement_gate = 0.42 + focus["niche_fit"] * 0.58
        velocity_gate = 0.18 + focus["niche_fit"] * 0.82
        velocity = min((weighted_engagement / max(age, 12.0)) * 18.0, 34.0) * velocity_gate
        views = metrics.get("view_count", 0)
        engagement_rate = (
            min((weighted_engagement / max(views, 1)) * 800.0, 18.0) * engagement_gate if views else 0.0
        )
        replyability = _replyability_bonus(post)
        source_bonus = source.source_weight * 18.0
        niche_bonus = focus["niche_fit"] * 32.0 + focus["topic_relevance"] * 10.0 + focus["creator_fit"] * 7.0
        score = (
            freshness * 44.0
            + min(weighted_engagement * engagement_gate, 42.0)
            + velocity
            + engagement_rate
            + source_bonus
            + linked_bonus
            + launch_bonus
            + news_bonus
            + social_bonus
            + author_bonus
            + replyability
            + niche_bonus
            + feed_bonus
            + low_signal_penalty
        )
        tags = _score_tags(
            freshness=freshness,
            linked_bonus=linked_bonus,
            launch_bonus=launch_bonus,
            news_bonus=news_bonus,
            social_bonus=social_bonus,
            author_bonus=author_bonus,
            focus=focus,
            extra={
                "velocity": velocity,
                "engagement_rate": engagement_rate,
                "replyability": replyability,
                "feed_bonus": feed_bonus,
            },
        )
        return {
            "score": round(score, 2),
            "age_minutes": round(age, 1),
            "weighted_engagement": round(weighted_engagement, 2),
            "velocity": round(velocity, 2),
            "engagement_rate": round(engagement_rate, 2),
            "freshness": round(freshness, 3),
            "topic_relevance": round(focus["topic_relevance"], 3),
            "creator_fit": round(focus["creator_fit"], 3),
            "niche_fit": round(focus["niche_fit"], 3),
            "opportunity_bucket": bucket,
            "off_topic_penalty": round(focus["off_topic_penalty"], 2),
            "tags": tags,
            "summary": ", ".join(tags[:4]) if tags else "home timeline candidate",
        }

    source_bonus = source.source_weight * 20.0
    engagement_gate = 0.55 + focus["niche_fit"] * 0.45
    engagement_bonus = min(weighted_engagement * engagement_gate, 120.0)
    niche_bonus = focus["niche_fit"] * 26.0 + focus["topic_relevance"] * 8.0 + focus["creator_fit"] * 6.0
    score = (
        freshness * 60.0
        + source_bonus
        + linked_bonus
        + length_bonus
        + engagement_bonus
        + launch_bonus
        + news_bonus
        + social_bonus
        + author_bonus
        + niche_bonus
        + low_signal_penalty
    )
    tags = _score_tags(
        freshness=freshness,
        linked_bonus=linked_bonus,
        launch_bonus=launch_bonus,
        news_bonus=news_bonus,
        social_bonus=social_bonus,
        author_bonus=author_bonus,
        focus=focus,
        extra={"engagement": engagement_bonus, "length": length_bonus},
    )
    return {
        "score": round(score, 2),
        "age_minutes": round(age, 1),
        "weighted_engagement": round(weighted_engagement, 2),
        "freshness": round(freshness, 3),
        "topic_relevance": round(focus["topic_relevance"], 3),
        "creator_fit": round(focus["creator_fit"], 3),
        "niche_fit": round(focus["niche_fit"], 3),
        "opportunity_bucket": bucket,
        "off_topic_penalty": round(focus["off_topic_penalty"], 2),
        "tags": tags,
        "summary": ", ".join(tags[:4]) if tags else "fresh list candidate",
    }


def heuristic_score(post: ScrapedPost, source: SourceConfig, worker: WorkerSettings) -> float:
    return float(score_breakdown(post, source, worker)["score"])


def metrics_summary(raw_metrics: str | dict[str, int]) -> str:
    metrics = json.loads(raw_metrics) if isinstance(raw_metrics, str) else raw_metrics
    return (
        f"likes {metrics.get('like_count', 0)}, replies {metrics.get('reply_count', 0)}, "
        f"reposts {metrics.get('repost_count', 0)}, quotes {metrics.get('quote_count', 0)}"
    )


def _looks_like_launch_or_release(text: str) -> bool:
    patterns = (
        r"\blaunch\b",
        r"\breleased?\b",
        r"\bshipping\b",
        r"\bnow live\b",
        r"\bapi\b",
        r"\bmodel\b",
        r"\bbenchmark\b",
        r"\bdocs\b",
        r"\bpricing\b",
        r"\bwaitlist\b",
        r"\bopen source\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _looks_like_news_signal(text: str) -> bool:
    patterns = (
        r"\bbreaking\b",
        r"\bannounc(?:e|ed|ing)\b",
        r"\bintroducing\b",
        r"\bupdate\b",
        r"\bjust shipped\b",
        r"\broll(?:ing)? out\b",
        r"\bavailable now\b",
        r"\bchangelog\b",
        r"\bdocs\b",
        r"\bnow supports\b",
        r"\bnow available\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _replyability_bonus(post: ScrapedPost) -> float:
    bonus = 0.0
    text = post.text.lower()
    if "?" in post.text:
        bonus += 4.0
    if post.linked_url:
        bonus += 4.0
    if len(post.text.split()) >= 18:
        bonus += 3.0
    if any(keyword in text for keyword in ("wrong", "hot take", "unpopular", "tradeoff", "benchmark", "pricing")):
        bonus += 4.0
    if bool(post.raw.get("is_reply")):
        bonus -= 2.0
    return max(min(bonus, 12.0), -4.0)


def _focus_scores(
    post: ScrapedPost,
    source: SourceConfig,
    worker: WorkerSettings,
    author_stats: dict[str, int],
    *,
    launch_bonus: float,
    news_bonus: float,
) -> dict[str, float]:
    text = " ".join(part for part in [post.text, post.linked_url or ""]).lower()
    primary_hits = _keyword_hits(text, worker.focus_keywords)
    secondary_hits = _keyword_hits(text, worker.secondary_focus_keywords)
    deprioritize_hits = _keyword_hits(text, worker.deprioritize_keywords)
    tech_link_bonus = 0.18 if _looks_like_tech_link(post.linked_url) else 0.0
    is_high_velocity = source.key == "high_velocity"
    source_bias = 0.4 if is_high_velocity else 0.15 if source.type != "home" else 0.0
    topic_relevance = min(
        primary_hits * 0.2
        + secondary_hits * 0.09
        + tech_link_bonus
        + (0.1 if launch_bonus else 0.0)
        + (0.1 if news_bonus else 0.0)
        + source_bias,
        1.0,
    )
    creator_fit = min(
        author_stats.get("priority_source_hits", 0) * 0.26
        + author_stats.get("distinct_sources", 0) * 0.12
        + author_stats.get("recent_posts", 0) * 0.03,
        1.0,
    )
    niche_fit = min(topic_relevance * 0.72 + creator_fit * 0.28, 1.0)
    off_topic_penalty = 0.0
    if deprioritize_hits:
        off_topic_penalty = deprioritize_hits * (10.0 if topic_relevance < 0.45 else 4.0)
    if is_high_velocity:
        off_topic_penalty *= 0.2
    if source.type == "home" and topic_relevance < 0.14 and creator_fit < 0.25:
        off_topic_penalty += 10.0
    return {
        "primary_hits": float(primary_hits),
        "secondary_hits": float(secondary_hits),
        "deprioritize_hits": float(deprioritize_hits),
        "topic_relevance": topic_relevance,
        "creator_fit": creator_fit,
        "niche_fit": niche_fit,
        "off_topic_penalty": min(off_topic_penalty, 30.0),
    }


def _bucket_for_focus(niche_fit: float) -> str:
    if niche_fit >= 0.32:
        return "core"
    if niche_fit >= 0.14:
        return "adjacent"
    return "opportunistic"


def _feed_bonus(post: ScrapedPost) -> float:
    try:
        position = int(post.raw.get("feed_position"))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, 8.0 - position * 0.8)


def _keyword_hits(text: str, keywords: Iterable[str]) -> int:
    hits = 0
    for keyword in keywords:
        normalized = keyword.strip().lower()
        if not normalized:
            continue
        if " " in normalized:
            if normalized in text:
                hits += 1
            continue
        if re.search(rf"\b{re.escape(normalized)}\b", text):
            hits += 1
    return hits


def _looks_like_tech_link(url: str | None) -> bool:
    if not url:
        return False
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return False
    tech_domains = (
        "github.com",
        "gitlab.com",
        "docs.",
        "developer.",
        "developers.",
        "vercel.com",
        "huggingface.co",
        "openai.com",
        "anthropic.com",
        "cloud.google.com",
    )
    return any(domain in netloc for domain in tech_domains)


def _score_tags(
    freshness: float,
    linked_bonus: float,
    launch_bonus: float,
    news_bonus: float,
    social_bonus: float,
    author_bonus: float,
    focus: dict[str, float],
    extra: dict[str, float],
) -> list[str]:
    tags: list[str] = []
    if freshness >= 0.7:
        tags.append("very fresh")
    elif freshness >= 0.4:
        tags.append("still fresh")
    if linked_bonus:
        tags.append("has link")
    if launch_bonus:
        tags.append("launch/release signal")
    if news_bonus:
        tags.append("timely update")
    if social_bonus:
        tags.append("second-degree network")
    if author_bonus >= 6:
        tags.append("author keeps surfacing")
    niche_fit = focus.get("niche_fit", 0.0)
    topic_relevance = focus.get("topic_relevance", 0.0)
    if niche_fit >= 0.32:
        tags.append("strong niche fit")
    elif niche_fit >= 0.14:
        tags.append("adjacent to niche")
    if topic_relevance >= 0.5:
        tags.append("tech/builder topic")
    if focus.get("creator_fit", 0.0) >= 0.45:
        tags.append("creator already fits graph")
    if focus.get("off_topic_penalty", 0.0) >= 10:
        tags.append("off-topic risk")
    velocity = extra.get("velocity", 0.0)
    if velocity >= 16:
        tags.append("early velocity")
    engagement_rate = extra.get("engagement_rate", 0.0)
    if engagement_rate >= 7:
        tags.append("healthy engagement rate")
    engagement = extra.get("engagement", 0.0)
    if engagement >= 25:
        tags.append("engagement already visible")
    if extra.get("replyability", 0.0) >= 6:
        tags.append("clear reply angle")
    if extra.get("length", 0.0):
        tags.append("enough context")
    if extra.get("feed_bonus", 0.0) >= 5:
        tags.append("high in home feed")
    return tags
