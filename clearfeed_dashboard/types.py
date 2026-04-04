from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class SourceConfig:
    key: str
    label: str
    type: str
    cadence_minutes: int
    source_weight: float
    preferred_action: str
    url: str | None = None
    use_for_original_posts: bool = False
    max_age_minutes: int | None = None


@dataclass(slots=True)
class WorkerSettings:
    min_delay_minutes: int
    max_delay_minutes: int
    max_candidates_per_cycle: int
    candidate_overlap_minutes: int
    max_reply_age_minutes: int
    article_expand_char_limit: int
    scrape_timeout_ms: int
    recent_signals_limit: int
    original_post_options: int
    max_original_drafts_per_day: int
    default_image_mode: str
    homepage_scrape_limit: int
    homepage_llm_pool_size: int
    list_max_alerts_per_cycle: int
    homepage_max_alerts_per_cycle: int
    homepage_max_opportunistic_alerts_per_cycle: int
    list_min_views_required: int
    list_min_views_age_minutes: int
    homepage_min_views_required: int
    homepage_min_views_age_minutes: int
    author_signal_lookback_hours: int
    focus_keywords: list[str]
    secondary_focus_keywords: list[str]
    deprioritize_keywords: list[str]
    voice_review_enabled: bool
    voice_review_interval_hours: int
    voice_review_mode: str
    voice_review_cadence: str
    voice_review_min_examples: int
    voice_review_max_examples: int


@dataclass(slots=True)
class ScrapedPost:
    tweet_id: str
    source_key: str
    source_url: str
    author_handle: str
    author_name: str
    text: str
    posted_at: datetime | None
    url: str
    linked_url: str | None
    metrics: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CandidateDecision:
    tweet_id: str
    heuristic_score: float
    llm_score: float
    total_score: float
    recommended_action: str
    why: str


@dataclass(slots=True)
class DraftPayload:
    draft_type: str
    text: str
    rationale: str
    image_prompt: str | None = None
    image_reason: str | None = None
