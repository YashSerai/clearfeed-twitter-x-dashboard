from __future__ import annotations

from pathlib import Path
from typing import Any

import requests
from requests_oauthlib import OAuth1

from .config import AppConfig


class XAPI:
    def __init__(self, config: AppConfig):
        self.config = config
        self.auth = OAuth1(
            config.x_api_key,
            config.x_api_secret,
            config.x_access_token,
            config.x_access_token_secret,
        )

    def create_tweet(
        self,
        text: str,
        reply_to_tweet_id: str | None = None,
        quote_tweet_id: str | None = None,
        media_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text}
        if reply_to_tweet_id:
            payload["reply"] = {"in_reply_to_tweet_id": reply_to_tweet_id}
        if quote_tweet_id:
            payload["quote_tweet_id"] = quote_tweet_id
        if media_ids:
            payload["media"] = {"media_ids": media_ids}
        response = requests.post("https://api.x.com/2/tweets", auth=self.auth, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def upload_image(self, image_path: Path) -> str:
        with image_path.open("rb") as handle:
            response = requests.post(
                "https://upload.x.com/1.1/media/upload.json",
                auth=self.auth,
                files={"media": handle},
                timeout=60,
            )
        response.raise_for_status()
        payload = response.json()
        media_id = payload.get("media_id_string")
        if not media_id:
            raise RuntimeError(f"Upload failed: {payload}")
        return media_id
