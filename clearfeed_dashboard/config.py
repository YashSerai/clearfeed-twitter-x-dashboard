from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .types import SourceConfig, WorkerSettings


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _optional_env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _get_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be a number.") from exc


def _get_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer.") from exc


def _normalize_voice_review_mode(value: str | None, default: str) -> str:
    normalized = (value or default or "scheduled").strip().lower()
    if normalized not in {"scheduled", "manual"}:
        raise RuntimeError("VOICE_REVIEW_MODE must be `scheduled` or `manual`.")
    return normalized


def _voice_review_cadence_from_hours(hours: int) -> str:
    if hours == 24:
        return "daily"
    if hours == 24 * 7:
        return "weekly"
    if hours == 24 * 30:
        return "monthly"
    return "custom"


def _normalize_voice_review_cadence(value: str | None, default: str) -> str:
    normalized = (value or default or "daily").strip().lower()
    if normalized not in {"daily", "weekly", "monthly", "custom"}:
        raise RuntimeError("VOICE_REVIEW_CADENCE must be `daily`, `weekly`, or `monthly`.")
    return normalized


def _voice_review_interval_hours_for_cadence(cadence: str, fallback_hours: int) -> int:
    if cadence == "daily":
        return 24
    if cadence == "weekly":
        return 24 * 7
    if cadence == "monthly":
        return 24 * 30
    return fallback_hours


@dataclass(slots=True)
class AppConfig:
    root: Path
    database_path: Path
    storage_state_path: Path
    timezone: str
    playwright_headless: bool
    ai_provider: str
    ai_text_model: str
    ai_polish_model: str
    ai_originals_model: str
    ai_voice_review_model: str
    ai_archive_voice_model: str
    ai_vision_model: str | None
    ai_image_model: str | None
    google_cloud_project: str | None
    google_cloud_location: str
    google_application_credentials: str | None
    openai_compat_base_url: str | None
    openai_compat_api_key: str | None
    openai_compat_timeout_seconds: int
    vertex_timeout_seconds: int
    vertex_max_retries: int
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    public_base_url: str | None
    telegram_webapp_enabled_flag: bool
    telegram_legacy_forwarding_enabled_flag: bool
    cloudflared_auto_start: bool
    cloudflared_tunnel_mode: str
    style_files: list[Path]
    worker: WorkerSettings
    sources: list[SourceConfig]

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def normalized_public_base_url(self) -> str | None:
        if not self.public_base_url:
            return None
        return self.public_base_url.rstrip("/")

    @property
    def telegram_webapp_enabled(self) -> bool:
        public_url = self.normalized_public_base_url
        if not public_url:
            return False
        parsed = urlparse(public_url)
        return bool(
            self.telegram_enabled
            and self.telegram_webapp_enabled_flag
            and parsed.scheme == "https"
            and parsed.netloc
        )

    @property
    def telegram_legacy_forwarding_enabled(self) -> bool:
        return bool(self.telegram_enabled and self.telegram_legacy_forwarding_enabled_flag)

    @property
    def provider_label(self) -> str:
        return "Vertex" if self.ai_provider == "vertex" else "OpenAI-Compatible"

    @property
    def provider_config_ready(self) -> bool:
        if self.ai_provider == "openai_compatible":
            return bool(self.openai_compat_base_url and self.ai_text_model and self.ai_polish_model)
        return bool(self.google_cloud_project and self.google_application_credentials and self.ai_text_model and self.ai_polish_model)

    @property
    def drafting_enabled(self) -> bool:
        return self.provider_config_ready

    @property
    def vision_model_name(self) -> str | None:
        if self.ai_vision_model:
            return self.ai_vision_model
        if self.ai_provider == "vertex" and self.ai_text_model:
            return self.ai_text_model
        return None

    @property
    def vision_enabled(self) -> bool:
        return bool(self.provider_config_ready and self.vision_model_name)

    @property
    def image_generation_enabled(self) -> bool:
        return bool(self.provider_config_ready and self.ai_image_model)

    @property
    def web_research_enabled(self) -> bool:
        return self.ai_provider == "vertex" and self.provider_config_ready

    @property
    def session_ready(self) -> bool:
        return self.storage_state_path.exists()

    @property
    def sources_ready(self) -> bool:
        return bool(self.sources)

    def setup_status(self) -> dict[str, dict[str, str | bool]]:
        provider_detail = (
            f"{self.provider_label} provider configured."
            if self.provider_config_ready
            else (
                "Set OpenAI-compatible base URL and models."
                if self.ai_provider == "openai_compatible"
                else "Add Google project, credentials, and models."
            )
        )
        drafting_detail = (
            " | ".join(
                [
                    f"Text: {self.ai_text_model}",
                    f"Polish: {self.ai_polish_model}",
                    f"Originals: {self.ai_originals_model}",
                    f"Voice Review: {self.ai_voice_review_model}",
                    f"Archive Voice: {self.ai_archive_voice_model}",
                ]
            )
            if self.provider_config_ready
            else "Add text and polish models for drafting."
        )
        media_detail_parts: list[str] = []
        if self.vision_enabled and self.vision_model_name:
            media_detail_parts.append(f"Vision: {self.vision_model_name}")
        else:
            media_detail_parts.append("Vision: not configured")
        if self.image_generation_enabled and self.ai_image_model:
            media_detail_parts.append(f"Image: {self.ai_image_model}")
        else:
            media_detail_parts.append("Image: not configured")
        return {
            "provider": {
                "ok": self.provider_config_ready,
                "label": "AI Provider",
                "detail": provider_detail,
            },
            "drafting": {
                "ok": self.provider_config_ready,
                "label": "Drafting Models",
                "detail": drafting_detail,
            },
            "profiles": {"ok": True, "label": "Voice Profiles", "detail": "Templates ready."},
            "media": {
                "ok": self.vision_enabled and self.image_generation_enabled,
                "label": "Image + Vision",
                "detail": " | ".join(media_detail_parts),
            },
            "web": {
                "ok": self.web_research_enabled,
                "label": "Web Research",
                "detail": (
                    "Grounded web research is available."
                    if self.web_research_enabled
                    else "Optional. Unavailable on this provider."
                ),
            },
            "session": {
                "ok": self.session_ready,
                "label": "X Session",
                "detail": "Session captured." if self.session_ready else "Run capture-x-session.ps1.",
            },
            "sources": {
                "ok": self.sources_ready,
                "label": "Discovery Sources",
                "detail": f"{len(self.sources)} source(s) configured." if self.sources_ready else "Add at least one source.",
            },
            "telegram": {
                "ok": self.telegram_webapp_enabled,
                "label": "Telegram Access",
                "detail": (
                    (
                        f"Telegram is using the tunneled Mini App at {self.normalized_public_base_url}."
                        if self.telegram_webapp_enabled
                        else (
                            "Telegram Mini App is enabled. Use a public HTTPS tunnel URL for PUBLIC_BASE_URL and start services to refresh it."
                            if self.telegram_enabled and self.telegram_webapp_enabled_flag
                            else "Optional. Telegram is off."
                        )
                    )
                ),
            },
        }


