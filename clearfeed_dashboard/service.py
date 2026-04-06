from __future__ import annotations

import difflib
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
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from . import db
from .archive_voice import import_archive
from .article_expander import ArticleExpander
from .config import AppConfig, load_config
from .db import managed_connection
from .llm import DraftingEngine
from .scoring import age_minutes, human_age, metrics_summary, score_breakdown
from .scraper import XScraper, normalize_tweet_url
from .style import load_style_packet
from .telegram_api import DisabledTelegramAPI, TelegramAPI, callback_button, inline_keyboard, web_app_button
from .types import SourceConfig


class XAgentService:
    dashboard_draft_text_limit = 0

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
        self.drafting = DraftingEngine(self.config, self.style_packet) if self.config.drafting_enabled else None
        self.article_expander = ArticleExpander(
            char_limit=self.config.worker.article_expand_char_limit,
            storage_state_path=self.config.storage_state_path,
            headless=self.config.playwright_headless,
        )

    def bootstrap(self) -> None:
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
        self._sync_telegram_menu_button()

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
                return {"message": f"Drafted reply #{draft_id}."}
            if action == "draft_quote":
                self._ensure_drafting_enabled()
                draft_id = self._generate_candidate_draft(
                    conn,
                    candidate_id,
                    "quote_reply",
                    notify_telegram=notify_telegram,
                    user_guidance=draft_guidance,
                )
                return {"message": f"Drafted quote reply #{draft_id}."}
            if action == "watch":
                db.set_candidate_status(conn, candidate_id, "watched")
                db.record_event(conn, "candidate", candidate_id, "watch", {"via": "dashboard"})
                return {"message": f"Candidate #{candidate_id} marked watched."}
            if action == "ignore":
                db.set_candidate_status(conn, candidate_id, "ignored")
                db.record_event(conn, "candidate", candidate_id, "ignore", {"via": "dashboard"})
                return {"message": f"Candidate #{candidate_id} ignored."}
        raise ValueError(f"Unknown candidate action: {action}")

    def tweet_url_action(
        self,
        tweet_url: str,
        action: str,
        notify_telegram: bool = True,
        draft_guidance: str | None = None,
        source_channel: str = "dashboard",
    ) -> dict[str, Any]:
        normalized_action = str(action or "").strip()
        if normalized_action not in {"draft_reply", "draft_quote", "queue"}:
            raise ValueError(f"Unknown tweet link action: {action}")
        draft_type = "reply" if normalized_action == "draft_reply" else "quote_reply" if normalized_action == "draft_quote" else None
        if draft_type:
            self._ensure_drafting_enabled()
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            candidate_id = self._import_candidate_from_tweet_url(
                conn,
                tweet_url,
                recommended_action="quote_reply" if draft_type == "quote_reply" else "reply",
                source_channel=source_channel,
            )
            if draft_type:
                draft_id = self._generate_candidate_draft(
                    conn,
                    candidate_id,
                    draft_type,
                    notify_telegram=notify_telegram,
                    user_guidance=draft_guidance,
                )
                label = "quote reply" if draft_type == "quote_reply" else "reply"
                return {
                    "message": f"Drafted {label} #{draft_id}.",
                    "candidate_id": candidate_id,
                    "draft_id": draft_id,
                    "anchor": f"draft-{draft_id}",
                }
            return {
                "message": f"Added tweet as candidate #{candidate_id}.",
                "candidate_id": candidate_id,
                "anchor": f"candidate-{candidate_id}",
            }

    def draft_action(
        self,
        draft_id: int,
        action: str,
        notify_telegram: bool = True,
        draft_text: str | None = None,
    ) -> dict[str, Any]:
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            draft = db.get_draft(conn, draft_id)
            if not draft:
                raise RuntimeError(f"Draft {draft_id} not found")

            normalized_text = self._normalize_dashboard_draft_text(draft_text)
            if (
                normalized_text is not None
                and str(draft["status"] or "") == "drafted"
                and normalized_text != str(draft["draft_text"] or "")
            ):
                db.update_draft_text(conn, draft_id, normalized_text)
                db.record_event(conn, "draft", draft_id, "edit_text", {"via": "dashboard", "autosave": True})
                draft = db.get_draft(conn, draft_id)

            if action in {"approve", "manual"}:
                self._mark_draft_manual(
                    conn,
                    draft_id,
                    source_channel="dashboard",
                    notify_telegram=notify_telegram,
                )
                return {"message": f"Draft #{draft_id} marked as posted."}
            if action == "reject":
                db.mark_draft_status(conn, draft_id, "rejected")
                db.record_event(conn, "draft", draft_id, "reject", {"via": "dashboard"})
                db.record_voice_learning_event(conn, draft_id, "rejected", "dashboard")
                return {"message": f"Draft #{draft_id} rejected."}
            if action == "image":
                image_path = self._generate_draft_image(conn, draft_id, notify_telegram=notify_telegram)
                return {"message": f"Generated image for draft #{draft_id}: {image_path}"}
            if action == "save_text":
                normalized = self._normalize_dashboard_draft_text(draft_text, required=True)
                if normalized != str(draft["draft_text"] or ""):
                    db.update_draft_text(conn, draft_id, normalized)
                    db.record_event(conn, "draft", draft_id, "edit_text", {"via": "dashboard"})
                return {"message": f"Saved draft #{draft_id}."}
        raise ValueError(f"Unknown draft action: {action}")

    def create_original_drafts(
        self,
        topic: str,
        selected_topics: list[dict[str, Any]] | None = None,
        notify_telegram: bool = True,
    ) -> list[int]:
        self._ensure_drafting_enabled()
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            return self._generate_original_post_drafts(
                conn,
                topic,
                selected_topics=selected_topics,
                notify_telegram=notify_telegram,
            )

    def suggest_original_post_topics(self, topic_hint: str = "", limit: int | None = None) -> list[dict[str, str]]:
        self._ensure_drafting_enabled()
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            return self._suggest_original_post_topics(
                conn,
                topic_hint=topic_hint,
                limit=limit or self.config.worker.original_topic_suggestion_limit,
            )

    def import_x_archive(self, archive_dir: str) -> dict[str, Any]:
        archive_path, items, summary_text = import_archive(Path(archive_dir))
        summary_output = self.config.root / "profiles" / "generated" / "ARCHIVE_VOICE.md"
        summary_output.parent.mkdir(parents=True, exist_ok=True)
        summary_output.write_text(summary_text, encoding="utf-8")
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            import_id = db.create_archive_import(conn, str(archive_path), archive_path.name, len(items))
            db.insert_archive_items(conn, import_id, items)
            db.create_archive_voice_summary(
                conn,
                import_id=import_id,
                summary_text=summary_text,
                summary_path=str(summary_output),
                item_count=len(items),
            )
            db.record_event(conn, "archive_import", import_id, "imported", {"item_count": len(items), "path": str(archive_path)})
        return {
            "message": f"Imported {len(items)} archive items from {archive_path.name}.",
            "import_id": import_id,
            "item_count": len(items),
            "summary_path": str(summary_output),
        }

    def maybe_run_archive_voice_build(self) -> dict[str, Any]:
        self._ensure_drafting_enabled()
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            pending = db.get_latest_voice_review_proposal(conn, status="pending", proposal_type="archive")
            if pending:
                return {
                    "status": "pending",
                    "message": f"Archive voice proposal #{pending['id']} is still waiting for approval.",
                    "proposal_id": int(pending["id"]),
                }
            latest_import = db.get_latest_archive_import(conn)
            if not latest_import:
                return {"status": "missing", "message": "Import an X archive before building archive voice."}
            latest_summary = db.get_latest_archive_voice_summary(conn, import_id=int(latest_import["id"]))
            if not latest_summary:
                return {"status": "missing", "message": "No archive summary found for the latest import."}

            preview_rows = db.list_archive_items_preview(conn, int(latest_import["id"]), limit=18)
            archive_examples = [{"kind": str(row["item_kind"]), "text": str(row["text"])} for row in preview_rows]
            current_voice = self._voice_file_path().read_text(encoding="utf-8")
            proposed = self.drafting.propose_archive_voice_update(
                whoami_text=self._whoami_file_path().read_text(encoding="utf-8"),
                voice_text=current_voice,
                humanizer_text=self._humanizer_file_path().read_text(encoding="utf-8"),
                archive_summary_text=str(latest_summary["summary_text"]),
                archive_examples=archive_examples,
            )
            proposal_text = self._preserve_voice_guardrails(
                current_voice=current_voice,
                proposed_voice=proposed["proposed_voice_md"],
            )
            if proposal_text.strip() == current_voice.strip():
                return {"status": "no_change", "message": "Archive voice build found no meaningful Voice.md update."}

            diff_text = "\n".join(
                difflib.unified_diff(
                    current_voice.splitlines(),
                    proposal_text.splitlines(),
                    fromfile=self._display_profile_path(self._voice_file_path()),
                    tofile="profiles/proposals/Voice.archive.proposed.md",
                    lineterm="",
                )
            )
            proposal_id = db.create_voice_review_proposal(
                conn,
                summary_text=proposed["summary_text"] or "Archive import generated a new Voice.md proposal.",
                proposal_text=proposal_text,
                diff_text=diff_text,
                sample_count=int(latest_summary["item_count"]),
                reviewed_until_event_id=0,
                proposal_type="archive",
                source_import_id=int(latest_import["id"]),
            )
            db.record_event(conn, "voice_review", proposal_id, "archive_proposal_created", {"import_id": int(latest_import["id"])})
            return {
                "status": "created",
                "message": f"Created archive voice proposal #{proposal_id}.",
                "proposal_id": proposal_id,
            }

    def archive_voice_status(self) -> dict[str, Any]:
        voice_path = self._voice_file_path()
        current_voice_text = voice_path.read_text(encoding="utf-8") if voice_path.exists() else ""
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            latest_import = db.get_latest_archive_import(conn)
            latest_summary = db.get_latest_archive_voice_summary(conn, import_id=int(latest_import["id"])) if latest_import else None
            pending = db.get_latest_voice_review_proposal(conn, status="pending", proposal_type="archive")
            latest = db.get_latest_voice_review_proposal(conn, proposal_type="archive")
            return {
                "latest_import": (
                    {
                        "id": int(latest_import["id"]),
                        "archive_name": str(latest_import["archive_name"]),
                        "archive_path": str(latest_import["archive_path"]),
                        "item_count": int(latest_import["item_count"]),
                        "imported_at": str(latest_import["imported_at"]),
                    }
                    if latest_import
                    else None
                ),
                "latest_summary": (
                    {
                        "id": int(latest_summary["id"]),
                        "summary_text": str(latest_summary["summary_text"]),
                        "summary_path": str(latest_summary["summary_path"] or ""),
                        "item_count": int(latest_summary["item_count"]),
                        "created_at": str(latest_summary["created_at"]),
                    }
                    if latest_summary
                    else None
                ),
                  "pending": (
                      {
                          "id": int(pending["id"]),
                          "summary_text": str(pending["summary_text"]),
                          "diff_text": str(pending["diff_text"]),
                          "proposal_text": str(pending["proposal_text"]),
                          "sample_count": int(pending["sample_count"]),
                          "created_at": str(pending["created_at"]),
                      }
                      if pending
                      else None
                ),
                "latest": (
                    {
                        "id": int(latest["id"]),
                        "status": str(latest["status"]),
                        "created_at": str(latest["created_at"]),
                        "reviewed_at": str(latest["reviewed_at"] or ""),
                    }
                      if latest
                      else None
                  ),
                  "current_voice_path": self._display_profile_path(voice_path),
                  "current_voice_text": current_voice_text,
              }

    def maybe_run_voice_review(self, force: bool = False) -> dict[str, Any]:
        self._ensure_drafting_enabled()
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            pending = db.get_latest_voice_review_proposal(conn, status="pending", proposal_type="learning")
            if pending:
                return {
                    "status": "pending",
                    "message": f"Voice review proposal #{pending['id']} is still waiting for approval.",
                    "proposal_id": int(pending["id"]),
                }

            latest = db.get_latest_voice_review_proposal(conn, proposal_type="learning")
            reviewed_until_event_id = int(latest["reviewed_until_event_id"]) if latest else 0
            last_review_at = _parse_runtime_datetime(str(latest["created_at"])) if latest and latest["created_at"] else None

            if not force and self.config.worker.voice_review_mode == "manual":
                return {"status": "disabled", "message": "Voice review is set to manual-only mode."}
            if (
                not force
                and last_review_at
                and datetime.now(timezone.utc) - last_review_at
                < timedelta(hours=self.config.worker.voice_review_interval_hours)
            ):
                return {"status": "skipped", "message": "Voice review already ran recently."}

            available_examples = db.count_voice_learning_events_since(conn, reviewed_until_event_id)
            if available_examples < self.config.worker.voice_review_min_examples:
                return {
                    "status": "skipped",
                    "message": (
                        f"Need at least {self.config.worker.voice_review_min_examples} new learning events before "
                        "running voice review."
                    ),
                }

            learning_rows = db.list_latest_voice_learning_events(
                conn,
                after_event_id=reviewed_until_event_id,
                limit=self.config.worker.voice_review_max_examples,
            )
            if len(learning_rows) < self.config.worker.voice_review_min_examples:
                return {"status": "skipped", "message": "Not enough distinct draft decisions to review yet."}

            learning_events = [
                {
                    "id": int(row["id"]),
                    "draft_type": str(row["draft_type"]),
                    "decision": str(row["decision"]),
                    "source_channel": str(row["source_channel"]),
                    "source_text": str(row["source_text"] or ""),
                    "generated_text": str(row["generated_text"]),
                    "final_text": str(row["final_text"]),
                    "was_edited": bool(row["was_edited"]),
                    "generation_notes": str(row["generation_notes"] or ""),
                }
                for row in learning_rows
            ]

            voice_path = self._voice_file_path()
            current_voice = voice_path.read_text(encoding="utf-8")
            proposed = self.drafting.propose_voice_update(
                whoami_text=self._whoami_file_path().read_text(encoding="utf-8"),
                voice_text=current_voice,
                humanizer_text=self._humanizer_file_path().read_text(encoding="utf-8"),
                learning_events=learning_events,
            )
            proposal_text = self._preserve_voice_guardrails(
                current_voice=current_voice,
                proposed_voice=proposed["proposed_voice_md"],
            )
            if proposal_text.strip() == current_voice.strip():
                return {"status": "no_change", "message": "Voice review found no meaningful update to apply."}

            diff_text = "\n".join(
                difflib.unified_diff(
                    current_voice.splitlines(),
                    proposal_text.splitlines(),
                    fromfile=self._display_profile_path(voice_path),
                    tofile="profiles/proposals/Voice.proposed.md",
                    lineterm="",
                )
            )
            proposal_id = db.create_voice_review_proposal(
                conn,
                summary_text=proposed["summary_text"] or "Voice review generated a new proposal.",
                proposal_text=proposal_text,
                diff_text=diff_text,
                sample_count=len(learning_events),
                reviewed_until_event_id=max(int(row["id"]) for row in learning_rows),
                proposal_type="learning",
            )
            db.record_event(conn, "voice_review", proposal_id, "proposal_created", {"sample_count": len(learning_events)})
            return {
                "status": "created",
                "message": f"Created voice review proposal #{proposal_id}.",
                "proposal_id": proposal_id,
            }

    def approve_voice_review(self, proposal_id: int) -> dict[str, Any]:
        return self._apply_voice_proposal(proposal_id, expected_type="learning")

    def approve_archive_voice_proposal(self, proposal_id: int) -> dict[str, Any]:
        return self._apply_voice_proposal(proposal_id, expected_type="archive")

    def reject_voice_review(self, proposal_id: int) -> dict[str, Any]:
        return self._reject_voice_proposal(proposal_id, expected_type="learning")

    def reject_archive_voice_proposal(self, proposal_id: int) -> dict[str, Any]:
        return self._reject_voice_proposal(proposal_id, expected_type="archive")

    def _apply_voice_proposal(self, proposal_id: int, expected_type: str) -> dict[str, Any]:
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            proposal = conn.execute("SELECT * FROM voice_review_proposals WHERE id = ?", (proposal_id,)).fetchone()
            if not proposal:
                raise RuntimeError(f"Voice review proposal {proposal_id} not found.")
            if str(proposal["proposal_type"]) != expected_type:
                raise RuntimeError(f"Proposal #{proposal_id} is not a {expected_type} proposal.")
            if str(proposal["status"]) != "pending":
                raise RuntimeError(f"Voice review proposal #{proposal_id} is already {proposal['status']}.")

            voice_path = self._voice_file_path()
            history_dir = self.config.root / "profiles" / "history"
            history_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            backup_path = history_dir / f"Voice-{timestamp}.md"
            shutil.copy2(voice_path, backup_path)
            voice_path.write_text(str(proposal["proposal_text"]).strip() + "\n", encoding="utf-8")

            db.set_voice_review_proposal_status(conn, proposal_id, "approved", review_notes=f"Backup: {backup_path.name}")
            db.record_event(conn, "voice_review", proposal_id, "proposal_approved", {"backup_path": str(backup_path)})
            proposal_label = "archive voice proposal" if expected_type == "archive" else "voice review proposal"
            return {"message": f"Applied {proposal_label} #{proposal_id}.", "backup_path": str(backup_path)}

    def _reject_voice_proposal(self, proposal_id: int, expected_type: str) -> dict[str, Any]:
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            proposal = conn.execute("SELECT * FROM voice_review_proposals WHERE id = ?", (proposal_id,)).fetchone()
            if not proposal:
                raise RuntimeError(f"Voice review proposal {proposal_id} not found.")
            if str(proposal["proposal_type"]) != expected_type:
                raise RuntimeError(f"Proposal #{proposal_id} is not a {expected_type} proposal.")
            if str(proposal["status"]) != "pending":
                raise RuntimeError(f"Voice review proposal #{proposal_id} is already {proposal['status']}.")
            db.set_voice_review_proposal_status(conn, proposal_id, "rejected")
            db.record_event(conn, "voice_review", proposal_id, "proposal_rejected", {})
            proposal_label = "archive voice proposal" if expected_type == "archive" else "voice review proposal"
            return {"message": f"Rejected {proposal_label} #{proposal_id}."}

    def voice_review_status(self) -> dict[str, Any]:
        voice_path = self._voice_file_path()
        current_voice_text = voice_path.read_text(encoding="utf-8") if voice_path.exists() else ""
        with managed_connection(self.config.database_path) as conn:
            db.bootstrap(conn)
            pending = db.get_latest_voice_review_proposal(conn, status="pending", proposal_type="learning")
            latest = db.get_latest_voice_review_proposal(conn, proposal_type="learning")
            reviewed_until = int(latest["reviewed_until_event_id"]) if latest else 0
            return {
                "enabled": bool(self.config.worker.voice_review_enabled),
                "mode": str(self.config.worker.voice_review_mode),
                "cadence": str(self.config.worker.voice_review_cadence),
                "model_name": str(self.config.ai_voice_review_model),
                  "pending": (
                      {
                          "id": int(pending["id"]),
                          "summary_text": str(pending["summary_text"]),
                          "diff_text": str(pending["diff_text"]),
                          "proposal_text": str(pending["proposal_text"]),
                          "sample_count": int(pending["sample_count"]),
                          "created_at": str(pending["created_at"]),
                      }
                      if pending
                      else None
                ),
                "latest": (
                    {
                        "id": int(latest["id"]),
                        "status": str(latest["status"]),
                        "created_at": str(latest["created_at"]),
                        "reviewed_at": str(latest["reviewed_at"] or ""),
                        "sample_count": int(latest["sample_count"]),
                    }
                      if latest
                      else None
                  ),
                  "new_examples": db.count_voice_learning_events_since(conn, reviewed_until),
                  "current_voice_path": self._display_profile_path(voice_path),
                  "current_voice_text": current_voice_text,
              }

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
                    conn.execute("DELETE FROM runtime_state")
                    conn.execute("DELETE FROM voice_learning_events")
                    conn.execute("DELETE FROM voice_review_proposals")
                    conn.execute("DELETE FROM archive_voice_summaries")
                    conn.execute("DELETE FROM archive_posts")
                    conn.execute("DELETE FROM archive_imports")
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
                    if not self._passes_view_gate(post, source):
                        continue
                    breakdown = score_breakdown(
                        post,
                        source,
                        self.config.worker,
                        author_stats=author_stats.get(post.author_handle.lower(), {}),
                    )
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
                            "topic_relevance": breakdown["topic_relevance"],
                            "creator_fit": breakdown["creator_fit"],
                            "niche_fit": breakdown["niche_fit"],
                            "opportunity_bucket": breakdown["opportunity_bucket"],
                            "off_topic_penalty": breakdown["off_topic_penalty"],
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
                    bucket_bonus = {"core": 8.0, "adjacent": 2.0, "opportunistic": -12.0}.get(
                        item["opportunity_bucket"],
                        0.0,
                    )
                    total_score = round(item["heuristic_score"] * 0.6 + llm_score * 0.4 + action_bonus + bucket_bonus, 2)
                    if total_score < self._min_total_threshold(item["source_type"]):
                        continue
                    db.upsert_candidate(
                        conn,
                        tweet_id=item["tweet_id"],
                        source_key=item["source_key"],
                        heuristic_score=item["heuristic_score"],
                        llm_score=llm_score,
                        total_score=total_score,
                        opportunity_bucket=item["opportunity_bucket"],
                        recommended_action=action,
                        why=why,
                        score_payload={
                            "heuristic_score": item["heuristic_score"],
                            "llm_score": llm_score,
                            "total_score": total_score,
                            "topic_relevance": item["topic_relevance"],
                            "creator_fit": item["creator_fit"],
                            "niche_fit": item["niche_fit"],
                            "off_topic_penalty": item["off_topic_penalty"],
                            "tags": item["score_tags"],
                            "source_type": item["source_type"],
                        },
                    )

                if self.config.telegram_legacy_forwarding_enabled:
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

    def _passes_view_gate(self, post: Any, source: Any) -> bool:
        if source.type not in {"list", "home"}:
            return True
        age = age_minutes(post.posted_at)
        views = int((post.metrics or {}).get("view_count", 0) or 0)
        if getattr(source, "min_view_count", None) is not None:
            threshold_age = float(getattr(source, "min_view_age_minutes", None) or 0)
            if age < threshold_age:
                return True
            if age > self._max_age_minutes_for_source(source):
                return False
            return views >= int(source.min_view_count)
        if source.type == "list":
            return views >= self.config.worker.list_min_views_required
        return views >= self.config.worker.homepage_min_views_required

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
        selected_ids: set[int] = set()
        home_selected = 0
        list_selected = 0
        opportunistic_home_selected = 0

        def can_take_home(row: sqlite3.Row, home_cap: int) -> bool:
            if row["source_key"] != "home_timeline":
                return False
            if home_selected >= home_cap:
                return False
            if row["opportunity_bucket"] != "opportunistic":
                return True
            return opportunistic_home_selected < self.config.worker.homepage_max_opportunistic_alerts_per_cycle

        def take(row: sqlite3.Row) -> None:
            nonlocal home_selected, list_selected, opportunistic_home_selected
            row_id = int(row["id"])
            if row_id in selected_ids:
                return
            selected.append(row)
            selected_ids.add(row_id)
            if row["source_key"] == "home_timeline":
                home_selected += 1
                if row["opportunity_bucket"] == "opportunistic":
                    opportunistic_home_selected += 1
            elif row["source_key"] != "mentions":
                list_selected += 1

        list_rows = [row for row in rows if row["source_key"] not in {"home_timeline", "mentions"}]
        home_rows = [row for row in rows if row["source_key"] == "home_timeline"]
        other_rows = [row for row in rows if row["source_key"] == "mentions"]

        for row in list_rows:
            if len(selected) >= self.config.worker.max_candidates_per_cycle:
                break
            if list_selected >= self.config.worker.list_max_alerts_per_cycle:
                break
            take(row)

        list_shortfall = max(self.config.worker.list_max_alerts_per_cycle - list_selected, 0)
        home_cap = min(
            self.config.worker.max_candidates_per_cycle - len(selected),
            self.config.worker.homepage_max_alerts_per_cycle + list_shortfall,
        )
        for row in home_rows:
            if len(selected) >= self.config.worker.max_candidates_per_cycle:
                break
            if home_selected >= home_cap:
                break
            if not can_take_home(row, home_cap):
                continue
            take(row)

        for row in other_rows:
            if len(selected) >= self.config.worker.max_candidates_per_cycle:
                break
            take(row)

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
                if self.drafting is not None:
                    try:
                        self.maybe_run_voice_review(force=False)
                    except Exception as exc:
                        self.logger.exception("voice review iteration failed: %s", exc)
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
        if text.startswith("/start") or text.startswith("/app"):
            if self.config.telegram_webapp_enabled:
                self.telegram.send_message(
                    "Open the Clearfeed Mini App to review the queue, draft replies, and finish posts.",
                    reply_markup=inline_keyboard([[web_app_button("Open Clearfeed", self._telegram_webapp_url())]]),
                )
            else:
                self.telegram.send_message(
                    "Telegram alerts are enabled, but the Mini App is not ready yet. Set PUBLIC_BASE_URL to your HTTPS tunnel URL to enable it."
                )
            return
        if text.startswith("/status"):
            self.telegram.send_message(self._status_snapshot(conn))
            return
        self.telegram.send_message(
            "Use the Telegram menu button to open Clearfeed.\n\nOptional commands:\n/status\n/app"
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
            if action == "cp":
                self.telegram.safe_answer_callback_query(callback_id, "Open the Mini App to review and copy this draft.")
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
        if media_urls and self.drafting.supports_vision():
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
            model_name=self.config.ai_text_model,
            image_prompt=draft.image_prompt,
            image_reason=draft.image_reason,
            generation_notes=normalized_guidance,
        )
        db.set_candidate_status(conn, candidate_id, "drafted")
        event_payload: dict[str, Any] = {"draft_type": draft_type}
        if normalized_guidance:
            event_payload["generation_notes"] = normalized_guidance
        db.record_event(conn, "draft", draft_id, "created", event_payload)
        if notify_telegram and self.config.telegram_legacy_forwarding_enabled:
            message = self.telegram.send_message(
                self._render_draft_message(conn, draft_id),
                reply_markup=self._draft_keyboard(draft_id, has_image_prompt=bool(draft.image_prompt)),
            )
            db.set_draft_message_id(conn, draft_id, int(message["message_id"]))
        return draft_id

    def _mark_draft_manual(
        self,
        conn: sqlite3.Connection,
        draft_id: int,
        source_channel: str = "dashboard",
        notify_telegram: bool = True,
    ) -> dict[str, str]:
        draft = db.get_draft(conn, draft_id)
        if not draft:
            if notify_telegram and self.config.telegram_legacy_forwarding_enabled:
                self.telegram.send_message(f"Draft {draft_id} not found.")
            raise RuntimeError(f"Draft {draft_id} not found")

        db.mark_draft_status(conn, draft_id, "manual_posted")
        if draft["candidate_id"]:
            db.set_candidate_status(conn, int(draft["candidate_id"]), "manual_posted")
        db.record_event(conn, "draft", draft_id, "manual_posted", {"via": source_channel})
        db.record_voice_learning_event(conn, draft_id, "manual_posted", source_channel)
        if notify_telegram and self.config.telegram_legacy_forwarding_enabled:
            self.telegram.send_message(
                f"Draft #{draft_id} marked as posted."
            )
        return {"status": "manual_posted", "tweet_id": ""}

    def _generate_draft_image(self, conn: sqlite3.Connection, draft_id: int, notify_telegram: bool = True) -> str:
        self._ensure_drafting_enabled()
        draft = db.get_draft(conn, draft_id)
        if not draft:
            if notify_telegram and self.config.telegram_legacy_forwarding_enabled:
                self.telegram.send_message(f"Draft {draft_id} not found.")
            raise RuntimeError(f"Draft {draft_id} not found")
        if not draft["image_prompt"]:
            if notify_telegram and self.config.telegram_legacy_forwarding_enabled:
                self.telegram.send_message("This draft does not have an image suggestion.")
            raise RuntimeError(f"Draft {draft_id} has no image prompt")
        output_path = self.config.root / "data" / "generated" / f"draft_{draft_id}.png"
        self.drafting.generate_image(str(draft["image_prompt"]), output_path)
        db.update_draft_image(conn, draft_id, str(output_path))
        db.record_event(conn, "draft", draft_id, "image_generated", {"path": str(output_path)})
        if notify_telegram and self.config.telegram_legacy_forwarding_enabled:
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
        selected_topics: list[dict[str, Any]] | None = None,
        notify_telegram: bool = True,
    ) -> list[int]:
        self._ensure_drafting_enabled()
        source_keys = [source.key for source in self.config.sources if source.use_for_original_posts]
        normalized_topic = topic.strip()
        normalized_selected_topics = self._normalize_original_topic_selections(selected_topics)
        if normalized_selected_topics:
            if len(normalized_selected_topics) > self.config.worker.original_topics_per_batch:
                raise RuntimeError(
                    f"Select up to {self.config.worker.original_topics_per_batch} timely topics per batch."
                )
            requested_topics = normalized_selected_topics
        elif normalized_topic:
            requested_topics = [
                {
                    "title": "",
                    "why_now": "",
                    "suggested_angle": "",
                    "prompt_seed": normalized_topic,
                }
            ]
        else:
            raise RuntimeError(
                f"Select up to {self.config.worker.original_topics_per_batch} timely topics or write a custom draft brief first."
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
        recent_original_drafts = db.fetch_recent_original_draft_texts(
            conn,
            limit=max(len(requested_topics) * 3, 8),
        )
        draft_ids: list[int] = []
        global_brief = normalized_topic if normalized_selected_topics else ""
        for topic_selection in requested_topics:
            effective_topic = str(topic_selection.get("prompt_seed") or "").strip()
            if global_brief:
                effective_topic = f"{effective_topic}\n\nAdditional direction from the user:\n{global_brief}"
            drafts = self.drafting.generate_original_posts(
                topic=effective_topic,
                signals=signals,
                count=1,
                recent_original_drafts=recent_original_drafts,
            )
            if not drafts:
                continue
            draft = drafts[0]
            generation_note = self._build_original_generation_note(
                topic_title=str(topic_selection.get("title") or "").strip(),
                suggested_angle=str(topic_selection.get("suggested_angle") or "").strip(),
                overall_brief=global_brief if normalized_selected_topics else normalized_topic,
            )
            draft_id = db.insert_draft(
                conn,
                candidate_id=None,
                draft_type="original",
                draft_text=draft.text,
                rationale=draft.rationale,
                model_name=self.config.ai_originals_model,
                image_prompt=draft.image_prompt,
                image_reason=draft.image_reason,
                generation_notes=generation_note,
            )
            draft_ids.append(draft_id)
            recent_original_drafts.insert(0, draft.text)
            if notify_telegram and self.config.telegram_legacy_forwarding_enabled:
                message = self.telegram.send_message(
                    self._render_draft_message(conn, draft_id),
                    reply_markup=self._draft_keyboard(draft_id, has_image_prompt=bool(draft.image_prompt)),
                )
                db.set_draft_message_id(conn, draft_id, int(message["message_id"]))
        if draft_ids:
            db.set_runtime_value(conn, "dashboard.latest_original_batch_ids", json.dumps(draft_ids))
        return draft_ids

    def _suggest_original_post_topics(
        self,
        conn: sqlite3.Connection,
        topic_hint: str,
        limit: int,
    ) -> list[dict[str, str]]:
        source_keys = [source.key for source in self.config.sources if source.use_for_original_posts]
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
        recent_original_drafts = db.fetch_recent_original_draft_texts(conn, limit=max(limit * 2, 8))
        return self.drafting.suggest_original_post_topics(
            topic_hint=topic_hint,
            signals=signals,
            recent_original_drafts=recent_original_drafts,
            limit=limit,
        )

    def _normalize_original_topic_selections(
        self,
        selected_topics: list[dict[str, Any]] | None,
    ) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in selected_topics or []:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            why_now = str(raw.get("why_now") or "").strip()
            suggested_angle = str(raw.get("suggested_angle") or "").strip()
            prompt_seed = str(raw.get("prompt_seed") or title).strip()
            if not prompt_seed:
                continue
            dedupe_key = prompt_seed.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            normalized.append(
                {
                    "title": title,
                    "why_now": why_now,
                    "suggested_angle": suggested_angle,
                    "prompt_seed": prompt_seed,
                }
            )
        return normalized

    def _ensure_drafting_enabled(self) -> None:
        if self.drafting is not None:
            return
        raise RuntimeError(
            "Drafting is not configured. Run scripts/setup.ps1 and fill the selected AI provider settings in .env."
        )

    def _import_candidate_from_tweet_url(
        self,
        conn: sqlite3.Connection,
        tweet_url: str,
        *,
        recommended_action: str,
        source_channel: str,
    ) -> int:
        normalized_url = normalize_tweet_url(tweet_url)
        scraped_post = self.scraper.scrape_tweet_url(normalized_url)
        existing_candidate = db.get_candidate_by_tweet_id(conn, scraped_post.tweet_id)
        existing_post = db.get_scraped_post(conn, scraped_post.tweet_id)

        effective_source_key = str(
            (existing_candidate["source_key"] if existing_candidate else None)
            or (existing_post["source_key"] if existing_post else None)
            or "manual_link"
        )
        effective_source_url = str(
            (existing_post["source_url"] if existing_post and str(existing_post["source_url"] or "").strip() else None)
            or normalized_url
        )

        scraped_post.source_key = effective_source_key
        scraped_post.source_url = effective_source_url
        db.upsert_scraped_post(conn, scraped_post)

        source_config = self._source_config_for_candidate(effective_source_key, effective_source_url)
        score_details = score_breakdown(scraped_post, source_config, self.config.worker)
        heuristic_score = float(score_details.get("score") or 0.0)
        candidate_id = db.upsert_candidate(
            conn,
            tweet_id=scraped_post.tweet_id,
            source_key=effective_source_key,
            heuristic_score=heuristic_score,
            llm_score=0.0,
            total_score=heuristic_score + 250.0,
            opportunity_bucket="manual",
            recommended_action=recommended_action,
            why="Manually imported from a pasted tweet link. Prioritized because you explicitly asked Clearfeed to draft against it.",
            score_payload={"manual_link": True, **score_details},
        )
        db.set_candidate_status(conn, candidate_id, "new")
        db.record_event(
            conn,
            "candidate",
            candidate_id,
            "manual_import",
            {
                "tweet_id": scraped_post.tweet_id,
                "tweet_url": normalized_url,
                "recommended_action": recommended_action,
                "via": source_channel,
            },
        )
        return candidate_id

    def _source_config_for_candidate(self, source_key: str, source_url: str) -> SourceConfig:
        for source in self.config.sources:
            if source.key == source_key:
                return source
        highest_weight = max((source.source_weight for source in self.config.sources), default=1.0)
        return SourceConfig(
            key=source_key,
            label="Manual Link",
            type="list",
            cadence_minutes=0,
            source_weight=highest_weight,
            preferred_action="reply",
            url=source_url,
            use_for_original_posts=False,
            max_age_minutes=max(self.config.worker.max_reply_age_minutes, 60 * 24 * 14),
        )

    def _build_original_generation_note(
        self,
        topic_title: str,
        suggested_angle: str,
        overall_brief: str,
    ) -> str | None:
        parts: list[str] = []
        if topic_title:
            parts.append(f"Topic: {topic_title}")
        if suggested_angle:
            parts.append(f"Angle: {suggested_angle}")
        normalized_brief = overall_brief.strip()
        if normalized_brief:
            label = "Global brief" if topic_title else "Brief"
            parts.append(f"{label}: {normalized_brief}")
        return "\n".join(parts) if parts else None

    def _normalize_dashboard_draft_text(self, draft_text: str | None, required: bool = False) -> str | None:
        if draft_text is None:
            return None
        normalized = draft_text.strip()
        if not normalized:
            if required:
                raise RuntimeError("Draft text cannot be empty.")
            return None
        if self.dashboard_draft_text_limit and len(normalized) > self.dashboard_draft_text_limit:
            raise RuntimeError(
                f"Draft text must be {self.dashboard_draft_text_limit} characters or fewer."
            )
        return normalized

    def _candidate_keyboard(self, candidate_id: int) -> dict[str, Any]:
        if self.config.telegram_webapp_enabled:
            return inline_keyboard(
                [
                    [web_app_button("Open Candidate", self._telegram_webapp_url(candidate_id=candidate_id))],
                    [callback_button("Watch", f"c:wt:{candidate_id}"), callback_button("Ignore", f"c:ig:{candidate_id}")],
                ]
            )
        return inline_keyboard([[callback_button("Watch", f"c:wt:{candidate_id}"), callback_button("Ignore", f"c:ig:{candidate_id}")]])

    def _draft_keyboard(self, draft_id: int, has_image_prompt: bool) -> dict[str, Any]:
        if self.config.telegram_webapp_enabled:
            rows: list[list[dict[str, Any]]] = [
                [web_app_button("Open Draft", self._telegram_webapp_url(draft_id=draft_id))],
            ]
            return inline_keyboard(rows)
        return inline_keyboard([[callback_button("Open Clearfeed", "noop:noop:0")]])

    def _telegram_webapp_url(
        self,
        *,
        candidate_id: int | None = None,
        draft_id: int | None = None,
        view: str | None = None,
    ) -> str:
        base_url = self.config.normalized_public_base_url or ""
        query: dict[str, str] = {}
        if candidate_id is not None:
            query["candidate_id"] = str(candidate_id)
        if draft_id is not None:
            query["draft_id"] = str(draft_id)
        if view:
            query["view"] = view
        if not query:
            return f"{base_url}/mini"
        return f"{base_url}/mini?{urlencode(query)}"

    def _sync_telegram_menu_button(self) -> None:
        if not self.config.telegram_webapp_enabled:
            return
        try:
            self.telegram.set_chat_menu_button("Open Clearfeed", self._telegram_webapp_url())
        except Exception as exc:
            self.logger.warning("telegram webapp menu sync failed: %s", exc)

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
            f"Lane: {row['opportunity_bucket']}\n"
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
            lines.append("Notes:")
            lines.extend(str(draft["generation_notes"]).splitlines())
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

    def _voice_file_path(self) -> Path:
        return self._profile_file_path("Voice.md")

    def _whoami_file_path(self) -> Path:
        return self._profile_file_path("WhoAmI.md")

    def _humanizer_file_path(self) -> Path:
        return self._profile_file_path("Humanizer.md")

    def _profile_file_path(self, filename: str) -> Path:
        local_path = self.config.root / "profiles" / "local" / filename
        if local_path.exists():
            return local_path
        return self.config.root / "profiles" / "default" / filename

    def _display_profile_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.config.root)).replace("\\", "/")
        except ValueError:
            return str(path)

    def _preserve_voice_guardrails(self, current_voice: str, proposed_voice: str) -> str:
        marker = "## Active Guardrails"
        current_clean = current_voice.strip()
        proposed_clean = proposed_voice.strip()
        if marker not in current_clean:
            return proposed_clean
        _current_body, current_guardrails = current_clean.split(marker, 1)
        proposed_body = proposed_clean.split(marker, 1)[0] if marker in proposed_clean else proposed_clean
        merged = proposed_body.rstrip() + "\n\n" + marker + current_guardrails
        return merged.strip()

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
    logger = logging.getLogger("clearfeed_dashboard.worker")
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

