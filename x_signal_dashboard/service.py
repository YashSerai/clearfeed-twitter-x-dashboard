from __future__ import annotations

import json
import logging
import os
import random
import requests
import shutil
import sqlite3
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import db
from .article_expander import ArticleExpander
from .config import AppConfig, load_config
from .db import managed_connection
from .llm import DraftingEngine
from .scoring import age_minutes, human_age, metrics_summary, score_breakdown
from .scraper import XScraper
from .style import load_style_packet
from .telegram_api import DisabledTelegramAPI, TelegramAPI, inline_keyboard
from .x_api import XAPI


class XAgentService:
    dashboard_draft_text_limit = 400

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_config()
        self.logs_dir = self.config.root / "logs"
        self.runtime_dir = self.config.root / "data" / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.logger = _build_logger(self.logs_dir / "worker.log")
        self.status_path = self.runtime_dir / "worker_status.json"
        self.style_packet = load_style_packet(self.config.style_files)
        self.scraper = XScraper(self.config)
        self.telegram = (
            TelegramAPI(self.config.telegram_bot_token, self.config.telegram_chat_id)
            if self.config.telegram_enabled
            else DisabledTelegramAPI()
        )
        self.x_api = XAPI(self.config) if self.config.posting_enabled else None
        self.drafting = DraftingEngine(self.config, self.style_packet) if self.config.drafting_enabled else None
        self.article_expander = ArticleExpander(
            char_limit=self.config.worker.article_expand_char_limit,
            storage_state_path=self.config.storage_state_path,
            headless=self.config.playwright_headless,
        )

    def bootstrap(self) -> None:
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)

    def candidate_action(
        self,
        candidate_id: int,
        action: str,
        notify_telegram: bool = True,
        draft_guidance: str | None = None,
    ) -> dict[str, Any]:
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            if action == "draft_reply":
                self._ensure_drafting_enabled()
                draft_id = self._generate_candidate_draft(
                    conn,
                    candidate_id,
                    "reply",
                    notify_telegram=notify_telegram,
                    user_guidance=draft_guidance,
                )
                return {
                    "message": f"Drafted reply #{draft_id}.",
                    "focus_draft_id": draft_id,
                    "anchor": f"draft-{draft_id}",
                }
            if action == "draft_quote":
                self._ensure_drafting_enabled()
                draft_id = self._generate_candidate_draft(
                    conn,
                    candidate_id,
                    "quote_reply",
                    notify_telegram=notify_telegram,
                    user_guidance=draft_guidance,
                )
                return {
                    "message": f"Drafted quote reply #{draft_id}.",
                    "focus_draft_id": draft_id,
                    "anchor": f"draft-{draft_id}",
                }
            if action == "watch":
                db.set_candidate_status(conn, candidate_id, "watched")
                db.record_event(conn, "candidate", candidate_id, "watch", {"via": "dashboard"})
                return {"message": f"Candidate #{candidate_id} marked watched."}
            if action == "ignore":
                db.set_candidate_status(conn, candidate_id, "ignored")
                db.record_event(conn, "candidate", candidate_id, "ignore", {"via": "dashboard"})
                return {"message": f"Candidate #{candidate_id} ignored."}
        raise ValueError(f"Unknown candidate action: {action}")

    def draft_action(
        self,
        draft_id: int,
        action: str,
        notify_telegram: bool = True,
        draft_text: str | None = None,
    ) -> dict[str, Any]:
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            if action == "approve":
                approval = self._approve_draft(conn, draft_id, notify_telegram=notify_telegram)
                if approval["status"] == "posted":
                    message = f"Posted draft #{draft_id} as tweet {approval['tweet_id']}."
                else:
                    message = f"Approved draft #{draft_id} for local posting."
                return {
                    "message": message,
                    "focus_draft_id": draft_id,
                    "anchor": f"draft-{draft_id}",
                }
            if action == "manual":
                draft = db.get_draft(conn, draft_id)
                if not draft:
                    raise RuntimeError(f"Draft {draft_id} not found")
                db.mark_draft_status(conn, draft_id, "manual_posted")
                if draft["candidate_id"]:
                    db.set_candidate_status(conn, int(draft["candidate_id"]), "manual_posted")
                db.record_event(conn, "draft", draft_id, "manual_posted", {"via": "dashboard"})
                return {
                    "message": f"Draft #{draft_id} marked as manually posted.",
                    "focus_draft_id": draft_id,
                    "anchor": f"draft-{draft_id}",
                }
            if action == "reject":
                db.mark_draft_status(conn, draft_id, "rejected")
                db.record_event(conn, "draft", draft_id, "reject", {"via": "dashboard"})
                return {
                    "message": f"Draft #{draft_id} rejected.",
                    "focus_draft_id": draft_id,
                    "anchor": f"draft-{draft_id}",
                }
            if action == "image":
                image_path = self._generate_draft_image(conn, draft_id, notify_telegram=notify_telegram)
                return {
                    "message": f"Generated image for draft #{draft_id}: {image_path}",
                    "focus_draft_id": draft_id,
                    "anchor": f"draft-{draft_id}",
                }
            if action == "save_text":
                draft = db.get_draft(conn, draft_id)
                if not draft:
                    raise RuntimeError(f"Draft {draft_id} not found")
                normalized = (draft_text or "").strip()
                if not normalized:
                    raise RuntimeError("Draft text cannot be empty.")
                if len(normalized) > self.dashboard_draft_text_limit:
                    raise RuntimeError(
                        f"Draft text must be {self.dashboard_draft_text_limit} characters or fewer."
                    )
                db.update_draft_text(conn, draft_id, normalized)
                db.record_event(conn, "draft", draft_id, "edit_text", {"via": "dashboard"})
                return {
                    "message": f"Saved draft #{draft_id}.",
                    "focus_draft_id": draft_id,
                    "anchor": f"draft-{draft_id}",
                }
        raise ValueError(f"Unknown draft action: {action}")

    def create_original_drafts(self, topic: str, notify_telegram: bool = True) -> list[int]:
        self._ensure_drafting_enabled()
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            return self._generate_original_post_drafts(conn, topic, notify_telegram=notify_telegram)

    def reset_state(self, clear_telegram: bool = True) -> dict[str, int]:
        cleared_messages = 0
        last_exc: Exception | None = None
        for _ in range(6):
            try:
                with managed_connection(self.config.database_path) as conn:
                    db.bootstrap(conn)
                    if clear_telegram and self.config.telegram_enabled:
                        message_rows = conn.execute(
                            """
                            SELECT message_id
                            FROM (
                                SELECT alert_message_id AS message_id FROM candidates WHERE alert_message_id IS NOT NULL
                                UNION
                                SELECT telegram_message_id AS message_id FROM drafts WHERE telegram_message_id IS NOT NULL
                            )
                            ORDER BY message_id DESC
                            """
                        ).fetchall()
                        for row in message_rows:
                            if row["message_id"] and self.telegram.delete_message(int(row["message_id"])):
                                cleared_messages += 1

                    conn.execute("DELETE FROM drafts")
                    conn.execute("DELETE FROM candidates")
                    conn.execute("DELETE FROM scraped_posts")
                    conn.execute("DELETE FROM source_pages")
                    conn.execute("DELETE FROM approval_events")
                    conn.execute("DELETE FROM run_logs")
                    conn.execute("DELETE FROM telegram_state")
                last_exc = None
                break
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if "locked" not in str(exc).lower():
                    raise
                time.sleep(2)
        if last_exc is not None:
            raise last_exc

        generated_dir = self.config.root / "data" / "generated"
        if generated_dir.exists():
            shutil.rmtree(generated_dir, ignore_errors=True)

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._write_status(state="reset", last_error=None)
        self.logger.info("state reset deleted_messages=%s", cleared_messages)
        return {"deleted_messages": cleared_messages}

    def run_cycle(self) -> None:
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            if not self.config.sources:
                raise RuntimeError(
                    "No X list sources are configured. Add list URLs to data/sources/x_sources.yaml or .env first."
                )
            run_id = db.start_run(conn)
            db.set_worker_next_run_at(conn, None)
            db.set_worker_last_run_started_at(conn, db.utc_now_iso())
            db.set_worker_last_run_finished_at(conn, None)
            try:
                posts = self.scraper.scrape_sources(self.config.sources)
                source_map = {source.key: source for source in self.config.sources}
                for source in self.config.sources:
                    db.expire_stale_candidates_for_source(
                        conn,
                        source.key,
                        self._max_age_minutes_for_source(source),
                    )
                for post in posts:
                    db.upsert_scraped_post(conn, post)
                    db.update_source_page(conn, post.source_key, f"Scraped {post.url}")

                author_stats = self._recent_author_stats(conn, source_map)
                scored: list[dict[str, Any]] = []
                for post in posts:
                    if not post.text.strip() and not post.linked_url:
                        continue
                    source = source_map[post.source_key]
                    if age_minutes(post.posted_at) > self._max_age_minutes_for_source(source):
                        continue
                    breakdown = score_breakdown(post, source, author_stats=author_stats.get(post.author_handle.lower(), {}))
                    score = float(breakdown["score"])
                    if score < self._min_heuristic_threshold(source.type):
                        continue
                    scored.append(
                        {
                            "tweet_id": post.tweet_id,
                            "source_key": post.source_key,
                            "source_label": source.label,
                            "source_type": source.type,
                            "preferred_action": source.preferred_action,
                            "author_handle": post.author_handle,
                            "author_name": post.author_name,
                            "text": post.text,
                            "url": post.url,
                            "linked_url": post.linked_url,
                            "metrics": post.metrics,
                            "age_minutes": breakdown["age_minutes"],
                            "score_summary": breakdown["summary"],
                            "score_tags": breakdown["tags"],
                            "social_context": post.raw.get("social_context"),
                            "heuristic_score": score,
                        }
                    )

                ranked = sorted(scored, key=lambda item: item["heuristic_score"], reverse=True)
                llm_input = self._build_llm_pool(ranked)
                llm_map: dict[str, dict[str, Any]] = {}
                if llm_input and self.drafting is not None:
                    try:
                        decisions = self.drafting.prioritize_candidates(llm_input)
                        llm_map = {
                            decision.tweet_id: {
                                "llm_score": decision.llm_score,
                                "recommended_action": decision.recommended_action,
                                "why": decision.why,
                            }
                            for decision in decisions
                        }
                    except Exception as exc:
                        db.record_event(conn, "system", None, "llm_prioritize_error", {"error": str(exc)})

                for item in ranked[:25]:
                    llm = llm_map.get(item["tweet_id"], {})
                    llm_score = float(llm.get("llm_score", 0))
                    why = str(llm.get("why") or item["score_summary"] or f"Fresh {item['source_label']} signal with reply surface.")
                    action = str(llm.get("recommended_action") or item["preferred_action"])
                    action_bonus = 10.0 if action in {"reply", "quote_reply"} else -5.0
                    total_score = round(item["heuristic_score"] * 0.6 + llm_score * 0.4 + action_bonus, 2)
                    if total_score < self._min_total_threshold(item["source_type"]):
                        continue
                    db.upsert_candidate(
                        conn,
                        tweet_id=item["tweet_id"],
                        source_key=item["source_key"],
                        heuristic_score=item["heuristic_score"],
                        llm_score=llm_score,
                        total_score=total_score,
                        recommended_action=action,
                        why=why,
                    )

                if self.config.telegram_enabled:
                    for row in self._select_candidates_for_alerts(
                        db.list_unalerted_candidates(conn, max(self.config.worker.max_candidates_per_cycle * 6, 30))
                    ):
                        message = self.telegram.send_message(
                            self._render_candidate_alert(row),
                            reply_markup=self._candidate_keyboard(int(row["id"])),
                        )
                        db.mark_candidate_alerted(conn, int(row["id"]), int(message["message_id"]))

                db.finish_run(conn, run_id, "success", f"Scraped {len(posts)} posts, kept {len(ranked)} candidates")
                db.set_worker_last_run_finished_at(conn, db.utc_now_iso())
                self.logger.info("cycle success scraped=%s kept=%s alerted=%s", len(posts), len(ranked), min(len(ranked), self.config.worker.max_candidates_per_cycle))
            except Exception as exc:
                db.finish_run(conn, run_id, "failed", str(exc))
                db.set_worker_last_run_finished_at(conn, db.utc_now_iso())
                self.logger.exception("cycle failed")
                raise

    def _recent_author_stats(
        self,
        conn: sqlite3.Connection,
        source_map: dict[str, Any],
    ) -> dict[str, dict[str, int]]:
        stats: dict[str, dict[str, int]] = {}
        for row in db.fetch_recent_author_rows(conn, self.config.worker.author_signal_lookback_hours):
            handle = str(row["author_handle"] or "").lower()
            source_key = str(row["source_key"] or "")
            if not handle:
                continue
            bucket = stats.setdefault(
                handle,
                {"recent_posts": 0, "distinct_sources": 0, "priority_source_hits": 0, "_sources": set()},
            )
            bucket["recent_posts"] += 1
            seen_sources = bucket["_sources"]
            if source_key not in seen_sources:
                seen_sources.add(source_key)
                bucket["distinct_sources"] += 1
            source = source_map.get(source_key)
            if source and source.source_weight >= 0.9:
                bucket["priority_source_hits"] += 1
        for bucket in stats.values():
            bucket.pop("_sources", None)
        return stats

    def _max_age_minutes_for_source(self, source: Any) -> int:
        return int(getattr(source, "max_age_minutes", None) or self.config.worker.max_reply_age_minutes)

    def _min_heuristic_threshold(self, source_type: str) -> float:
        return 52.0 if source_type == "home" else 40.0

    def _min_total_threshold(self, source_type: str) -> float:
        return 62.0 if source_type == "home" else 46.0

    def _build_llm_pool(self, ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
        default_pool_size = max(self.config.worker.max_candidates_per_cycle * 3, 12)
        home_items = [item for item in ranked if item["source_type"] == "home"][: self.config.worker.homepage_llm_pool_size]
        regular_items = [item for item in ranked if item["source_type"] != "home"][:default_pool_size]
        pool: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in home_items + regular_items:
            if item["tweet_id"] in seen:
                continue
            seen.add(item["tweet_id"])
            pool.append(item)
        return pool

    def _select_candidates_for_alerts(self, rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
        selected: list[sqlite3.Row] = []
        home_selected = 0
        for row in rows:
            if len(selected) >= self.config.worker.max_candidates_per_cycle:
                break
            if row["source_key"] == "home_timeline":
                if home_selected >= self.config.worker.homepage_max_alerts_per_cycle:
                    continue
                home_selected += 1
            selected.append(row)
        return selected

    def process_telegram_updates(self) -> None:
        if not self.config.telegram_enabled:
            return
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            offset = db.get_telegram_offset(conn)
            updates = self.telegram.get_updates(offset)
            for update in updates:
                next_offset = int(update["update_id"]) + 1
                if "callback_query" in update:
                    self._handle_callback(conn, update["callback_query"])
                elif "message" in update:
                    self._handle_message(conn, update["message"])
                db.set_telegram_offset(conn, next_offset)

    def run_worker_loop(self) -> None:
        self.bootstrap()
        restored_next_run = self._load_worker_next_run_at()
        self._write_status(
            state="starting",
            next_run_at=restored_next_run.timestamp() if restored_next_run else None,
            last_error=None,
        )
        self.logger.info("worker boot pid=%s", os.getpid())
        if restored_next_run and restored_next_run > datetime.now(timezone.utc):
            sleep_seconds = max(int((restored_next_run - datetime.now(timezone.utc)).total_seconds()), 0)
            self._write_status(state="sleeping", next_run_at=restored_next_run.timestamp(), last_error=None)
            self.logger.info(
                "worker restoring persisted schedule next_run_at=%s sleep_seconds=%s",
                restored_next_run.isoformat(),
                sleep_seconds,
            )
            time.sleep(sleep_seconds)
        while True:
            next_delay = random.randint(
                self.config.worker.min_delay_minutes * 60,
                self.config.worker.max_delay_minutes * 60,
            )
            try:
                self._write_status(state="running", last_error=None)
                self.process_telegram_updates()
                self.run_cycle()
                self.process_telegram_updates()
                next_run_at = datetime.now(timezone.utc) + timedelta(seconds=next_delay)
                self._persist_worker_next_run_at(next_run_at)
                self._write_status(state="sleeping", next_run_at=next_run_at.timestamp(), last_error=None)
                self.logger.info("worker sleeping seconds=%s", next_delay)
            except Exception as exc:
                error_text = f"{type(exc).__name__}: {exc}"
                self.logger.exception("worker iteration failed")
                next_run_at = datetime.now(timezone.utc) + timedelta(seconds=next_delay)
                self._persist_worker_next_run_at(next_run_at)
                self._write_status(
                    state="error",
                    next_run_at=next_run_at.timestamp(),
                    last_error=error_text,
                    traceback_text=traceback.format_exc(),
                )
            time.sleep(next_delay)

    def _handle_message(self, conn: sqlite3.Connection, message: dict[str, Any]) -> None:
        text = (message.get("text") or "").strip()
        if not text:
            return
        if text.startswith("/status"):
            self.telegram.send_message(self._status_snapshot(conn))
            return
        if text.startswith("/post"):
            topic = text[len("/post") :].strip()
            self._generate_original_post_drafts(conn, topic)
            return
        self.telegram.send_message(
            "Commands:\n/status\n/post optional-topic\n\nCandidate alerts will keep arriving automatically."
        )

    def _handle_callback(self, conn: sqlite3.Connection, callback: dict[str, Any]) -> None:
        callback_id = callback["id"]
        data = callback.get("data", "")
        parts = data.split(":")
        if len(parts) != 3:
            self.telegram.safe_answer_callback_query(callback_id, "Unknown action")
            return

        prefix, action, raw_id = parts
        entity_id = int(raw_id)

        if prefix == "c":
            if action == "dr":
                self.telegram.safe_answer_callback_query(callback_id, "Drafting reply")
                self._generate_candidate_draft(conn, entity_id, "reply")
                return
            if action == "dq":
                self.telegram.safe_answer_callback_query(callback_id, "Drafting quote reply")
                self._generate_candidate_draft(conn, entity_id, "quote_reply")
                return
            if action == "wt":
                db.set_candidate_status(conn, entity_id, "watched")
                db.record_event(conn, "candidate", entity_id, "watch", {})
                self.telegram.safe_answer_callback_query(callback_id, "Marked watch")
                return
            if action == "ig":
                db.set_candidate_status(conn, entity_id, "ignored")
                db.record_event(conn, "candidate", entity_id, "ignore", {})
                self.telegram.safe_answer_callback_query(callback_id, "Ignored")
                return

        if prefix == "d":
            if action == "ap":
                self.telegram.safe_answer_callback_query(
                    callback_id,
                    "Posting" if self.config.posting_enabled else "Approving locally",
                )
                self._approve_draft(conn, entity_id)
                return
            if action == "mp":
                draft = db.get_draft(conn, entity_id)
                if draft:
                    db.mark_draft_status(conn, entity_id, "manual_posted")
                    if draft["candidate_id"]:
                        db.set_candidate_status(conn, int(draft["candidate_id"]), "manual_posted")
                    db.record_event(conn, "draft", entity_id, "manual_posted", {"via": "telegram"})
                self.telegram.safe_answer_callback_query(callback_id, "Marked manual")
                return
            if action == "im":
                self.telegram.safe_answer_callback_query(callback_id, "Generating image")
                self._generate_draft_image(conn, entity_id)
                return
            if action == "rj":
                db.mark_draft_status(conn, entity_id, "rejected")
                db.record_event(conn, "draft", entity_id, "reject", {})
                self.telegram.safe_answer_callback_query(callback_id, "Rejected")
                return

        self.telegram.safe_answer_callback_query(callback_id, "No action taken")

    def _generate_candidate_draft(
        self,
        conn: sqlite3.Connection,
        candidate_id: int,
        draft_type: str,
        notify_telegram: bool = True,
        user_guidance: str | None = None,
    ) -> int:
        self._ensure_drafting_enabled()
        candidate = db.get_candidate(conn, candidate_id)
        if not candidate:
            if notify_telegram:
                self.telegram.send_message(f"Candidate {candidate_id} not found.")
            raise RuntimeError(f"Candidate {candidate_id} not found")
        posted_at = _parse_candidate_posted_at(candidate["posted_at"])
        candidate_age_minutes = age_minutes(posted_at)
        stale_note = ""
        if candidate_age_minutes > self.config.worker.max_reply_age_minutes:
            stale_note = (
                f"Saved candidate is older than {self.config.worker.max_reply_age_minutes} minutes. "
                "Drafting anyway because you explicitly asked for it."
            )
        candidate_payload = {
            "tweet_id": candidate["tweet_id"],
            "author_handle": candidate["author_handle"],
            "author_name": candidate["author_name"],
            "text": candidate["text"],
            "url": candidate["url"],
            "linked_url": candidate["linked_url"],
            "recommended_action": candidate["recommended_action"],
            "why": candidate["why"],
            "age": human_age(posted_at),
            "metrics": json.loads(candidate["raw_metrics"]),
        }
        media_urls = _extract_media_urls(candidate)
        tweet_context = None
        try:
            tweet_context = self.article_expander.expand_tweet_context(candidate["url"])
        except Exception as exc:
            db.record_event(conn, "candidate", candidate_id, "tweet_context_error", {"error": str(exc)})
        image_context = None
        if media_urls:
            try:
                image_paths = self._download_tweet_media(candidate["tweet_id"], media_urls)
                if image_paths:
                    image_context = self.drafting.summarize_tweet_images(candidate_payload, image_paths)
            except Exception as exc:
                db.record_event(conn, "candidate", candidate_id, "tweet_image_context_error", {"error": str(exc)})
        article_context = None
        if candidate["linked_url"]:
            try:
                article_context = self.article_expander.expand(candidate["linked_url"])
            except Exception as exc:
                db.record_event(conn, "candidate", candidate_id, "article_expand_error", {"error": str(exc)})

        normalized_guidance = (user_guidance or "").strip() or None
        draft = self.drafting.draft_candidate_reply(
            candidate_payload,
            draft_type,
            tweet_context=tweet_context,
            image_context=image_context,
            article_context=article_context,
            user_guidance=normalized_guidance,
        )
        draft_id = db.insert_draft(
            conn,
            candidate_id=candidate_id,
            draft_type=draft.draft_type,
            draft_text=draft.text,
            rationale="\n\n".join(part for part in [draft.rationale, stale_note] if part),
            model_name=self.config.gemini_text_model,
            image_prompt=draft.image_prompt,
            image_reason=draft.image_reason,
            generation_notes=normalized_guidance,
        )
        db.set_candidate_status(conn, candidate_id, "drafted")
        event_payload: dict[str, Any] = {"draft_type": draft_type}
        if normalized_guidance:
            event_payload["generation_notes"] = normalized_guidance
        db.record_event(conn, "draft", draft_id, "created", event_payload)
        if notify_telegram and self.config.telegram_enabled:
            message = self.telegram.send_message(
                self._render_draft_message(conn, draft_id),
                reply_markup=self._draft_keyboard(draft_id, has_image_prompt=bool(draft.image_prompt)),
            )
            db.set_draft_message_id(conn, draft_id, int(message["message_id"]))
        return draft_id

    def _approve_draft(self, conn: sqlite3.Connection, draft_id: int, notify_telegram: bool = True) -> dict[str, str]:
        draft = db.get_draft(conn, draft_id)
        if not draft:
            if notify_telegram:
                self.telegram.send_message(f"Draft {draft_id} not found.")
            raise RuntimeError(f"Draft {draft_id} not found")

        if not self.config.posting_enabled or self.x_api is None:
            db.mark_draft_status(conn, draft_id, "approved_local")
            if draft["candidate_id"]:
                db.set_candidate_status(conn, int(draft["candidate_id"]), "approved_local")
            db.record_event(conn, "draft", draft_id, "approved_local", {"via": "dashboard"})
            if notify_telegram and self.config.telegram_enabled:
                self.telegram.send_message(f"Draft #{draft_id} approved for local posting.")
            return {"status": "approved_local", "tweet_id": ""}

        media_ids = None
        if draft["image_path"]:
            media_ids = [self.x_api.upload_image(Path(draft["image_path"]))]

        if draft["draft_type"] == "reply":
            response = self.x_api.create_tweet(draft["draft_text"], reply_to_tweet_id=draft["tweet_id"], media_ids=media_ids)
        elif draft["draft_type"] == "quote_reply":
            response = self.x_api.create_tweet(draft["draft_text"], quote_tweet_id=draft["tweet_id"], media_ids=media_ids)
        else:
            response = self.x_api.create_tweet(draft["draft_text"], media_ids=media_ids)

        tweet_id = response["data"]["id"]
        db.mark_draft_status(conn, draft_id, "posted", posted_tweet_id=tweet_id)
        if draft["candidate_id"]:
            db.set_candidate_status(conn, int(draft["candidate_id"]), "posted")
        db.record_event(conn, "draft", draft_id, "posted", {"tweet_id": tweet_id})
        if notify_telegram and self.config.telegram_enabled:
            self.telegram.send_message(
                f"Posted `{draft['draft_type']}` successfully.\nhttps://x.com/{draft['author_handle']}/status/{tweet_id}"
            )
        return {"status": "posted", "tweet_id": tweet_id}

    def _generate_draft_image(self, conn: sqlite3.Connection, draft_id: int, notify_telegram: bool = True) -> str:
        self._ensure_drafting_enabled()
        draft = db.get_draft(conn, draft_id)
        if not draft:
            if notify_telegram:
                self.telegram.send_message(f"Draft {draft_id} not found.")
            raise RuntimeError(f"Draft {draft_id} not found")
        if not draft["image_prompt"]:
            if notify_telegram:
                self.telegram.send_message("This draft does not have an image suggestion.")
            raise RuntimeError(f"Draft {draft_id} has no image prompt")
        output_path = self.config.root / "data" / "generated" / f"draft_{draft_id}.png"
        self.drafting.vertex.generate_image(self.config.gemini_image_model, draft["image_prompt"], output_path)
        db.update_draft_image(conn, draft_id, str(output_path))
        db.record_event(conn, "draft", draft_id, "image_generated", {"path": str(output_path)})
        if notify_telegram and self.config.telegram_enabled:
            result = self.telegram.send_photo(
                caption=self._render_draft_message(conn, draft_id),
                photo_path=output_path,
                reply_markup=self._draft_keyboard(draft_id, has_image_prompt=True),
            )
            db.set_draft_message_id(conn, draft_id, int(result["message_id"]))
        return str(output_path)

    def _generate_original_post_drafts(
        self,
        conn: sqlite3.Connection,
        topic: str,
        notify_telegram: bool = True,
    ) -> list[int]:
        self._ensure_drafting_enabled()
        source_keys = [source.key for source in self.config.sources if source.use_for_original_posts]
        existing_today = db.count_original_drafts_today(conn)
        remaining = max(self.config.worker.max_original_drafts_per_day - existing_today, 0)
        if remaining <= 0:
            raise RuntimeError(
                f"Daily original-draft cap reached ({self.config.worker.max_original_drafts_per_day} per day)."
            )
        signal_rows = db.fetch_recent_posts_for_originals(
            conn,
            source_keys=source_keys,
            limit=self.config.worker.recent_signals_limit,
        )
        signals = [
            {
                "tweet_id": row["tweet_id"],
                "source_key": row["source_key"],
                "author_handle": row["author_handle"],
                "text": row["text"],
                "url": row["url"],
                "linked_url": row["linked_url"],
            }
            for row in signal_rows
        ]
        drafts = self.drafting.generate_original_posts(
            topic=topic,
            signals=signals,
            count=min(self.config.worker.original_post_options, remaining),
        )
        draft_ids: list[int] = []
        for draft in drafts:
            draft_id = db.insert_draft(
                conn,
                candidate_id=None,
                draft_type="original",
                draft_text=draft.text,
                rationale=draft.rationale,
                model_name=self.config.gemini_polish_model,
                image_prompt=draft.image_prompt,
                image_reason=draft.image_reason,
            )
            draft_ids.append(draft_id)
            if notify_telegram and self.config.telegram_enabled:
                message = self.telegram.send_message(
                    self._render_draft_message(conn, draft_id),
                    reply_markup=self._draft_keyboard(draft_id, has_image_prompt=bool(draft.image_prompt)),
                )
                db.set_draft_message_id(conn, draft_id, int(message["message_id"]))
        return draft_ids

    def _ensure_drafting_enabled(self) -> None:
        if self.drafting is not None:
            return
        raise RuntimeError(
            "Drafting is not configured. Set GOOGLE_CLOUD_PROJECT and GOOGLE_APPLICATION_CREDENTIALS in .env."
        )

    def _candidate_keyboard(self, candidate_id: int) -> dict[str, Any]:
        return inline_keyboard(
            [
                [("Draft Reply", f"c:dr:{candidate_id}"), ("Draft Quote", f"c:dq:{candidate_id}")],
                [("Watch", f"c:wt:{candidate_id}"), ("Ignore", f"c:ig:{candidate_id}")],
            ]
        )

    def _draft_keyboard(self, draft_id: int, has_image_prompt: bool) -> dict[str, Any]:
        approve_label = "Post Now" if self.config.posting_enabled else "Approve Draft"
        rows: list[list[tuple[str, str]]] = [[(approve_label, f"d:ap:{draft_id}"), ("Mark Posted", f"d:mp:{draft_id}")]]
        rows.append([("Reject", f"d:rj:{draft_id}")])
        if has_image_prompt:
            rows.append([("Generate Image", f"d:im:{draft_id}")])
        return inline_keyboard(rows)

    def _render_candidate_alert(self, row: sqlite3.Row) -> str:
        excerpt = row["text"].strip()
        if len(excerpt) > 500:
            excerpt = excerpt[:497].rstrip() + "..."
        media_suffix = ""
        if _extract_media_urls(row):
            media_suffix = "\nMedia: attached image(s)"
        return (
            f"Candidate #{row['id']} | {row['source_key']}\n"
            f"@{row['author_handle']} ({row['author_name']})\n"
            f"Age: {human_age(_parse_candidate_posted_at(row['posted_at']))}\n"
            f"Action: {row['recommended_action']}\n"
            f"Score: {row['total_score']:.1f}\n"
            f"Signal: {row['why']}\n"
            f"Metrics: {metrics_summary(row['raw_metrics'])}\n\n"
            f"{excerpt}{media_suffix}\n\n"
            f"{row['url']}"
        )

    def _render_draft_message(self, conn: sqlite3.Connection, draft_id: int) -> str:
        draft = db.get_draft(conn, draft_id)
        if not draft:
            return f"Draft {draft_id} not found."
        lines = [f"Draft #{draft_id} | {draft['draft_type']}"]
        if draft["source_url"]:
            lines.append(f"Source: {draft['source_url']}")
        lines.append("")
        lines.append(draft["draft_text"])
        lines.append("")
        if draft["generation_notes"]:
            lines.append(f"Brief: {draft['generation_notes']}")
            lines.append("")
        lines.append(f"Why: {draft['rationale']}")
        if draft["image_reason"]:
            lines.append(f"Image: {draft['image_reason']}")
        if draft["image_path"]:
            lines.append(f"Image file: {draft['image_path']}")
        return "\n".join(lines)

    def _status_snapshot(self, conn: sqlite3.Connection) -> str:
        counts = {
            row["status"]: row["count"]
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM candidates GROUP BY status").fetchall()
        }
        draft_counts = {
            row["status"]: row["count"]
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM drafts GROUP BY status").fetchall()
        }
        return (
            "System status\n"
            f"Candidates: {counts}\n"
            f"Drafts: {draft_counts}\n"
            f"DB: {self.config.database_path}\n"
            f"Sources: {[source.key for source in self.config.sources]}"
        )

    def _write_status(
        self,
        state: str,
        next_run_at: float | None = None,
        last_error: str | None = None,
        traceback_text: str | None = None,
    ) -> None:
        payload = {
            "pid": os.getpid(),
            "state": state,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "next_run_at": datetime.fromtimestamp(next_run_at, timezone.utc).isoformat() if next_run_at else None,
            "last_error": last_error,
            "traceback": traceback_text,
        }
        self.status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load_worker_next_run_at(self) -> datetime | None:
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            raw_value = db.get_worker_next_run_at(conn)
        return _parse_runtime_datetime(raw_value)

    def _persist_worker_next_run_at(self, next_run_at: datetime | None) -> None:
        raw_value = next_run_at.isoformat() if next_run_at else None
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            db.set_worker_next_run_at(conn, raw_value)

    def _download_tweet_media(self, tweet_id: str, media_urls: list[str]) -> list[Path]:
        media_dir = self.config.root / "data" / "tweet_media" / tweet_id
        media_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        for index, url in enumerate(media_urls[:4], start=1):
            ext = ".jpg"
            if ".png" in url:
                ext = ".png"
            output_path = media_dir / f"image_{index}{ext}"
            response = requests.get(url, timeout=45)
            response.raise_for_status()
            output_path.write_bytes(response.content)
            saved.append(output_path)
        return saved


def _parse_candidate_posted_at(value: str | None):
    if not value:
        return None
    try:
        from dateutil import parser as date_parser

        return date_parser.isoparse(value)
    except Exception:
        return None


def _parse_runtime_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_media_urls(candidate_row_or_dict: Any) -> list[str]:
    raw_json = None
    if isinstance(candidate_row_or_dict, sqlite3.Row):
        raw_json = candidate_row_or_dict["raw_json"] if "raw_json" in candidate_row_or_dict.keys() else None
    elif isinstance(candidate_row_or_dict, dict):
        raw_json = candidate_row_or_dict.get("raw_json")
    if not raw_json:
        return []
    try:
        payload = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except Exception:
        return []
    media_urls = payload.get("media_urls") or []
    return [str(url) for url in media_urls if str(url).strip()]


def _build_logger(path: Path) -> logging.Logger:
    logger = logging.getLogger("x_signal_dashboard.worker")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger
