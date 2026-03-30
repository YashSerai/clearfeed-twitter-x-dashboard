from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


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


@dataclass(slots=True)
class AppConfig:
    root: Path
    database_path: Path
    storage_state_path: Path
    timezone: str
    playwright_headless: bool
    google_cloud_project: str
    google_cloud_location: str
    google_application_credentials: str | None
    gemini_text_model: str
    gemini_polish_model: str
    gemini_image_model: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    x_api_key: str | None
    x_api_secret: str | None
    x_access_token: str | None
    x_access_token_secret: str | None
    x_user_id: str | None
    x_bearer_token: str | None
    style_files: list[Path]
    worker: WorkerSettings
    sources: list[SourceConfig]

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def posting_enabled(self) -> bool:
        required = [
            self.x_api_key,
            self.x_api_secret,
            self.x_access_token,
            self.x_access_token_secret,
        ]
        return all(bool(item) for item in required)


def load_config(root: str | Path | None = None) -> AppConfig:
    project_root = Path(root or Path.cwd()).resolve()
    _load_env_file(project_root / ".env")

    cfg = yaml.safe_load((project_root / "config.yaml").read_text(encoding="utf-8"))
    sources_cfg = yaml.safe_load(
        (project_root / "data" / "sources" / "x_sources.yaml").read_text(encoding="utf-8")
    )

    worker = WorkerSettings(**cfg["worker"])
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
            )
        )

    return AppConfig(
        root=project_root,
        database_path=(project_root / os.environ.get("DATABASE_PATH", "./data/marketing.sqlite3")).resolve(),
        storage_state_path=(
            project_root / os.environ.get("PLAYWRIGHT_STORAGE_STATE", "./data/browser/x_storage_state.json")
        ).resolve(),
        timezone=os.environ.get("TIMEZONE", "America/Vancouver"),
        playwright_headless=_get_bool("PLAYWRIGHT_HEADLESS", True),
        google_cloud_project=_require_env("GOOGLE_CLOUD_PROJECT"),
        google_cloud_location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        google_application_credentials=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or None,
        gemini_text_model=os.environ.get("GEMINI_TEXT_MODEL", "gemini-3-flash-preview"),
        gemini_polish_model=os.environ.get("GEMINI_POLISH_MODEL", "gemini-3-flash-preview"),
        gemini_image_model=os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image"),
        telegram_bot_token=_optional_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_optional_env("TELEGRAM_CHAT_ID"),
        x_api_key=_optional_env("X_API_KEY"),
        x_api_secret=_optional_env("X_API_SECRET"),
        x_access_token=_optional_env("X_ACCESS_TOKEN"),
        x_access_token_secret=_optional_env("X_ACCESS_TOKEN_SECRET"),
        x_user_id=_optional_env("X_USER_ID"),
        x_bearer_token=_optional_env("X_USER_ACCESS_BEARER_TOKEN"),
        style_files=style_files,
        worker=worker,
        sources=sources,
    )