def load_config(root: str | Path | None = None) -> AppConfig:
    project_root = Path(root or Path.cwd()).resolve()
    _load_env_file(project_root / ".env")

    cfg = yaml.safe_load((project_root / "config.yaml").read_text(encoding="utf-8"))
    sources_cfg = yaml.safe_load((project_root / "data" / "sources" / "x_sources.yaml").read_text(encoding="utf-8"))

    worker_cfg = dict(cfg["worker"])
    worker_cfg.setdefault("list_max_alerts_per_cycle", int(worker_cfg.get("max_candidates_per_cycle", 6)))
    worker_cfg.setdefault("homepage_max_opportunistic_alerts_per_cycle", 1)
    worker_cfg.setdefault("list_min_views_required", 1000)
    worker_cfg.setdefault("list_min_views_age_minutes", 10)
    worker_cfg.setdefault("homepage_min_views_required", 10000)
    worker_cfg.setdefault("homepage_min_views_age_minutes", 10)
    worker_cfg.setdefault("original_topics_per_batch", 3)
    worker_cfg.setdefault(
        "original_topic_suggestion_limit",
        int(worker_cfg.get("original_post_options", 5)),
    )
    worker_cfg.setdefault("focus_keywords", [])
    worker_cfg.setdefault("secondary_focus_keywords", [])
    worker_cfg.setdefault("deprioritize_keywords", [])
    worker_cfg["min_delay_minutes"] = _get_int("WORKER_MIN_DELAY_MINUTES", int(worker_cfg["min_delay_minutes"]))
    worker_cfg["max_delay_minutes"] = _get_int("WORKER_MAX_DELAY_MINUTES", int(worker_cfg["max_delay_minutes"]))
    worker_cfg["max_candidates_per_cycle"] = _get_int(
        "WORKER_MAX_CANDIDATES_PER_CYCLE",
        int(worker_cfg["max_candidates_per_cycle"]),
    )
    worker_cfg["homepage_scrape_limit"] = _get_int(
        "WORKER_HOMEPAGE_SCRAPE_LIMIT",
        int(worker_cfg["homepage_scrape_limit"]),
    )
    worker_cfg["homepage_llm_pool_size"] = _get_int(
        "WORKER_HOMEPAGE_LLM_POOL_SIZE",
        int(worker_cfg["homepage_llm_pool_size"]),
    )
    worker_cfg["homepage_max_alerts_per_cycle"] = _get_int(
        "WORKER_HOMEPAGE_MAX_ALERTS_PER_CYCLE",
        int(worker_cfg["homepage_max_alerts_per_cycle"]),
    )
    worker_cfg["recent_signals_limit"] = _get_int(
        "WORKER_RECENT_SIGNALS_LIMIT",
        int(worker_cfg["recent_signals_limit"]),
    )
    worker_cfg["original_post_options"] = _get_int(
        "WORKER_ORIGINAL_POST_OPTIONS",
        int(worker_cfg["original_post_options"]),
    )
    worker_cfg["original_topics_per_batch"] = _get_int(
        "WORKER_ORIGINAL_TOPICS_PER_BATCH",
        int(worker_cfg["original_topics_per_batch"]),
    )
    worker_cfg["max_original_drafts_per_day"] = _get_int(
        "WORKER_MAX_ORIGINAL_DRAFTS_PER_DAY",
        int(worker_cfg["max_original_drafts_per_day"]),
    )
    worker_cfg["original_topic_suggestion_limit"] = _get_int(
        "WORKER_ORIGINAL_TOPIC_SUGGESTION_LIMIT",
        int(worker_cfg["original_topic_suggestion_limit"]),
    )
    legacy_voice_review_enabled = bool(worker_cfg.get("voice_review_enabled", True))
    legacy_voice_review_interval_hours = int(worker_cfg.get("voice_review_interval_hours", 24))
    worker_cfg["voice_review_mode"] = _normalize_voice_review_mode(
        os.environ.get("VOICE_REVIEW_MODE"),
        "scheduled" if legacy_voice_review_enabled else "manual",
    )
    worker_cfg["voice_review_cadence"] = _normalize_voice_review_cadence(
        os.environ.get("VOICE_REVIEW_CADENCE"),
        _voice_review_cadence_from_hours(legacy_voice_review_interval_hours),
    )
    worker_cfg["voice_review_enabled"] = worker_cfg["voice_review_mode"] == "scheduled"
    worker_cfg["voice_review_interval_hours"] = _voice_review_interval_hours_for_cadence(
        str(worker_cfg["voice_review_cadence"]),
        legacy_voice_review_interval_hours,
    )
    if int(worker_cfg["max_delay_minutes"]) < int(worker_cfg["min_delay_minutes"]):
        raise RuntimeError("WORKER_MAX_DELAY_MINUTES must be greater than or equal to WORKER_MIN_DELAY_MINUTES.")

    worker = WorkerSettings(**worker_cfg)
    style_files = [project_root / rel_path for rel_path in cfg["style"]["files"]]
    missing_style_files = [str(path.relative_to(project_root)) for path in style_files if not path.exists()]
    if missing_style_files:
        raise RuntimeError(
            "Missing required profile file(s): "
            + ", ".join(missing_style_files)
            + ". Run scripts/setup.ps1 and fill out the profile templates."
        )

    sources: list[SourceConfig] = []
    for item in sources_cfg["sources"]:
        item_type = str(item.get("type") or "").strip().lower()
        if item_type not in {"list", "home"}:
            raise RuntimeError(
                f"Unsupported source type `{item_type or 'unknown'}` in data/sources/x_sources.yaml. "
                "Supported source types are `list` and `home`."
            )
        enabled_env_var = item.get("enabled_env_var")
        if enabled_env_var and not _get_bool(str(enabled_env_var), bool(item.get("enabled", False))):
            continue
        url = str(item.get("url") or "").strip() or None
        env_var = item.get("env_var")
        if env_var:
            env_value = os.environ.get(str(env_var), "").strip()
            if env_value:
                url = env_value
        if item_type == "home":
            url = "https://x.com/home"
        elif not url:
            continue
        source_weight = float(item.get("source_weight", 1.0))
        weight_env_var = item.get("weight_env_var")
        if weight_env_var:
            source_weight = _get_float(str(weight_env_var), source_weight)
        sources.append(
            SourceConfig(
                key=item["key"],
                label=item["label"],
                type=item_type,
                cadence_minutes=int(item["cadence_minutes"]),
                source_weight=source_weight,
                preferred_action=item["preferred_action"],
                url=url,
                use_for_original_posts=bool(item.get("use_for_original_posts", False)),
                max_age_minutes=int(item["max_age_minutes"]) if item.get("max_age_minutes") else None,
                min_view_count=int(item["min_view_count"]) if item.get("min_view_count") else None,
                min_view_age_minutes=int(item["min_view_age_minutes"]) if item.get("min_view_age_minutes") else None,
            )
        )

    ai_provider = os.environ.get("AI_PROVIDER", "vertex").strip().lower() or "vertex"
    if ai_provider not in {"vertex", "openai_compatible"}:
        raise RuntimeError("AI_PROVIDER must be `vertex` or `openai_compatible`.")
    ai_text_model = _optional_env("AI_TEXT_MODEL") or _optional_env("GEMINI_TEXT_MODEL") or ("gemini-3-flash-preview" if ai_provider == "vertex" else "")
    ai_polish_model = _optional_env("AI_POLISH_MODEL") or _optional_env("GEMINI_POLISH_MODEL") or ("gemini-3-flash-preview" if ai_provider == "vertex" else "")
    ai_originals_model = _optional_env("AI_ORIGINALS_MODEL") or ai_polish_model or ai_text_model
    ai_voice_review_model = _optional_env("AI_VOICE_REVIEW_MODEL") or ai_originals_model or ai_polish_model or ai_text_model
    ai_archive_voice_model = _optional_env("AI_ARCHIVE_VOICE_MODEL") or ai_voice_review_model or ai_originals_model or ai_polish_model or ai_text_model
    ai_vision_model = _optional_env("AI_VISION_MODEL")
    ai_image_model = _optional_env("AI_IMAGE_MODEL") or _optional_env("GEMINI_IMAGE_MODEL")

    return AppConfig(
        root=project_root,
        database_path=(project_root / os.environ.get("DATABASE_PATH", "./data/marketing.sqlite3")).resolve(),
        storage_state_path=(project_root / os.environ.get("PLAYWRIGHT_STORAGE_STATE", "./data/browser/x_storage_state.json")).resolve(),
        timezone=os.environ.get("TIMEZONE", "America/Vancouver"),
        playwright_headless=_get_bool("PLAYWRIGHT_HEADLESS", True),
        ai_provider=ai_provider,
        ai_text_model=ai_text_model,
        ai_polish_model=ai_polish_model,
        ai_originals_model=ai_originals_model,
        ai_voice_review_model=ai_voice_review_model,
        ai_archive_voice_model=ai_archive_voice_model,
        ai_vision_model=ai_vision_model,
        ai_image_model=ai_image_model,
        google_cloud_project=_optional_env("GOOGLE_CLOUD_PROJECT"),
        google_cloud_location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        google_application_credentials=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or None,
        openai_compat_base_url=_optional_env("OPENAI_COMPAT_BASE_URL"),
        openai_compat_api_key=_optional_env("OPENAI_COMPAT_API_KEY"),
        openai_compat_timeout_seconds=_get_int("OPENAI_COMPAT_TIMEOUT_SECONDS", 180),
        vertex_timeout_seconds=_get_int("VERTEX_TIMEOUT_SECONDS", 240),
        vertex_max_retries=_get_int("VERTEX_MAX_RETRIES", 2),
        telegram_bot_token=_optional_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_optional_env("TELEGRAM_CHAT_ID"),
        public_base_url=_optional_env("PUBLIC_BASE_URL"),
        telegram_webapp_enabled_flag=_get_bool("TELEGRAM_WEBAPP_ENABLED", True),
        telegram_legacy_forwarding_enabled_flag=_get_bool("TELEGRAM_LEGACY_FORWARDING_ENABLED", False),
        cloudflared_auto_start=_get_bool("CLOUDFLARED_AUTO_START", False),
        cloudflared_tunnel_mode=str(os.environ.get("CLOUDFLARED_TUNNEL_MODE", "quick") or "quick").strip().lower(),
        style_files=style_files,
        worker=worker,
        sources=sources,
    )
