from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class TelegramWebAppAuthError(ValueError):
    pass


@dataclass(slots=True)
class TelegramWebAppSession:
    raw: dict[str, str]
    user: dict[str, object]
    chat_type: str | None
    chat_instance: str | None
    start_param: str | None
    auth_date: int


def _coerce_json_value(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 24 * 60 * 60,
    now: int | None = None,
) -> TelegramWebAppSession:
    if not init_data.strip():
        raise TelegramWebAppAuthError(
            "Missing Telegram init data. Open Clearfeed from the Telegram Mini App button inside Telegram, not from a normal browser tab."
        )
    if not bot_token.strip():
        raise TelegramWebAppAuthError("Telegram bot token is not configured.")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "").strip()
    if not received_hash:
        raise TelegramWebAppAuthError("Telegram init data is missing the hash.")

    auth_date_raw = pairs.get("auth_date", "").strip()
    if not auth_date_raw:
        raise TelegramWebAppAuthError("Telegram init data is missing auth_date.")
    try:
        auth_date = int(auth_date_raw)
    except ValueError as exc:
        raise TelegramWebAppAuthError("Telegram init data has an invalid auth_date.") from exc

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise TelegramWebAppAuthError("Telegram init data failed signature verification.")

    current_time = now if now is not None else int(time.time())
    if auth_date > current_time + 30:
        raise TelegramWebAppAuthError("Telegram init data is from the future.")
    if current_time - auth_date > max_age_seconds:
        raise TelegramWebAppAuthError("Telegram init data has expired.")

    user_raw = pairs.get("user", "")
    user_payload = _coerce_json_value(user_raw)
    if not isinstance(user_payload, dict):
        raise TelegramWebAppAuthError("Telegram init data is missing a valid user payload.")

    return TelegramWebAppSession(
        raw={key: value for key, value in pairs.items()},
        user=user_payload,
        chat_type=pairs.get("chat_type") or None,
        chat_instance=pairs.get("chat_instance") or None,
        start_param=pairs.get("start_param") or None,
        auth_date=auth_date,
    )
