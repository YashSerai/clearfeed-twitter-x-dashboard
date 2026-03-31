from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from x_signal_dashboard import db
from x_signal_dashboard.config import load_config
from x_signal_dashboard.db import managed_connection
from x_signal_dashboard.service import XAgentService


class _FakeDrafting:
    def propose_voice_update(self, whoami_text: str, voice_text: str, humanizer_text: str, learning_events: list[dict]) -> dict[str, str]:
        return {
            "summary_text": "Approved edits lean punchier and more concrete.",
            "proposed_voice_md": textwrap.dedent(
                """
                # Voice

                ## Voice Snapshot
                - Tone: punchy, concrete, builder-first

                ## Strong Examples
                - Prefer direct edits that keep product specifics.

                ## Active Guardrails
                - CHANGED BY MODEL AND SHOULD NOT SURVIVE
                """
            ).strip(),
        }


class VoiceReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env_backup = os.environ.copy()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.env_backup)

    def _write_repo(self, root: Path) -> None:
        (root / "profiles" / "default").mkdir(parents=True, exist_ok=True)
        (root / "data" / "sources").mkdir(parents=True, exist_ok=True)
        (root / "profiles" / "default" / "WhoAmI.md").write_text("# Who I Am\n- Name: Test User\n", encoding="utf-8")
        (root / "profiles" / "default" / "Voice.md").write_text(
            textwrap.dedent(
                """
                # Voice

                ## Voice Snapshot
                - Tone: thoughtful

                ## Strong Examples
                - Keep examples here.

                ## Active Guardrails

                - Keep `profiles/default/WhoAmI.md` for factual identity, product context, and audience framing.
                - Keep `profiles/default/Voice.md` as the primary long-form voice source.
                - Keep `profiles/default/Humanizer.md` as the last-pass short-form constraint layer.
                - Fill and revise the sections above this block. Do not rewrite this block during normal setup.
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        (root / "profiles" / "default" / "Humanizer.md").write_text("do not change", encoding="utf-8")
        (root / "config.yaml").write_text(
            textwrap.dedent(
                """
                style:
                  files:
                    - profiles/default/WhoAmI.md
                    - profiles/default/Voice.md
                    - profiles/default/Humanizer.md
                worker:
                  min_delay_minutes: 30
                  max_delay_minutes: 30
                  max_candidates_per_cycle: 6
                  candidate_overlap_minutes: 60
                  max_reply_age_minutes: 60
                  article_expand_char_limit: 12000
                  scrape_timeout_ms: 45000
                  recent_signals_limit: 30
                  original_post_options: 1
                  max_original_drafts_per_day: 3
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
                  - key: test
                    label: Test
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

    def _close_service_logger(self, service: XAgentService) -> None:
        handlers = list(service.logger.handlers)
        for handler in handlers:
            handler.close()
            service.logger.removeHandler(handler)

    def test_voice_review_proposal_can_be_created_and_applied(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_repo(root)
            config = load_config(root)
            service = XAgentService(config)
            service.drafting = _FakeDrafting()
            service.bootstrap()

            with managed_connection(config.database_path) as conn:
                draft_1 = db.insert_draft(
                    conn,
                    candidate_id=None,
                    draft_type="reply",
                    draft_text="generated reply one",
                    rationale="reason",
                    model_name="fake-model",
                )
                db.update_draft_text(conn, draft_1, "edited final reply one")
                db.mark_draft_status(conn, draft_1, "approved_local")
                db.record_voice_learning_event(conn, draft_1, "approved_local", "dashboard")

                draft_2 = db.insert_draft(
                    conn,
                    candidate_id=None,
                    draft_type="reply",
                    draft_text="generated reply two",
                    rationale="reason",
                    model_name="fake-model",
                )
                db.mark_draft_status(conn, draft_2, "rejected")
                db.record_voice_learning_event(conn, draft_2, "rejected", "dashboard")

            result = service.maybe_run_voice_review(force=True)
            self.assertEqual(result["status"], "created")

            status = service.voice_review_status()
            self.assertIsNotNone(status["pending"])
            proposal_id = int(status["pending"]["id"])

            apply_result = service.approve_voice_review(proposal_id)
            self.assertIn("Applied voice review proposal", apply_result["message"])

            final_voice = (root / "profiles" / "default" / "Voice.md").read_text(encoding="utf-8")
            self.assertIn("punchy, concrete, builder-first", final_voice)
            self.assertIn("Keep `profiles/default/Humanizer.md` as the last-pass short-form constraint layer.", final_voice)
            self.assertNotIn("CHANGED BY MODEL", final_voice)
            self.assertTrue((root / "profiles" / "history").exists())
            self._close_service_logger(service)

    def test_voice_review_proposal_can_be_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_repo(root)
            config = load_config(root)
            service = XAgentService(config)
            service.drafting = _FakeDrafting()
            service.bootstrap()

            with managed_connection(config.database_path) as conn:
                for idx in range(2):
                    draft_id = db.insert_draft(
                        conn,
                        candidate_id=None,
                        draft_type="reply",
                        draft_text=f"generated reply {idx}",
                        rationale="reason",
                        model_name="fake-model",
                    )
                    db.mark_draft_status(conn, draft_id, "rejected")
                    db.record_voice_learning_event(conn, draft_id, "rejected", "dashboard")

            result = service.maybe_run_voice_review(force=True)
            proposal_id = int(result["proposal_id"])
            reject_result = service.reject_voice_review(proposal_id)
            self.assertIn("Rejected voice review proposal", reject_result["message"])

            status = service.voice_review_status()
            self.assertIsNone(status["pending"])
            self.assertEqual(status["latest"]["status"], "rejected")
            self._close_service_logger(service)


if __name__ == "__main__":
    unittest.main()
