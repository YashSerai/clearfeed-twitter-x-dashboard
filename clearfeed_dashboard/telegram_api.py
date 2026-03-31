from __future__ import annotations

from pathlib import Path
from typing import Any

import requests
from requests import HTTPError


class TelegramAPI:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def send_message(self, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": False}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        response = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=30)
        response.raise_for_status()
        return response.json()["result"]

    def send_photo(self, caption: str, photo_path: Path, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        with photo_path.open("rb") as handle:
            response = requests.post(
                f"{self.base_url}/sendPhoto",
                data={"chat_id": self.chat_id, "caption": caption},
                files={"photo": handle},
                timeout=60,
            )
        response.raise_for_status()
        result = response.json()["result"]
        if reply_markup:
            requests.post(
                f"{self.base_url}/editMessageReplyMarkup",
                json={
                    "chat_id": self.chat_id,
                    "message_id": result["message_id"],
                    "reply_markup": reply_markup,
                },
                timeout=30,
            ).raise_for_status()
        return result

    def get_updates(self, offset: int) -> list[dict[str, Any]]:
        response = requests.get(
            f"{self.base_url}/getUpdates",
            params={"offset": offset, "timeout": 0},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["result"]

    def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        requests.post(
            f"{self.base_url}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=30,
        ).raise_for_status()

    def safe_answer_callback_query(self, callback_query_id: str, text: str) -> None:
        try:
            self.answer_callback_query(callback_query_id, text)
        except HTTPError:
            return

    def delete_message(self, message_id: int) -> bool:
        response = requests.post(
            f"{self.base_url}/deleteMessage",
            json={"chat_id": self.chat_id, "message_id": message_id},
            timeout=30,
        )
        if response.status_code == 200:
            return True
        return False


class DisabledTelegramAPI:
    enabled = False

    def send_message(self, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"message_id": 0, "text": text, "disabled": True}

    def send_photo(self, caption: str, photo_path: Path, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"message_id": 0, "caption": caption, "photo_path": str(photo_path), "disabled": True}

    def get_updates(self, offset: int) -> list[dict[str, Any]]:
        return []

    def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        return

    def safe_answer_callback_query(self, callback_query_id: str, text: str) -> None:
        return

    def delete_message(self, message_id: int) -> bool:
        return False


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": callback_data} for label, callback_data in row] for row in rows
        ]
    }
