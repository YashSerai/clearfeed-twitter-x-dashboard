from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from clearfeed_dashboard import db
from clearfeed_dashboard.config import load_config
from clearfeed_dashboard.dashboard import (
    _mini_bootstrap_payload,
    _mini_candidate_action,
    _mini_draft_action,
    _mini_original_action,
    _mini_original_topics_action,
    _render_mini_app,
)
from clearfeed_dashboard.scraper import normalize_tweet_url
from clearfeed_dashboard.db import managed_connection
from clearfeed_dashboard.service import XAgentService
from clearfeed_dashboard.telegram_webapp import TelegramWebAppAuthError, validate_init_data
from clearfeed_dashboard.types import DraftPayload, ScrapedPost


class _FakeDrafting:
    def supports_vision(self) -> bool:
        return False

    def supports_web_search(self) -> bool:
        return False

    def draft_candidate_reply(
        self,
        candidate_payload: dict[str, object],
        draft_type: str,
        tweet_context: object | None = None,
        image_context: object | None = None,
        article_context: object | None = None,
        user_guidance: str | None = None,
    ) -> DraftPayload:
        suffix = f" | brief={user_guidance}" if user_guidance else ""
        return DraftPayload(
            draft_type=draft_type,
            text=f"{draft_type} draft for @{candidate_payload['author_handle']}{suffix}",
            rationale="Test rationale",
            image_prompt="A crisp product mockup",
            image_reason="Visualize the idea",
        )

    def generate_image(self, prompt: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-image")

    def generate_original_posts(
        self,
        topic: str,
        signals: list[dict[str, object]],
        count: int,
        recent_original_drafts: list[str] | None = None,
    ) -> list[DraftPayload]:
        _ = recent_original_drafts
        return [
            DraftPayload(
                draft_type="original",
                text=f"Original post about {topic or 'signals'}",
                rationale="Test original rationale",
                image_prompt="Bold product illustration",
                image_reason="Support the original post",
            )
            for _ in range(count)
        ]

    def suggest_original_post_topics(
        self,
        topic_hint: str,
        signals: list[dict[str, object]],
        recent_original_drafts: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, str]]:
        _ = signals
        _ = recent_original_drafts
        return [
            {
                "title": f"Topic {index + 1}",
                "why_now": "Fresh conversation in the signal pool.",
                "suggested_angle": "Explain what builders should notice.",
                "prompt_seed": f"{topic_hint or 'AI distribution'} | angle: explain what builders should notice",
            }
            for index in range(limit)
        ]


class _FakeTweetLinkScraper:
    def scrape_tweet_url(self, url: str) -> ScrapedPost:
        return ScrapedPost(
            tweet_id="tweet-link-1",
            source_key="manual_link",
            source_url=url,
            author_handle="builder",
            author_name="Builder",
            text="Threading concrete tradeoffs beats vague takes.",
            posted_at=datetime.now(timezone.utc),
            url=url,
            linked_url="https://example.com/post",
            metrics={"view_count": 44, "like_count": 11, "reply_count": 4, "repost_count": 1},
            raw={"media_urls": []},
        )


class TelegramMiniAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env_backup = os.environ.copy()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.env_backup)

    def _write_repo(self, root: Path) -> None:
        (root / "profiles" / "default").mkdir(parents=True, exist_ok=True)
        (root / "data" / "sources").mkdir(parents=True, exist_ok=True)
        (root / "profiles" / "default" / "WhoAmI.md").write_text("who", encoding="utf-8")
        (root / "profiles" / "default" / "Voice.md").write_text("voice", encoding="utf-8")
        (root / "profiles" / "default" / "Humanizer.md").write_text("humanizer", encoding="utf-8")
        (root / "config.yaml").write_text(
            textwrap.dedent(
                """
                style:
                  files:
                    - profiles/default/WhoAmI.md
                    - profiles/default/Voice.md
                    - profiles/default/Humanizer.md
                worker:
                  min_delay_minutes: 25
                  max_delay_minutes: 35
                  max_candidates_per_cycle: 6
                  candidate_overlap_minutes: 60
                  max_reply_age_minutes: 60
                  article_expand_char_limit: 12000
                  scrape_timeout_ms: 45000
                  recent_signals_limit: 30
                  original_post_options: 1
                  original_topics_per_batch: 3
                  max_original_drafts_per_day: 3
                  original_topic_suggestion_limit: 5
                  default_image_mode: suggest_only
                  homepage_scrape_limit: 0
                  homepage_llm_pool_size: 0
                  homepage_max_alerts_per_cycle: 0
                  author_signal_lookback_hours: 72
                  voice_review_enabled: true
                  voice_review_interval_hours: 24
                  voice_review_min_examples: 2
                  voice_review_max_examples: 8
                """
            ).strip(),
            encoding="utf-8",
        )
        (root / "data" / "sources" / "x_sources.yaml").write_text(
            textwrap.dedent(
                """
                sources:
                  - key: list_a
                    label: List A
                    type: list
                    cadence_minutes: 15
                    source_weight: 1.0
                    preferred_action: reply
                    url: https://x.com/i/lists/1
                    use_for_original_posts: true
                """
            ).strip(),
            encoding="utf-8",
        )

    def _build_service(self, root: Path) -> XAgentService:
        os.environ["AI_PROVIDER"] = "openai_compatible"
        os.environ["OPENAI_COMPAT_BASE_URL"] = "http://127.0.0.1:11434/v1"
        os.environ["AI_TEXT_MODEL"] = "test-text"
        os.environ["AI_POLISH_MODEL"] = "test-polish"
        os.environ["AI_IMAGE_MODEL"] = "test-image"
        os.environ["TELEGRAM_BOT_TOKEN"] = "123:abc"
        os.environ["TELEGRAM_CHAT_ID"] = "456"
        os.environ["TELEGRAM_WEBAPP_ENABLED"] = "true"
        os.environ["PUBLIC_BASE_URL"] = "https://example.trycloudflare.com"
        config = load_config(root)
        service = XAgentService(config)
        service.drafting = _FakeDrafting()
        service.telegram.send_message = lambda text, reply_markup=None: {"message_id": 1, "text": text}
        service.telegram.send_photo = (
            lambda caption, photo_path, reply_markup=None: {"message_id": 1, "caption": caption, "photo_path": str(photo_path)}
        )
        service.telegram.set_chat_menu_button = lambda text, web_app_url, chat_id=None: {"ok": True}
        service.bootstrap()
        return service

    def _close_service_logger(self, service: XAgentService) -> None:
        for handler in list(service.logger.handlers):
            handler.close()
            service.logger.removeHandler(handler)

    def _insert_candidate(self, service: XAgentService) -> int:
        with managed_connection(service.config.database_path) as conn:
            db.bootstrap(conn)
            db.upsert_scraped_post(
                conn,
                ScrapedPost(
                    tweet_id="tweet-1",
                    source_key="list_a",
                    source_url="https://x.com/i/lists/1",
                    author_handle="builder",
                    author_name="Builder",
                    text="A sharp point about distribution.",
                    posted_at=datetime.now(timezone.utc),
                    url="https://x.com/builder/status/1",
                    linked_url=None,
                    metrics={"view_count": 12, "like_count": 3, "reply_count": 1, "repost_count": 0},
                    raw={"media": []},
                ),
            )
            return db.upsert_candidate(
                conn,
                tweet_id="tweet-1",
                source_key="list_a",
                heuristic_score=55.0,
                llm_score=61.0,
                total_score=63.0,
                opportunity_bucket="core",
                recommended_action="reply",
                why="Useful test signal",
            )

    def _signed_init_data(self, bot_token: str, *, auth_date: int = 1_700_000_000) -> str:
        payload = {
            "auth_date": str(auth_date),
            "chat_instance": "ci-1",
            "chat_type": "private",
            "query_id": "q-1",
            "start_param": "candidate-1",
            "user": json.dumps({"id": 99, "first_name": "Test", "username": "tester"}, separators=(",", ":")),
        }
        data_check_string = "\n".join(f"{key}={payload[key]}" for key in sorted(payload))
        secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
        payload["hash"] = hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        return urlencode(payload)

    def test_validate_init_data_accepts_valid_signature(self) -> None:
        init_data = self._signed_init_data("123:abc")
        session = validate_init_data(init_data, "123:abc", now=1_700_000_100)
        self.assertEqual(int(session.user["id"]), 99)
        self.assertEqual(session.chat_type, "private")

    def test_validate_init_data_rejects_expired_payload(self) -> None:
        init_data = self._signed_init_data("123:abc", auth_date=1_700_000_000)
        with self.assertRaises(TelegramWebAppAuthError):
            validate_init_data(init_data, "123:abc", now=1_700_100_000)

    def test_normalize_tweet_url_accepts_x_and_twitter_domains(self) -> None:
        self.assertEqual(
            normalize_tweet_url("twitter.com/builder/status/1234567890?t=abc"),
            "https://x.com/builder/status/1234567890",
        )

    def test_mini_app_html_preserves_unsaved_editor_state_during_rerenders(self) -> None:
        page = _render_mini_app()
        self.assertIn("localCandidateBriefs", page)
        self.assertIn("localDraftTexts", page)
        self.assertIn("snapshotActiveEditor", page)
        self.assertIn("restoreActiveEditor", page)
        self.assertIn("pruneLocalEditorState", page)

    def test_tweet_url_action_imports_manual_candidate_and_creates_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_repo(root)
            service = self._build_service(root)
            service.scraper = _FakeTweetLinkScraper()
            try:
                result = service.tweet_url_action(
                    "twitter.com/builder/status/1234567890?ref_src=twsrc",
                    "draft_reply",
                    notify_telegram=False,
                    draft_guidance="Lead with the tradeoff, then make it practical.",
                )
                self.assertIn("Drafted reply", result["message"])

                with managed_connection(service.config.database_path) as conn:
                    candidate = conn.execute(
                        """
                        SELECT c.status, c.source_key, c.recommended_action, s.url, s.author_handle
                        FROM candidates c
                        JOIN scraped_posts s ON s.tweet_id = c.tweet_id
                        ORDER BY c.id DESC
                        LIMIT 1
                        """
                    ).fetchone()
                    self.assertEqual(str(candidate["status"]), "drafted")
                    self.assertEqual(str(candidate["source_key"]), "manual_link")
                    self.assertEqual(str(candidate["recommended_action"]), "reply")
                    self.assertEqual(str(candidate["url"]), "https://x.com/builder/status/1234567890")
                    self.assertEqual(str(candidate["author_handle"]), "builder")

                    draft = conn.execute(
                        "SELECT draft_type, generation_notes, draft_text FROM drafts ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    self.assertEqual(str(draft["draft_type"]), "reply")
                    self.assertEqual(
                        str(draft["generation_notes"]),
                        "Lead with the tradeoff, then make it practical.",
                    )
                    self.assertIn("brief=Lead with the tradeoff, then make it practical.", str(draft["draft_text"]))
            finally:
                self._close_service_logger(service)

    def test_mini_app_actions_share_dashboard_service_logic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_repo(root)
            service = self._build_service(root)
            try:
                candidate_id = self._insert_candidate(service)

                create_result = _mini_candidate_action(
                    service,
                    {
                        "candidate_id": candidate_id,
                        "action": "draft_reply",
                        "draft_guidance": "Lead with the objection, then make it concrete.",
                    },
                )
                self.assertIn("Drafted reply", create_result["message"])

                with managed_connection(service.config.database_path) as conn:
                    draft = conn.execute("SELECT * FROM drafts ORDER BY id DESC LIMIT 1").fetchone()
                    self.assertIsNotNone(draft)
                    self.assertEqual(str(draft["generation_notes"]), "Lead with the objection, then make it concrete.")
                    draft_id = int(draft["id"])

                save_result = _mini_draft_action(
                    service,
                    {
                        "draft_id": draft_id,
                        "action": "save_text",
                        "draft_text": "Edited final draft text",
                    },
                )
                self.assertIn("Saved draft", save_result["message"])

                image_result = _mini_draft_action(
                    service,
                    {
                        "draft_id": draft_id,
                        "action": "image",
                        "draft_text": "Edited final draft text",
                    },
                )
                self.assertIn("Generated image", image_result["message"])

                manual_result = _mini_draft_action(
                    service,
                    {
                        "draft_id": draft_id,
                        "action": "manual",
                        "draft_text": "Edited final draft text",
                    },
                )
                self.assertIn("marked as posted", manual_result["message"])

                original_result = _mini_original_action(
                    service,
                    {
                        "topic": "Make each draft practical for builders.",
                        "selected_topics": [
                            {
                                "title": "Topic 1",
                                "why_now": "Fresh conversation in the signal pool.",
                                "suggested_angle": "Explain what builders should notice.",
                                "prompt_seed": "AI distribution | angle: explain what builders should notice",
                            },
                            {
                                "title": "Topic 2",
                                "why_now": "Fresh conversation in the signal pool.",
                                "suggested_angle": "Call out the market implication.",
                                "prompt_seed": "Model launches | angle: call out the market implication",
                            },
                        ],
                    },
                )
                self.assertIn("Created 2 original draft", original_result["message"])

                topics_result = _mini_original_topics_action(service, {"topic_hint": "OpenAI model launches"})
                self.assertEqual(len(topics_result["topic_suggestions"]), 5)
                self.assertIn("Found 5 timely topic", topics_result["message"])

                payload = _mini_bootstrap_payload(service, focus_candidate_id=candidate_id, focus_draft_id=draft_id)
                self.assertEqual(payload["focus"]["candidate_id"], candidate_id)
                self.assertEqual(payload["focus"]["draft_id"], draft_id)
                self.assertTrue(payload["app"]["telegram_webapp_enabled"])
                self.assertGreaterEqual(len(payload["original_drafts"]), 2)
                self.assertEqual(payload["queue"], [])
                with managed_connection(service.config.database_path) as conn:
                    persisted = conn.execute("SELECT status, draft_text FROM drafts WHERE id = ?", (draft_id,)).fetchone()
                    self.assertEqual(str(persisted["status"]), "manual_posted")
                    self.assertEqual(str(persisted["draft_text"]), "Edited final draft text")
                    original_draft_count = conn.execute(
                        "SELECT COUNT(*) AS c FROM drafts WHERE draft_type = 'original'"
                    ).fetchone()
                    self.assertEqual(int(original_draft_count["c"]), 2)
                self.assertTrue(Path(root / "data" / "generated" / f"draft_{draft_id}.png").exists())
            finally:
                self._close_service_logger(service)


if __name__ == "__main__":
    unittest.main()
