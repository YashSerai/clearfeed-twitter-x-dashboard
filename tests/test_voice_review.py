from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from clearfeed_dashboard import db
from clearfeed_dashboard.config import load_config
from clearfeed_dashboard.db import managed_connection
from clearfeed_dashboard.service import XAgentService


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

    def propose_archive_voice_update(
        self,
        whoami_text: str,
        voice_text: str,
        humanizer_text: str,
        archive_summary_text: str,
        archive_examples: list[dict],
    ) -> dict[str, str]:
        return {
            "summary_text": "The archive shows a sharper, more skeptical builder voice than the current file.",
            "proposed_voice_md": textwrap.dedent(
                """
                # Voice

                ## Voice Snapshot
                - Tone: skeptical, direct, builder-native

                ## Strong Examples
                - Lead with the claim and keep it concrete.

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
        (root / "profiles" / "generated").mkdir(parents=True, exist_ok=True)
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
                draft_1 = db.insert_draft(conn, None, "reply", "generated reply one", "reason", "fake-model")
                db.update_draft_text(conn, draft_1, "edited final reply one")
                db.mark_draft_status(conn, draft_1, "approved_local")
                db.record_voice_learning_event(conn, draft_1, "approved_local", "dashboard")

                draft_2 = db.insert_draft(conn, None, "reply", "generated reply two", "reason", "fake-model")
                db.mark_draft_status(conn, draft_2, "rejected")
                db.record_voice_learning_event(conn, draft_2, "rejected", "dashboard")

            result = service.maybe_run_voice_review(force=True)
            self.assertEqual(result["status"], "created")

            status = service.voice_review_status()
            proposal_id = int(status["pending"]["id"])
            apply_result = service.approve_voice_review(proposal_id)
            self.assertIn("Applied voice review proposal", apply_result["message"])

            final_voice = (root / "profiles" / "default" / "Voice.md").read_text(encoding="utf-8")
            self.assertIn("punchy, concrete, builder-first", final_voice)
            self.assertIn("Keep `profiles/default/Humanizer.md` as the last-pass short-form constraint layer.", final_voice)
            self.assertNotIn("CHANGED BY MODEL", final_voice)
            self._close_service_logger(service)

    def test_archive_voice_import_and_proposal_can_be_applied(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_repo(root)
            archive_root = root / "twitter-test"
            (archive_root / "data").mkdir(parents=True, exist_ok=True)
            (archive_root / "data" / "tweets.js").write_text(
                "window.YTD.tweets.part0 = "
                '[{"tweet":{"full_text":"The real edge is distribution with continuity.","retweeted":false,"in_reply_to_status_id_str":""}},'
                '{"tweet":{"full_text":"Most AI products still feel stateless.","retweeted":false,"in_reply_to_status_id_str":"123"}}];',
                encoding="utf-8",
            )
            config = load_config(root)
            service = XAgentService(config)
            service.drafting = _FakeDrafting()
            service.bootstrap()

            import_result = service.import_x_archive(str(archive_root))
            self.assertEqual(import_result["item_count"], 2)
            self.assertTrue((root / "profiles" / "generated" / "ARCHIVE_VOICE.md").exists())

            build_result = service.maybe_run_archive_voice_build()
            self.assertEqual(build_result["status"], "created")

            archive_status = service.archive_voice_status()
            proposal_id = int(archive_status["pending"]["id"])
            apply_result = service.approve_archive_voice_proposal(proposal_id)
            self.assertIn("Applied archive voice proposal", apply_result["message"])

            final_voice = (root / "profiles" / "default" / "Voice.md").read_text(encoding="utf-8")
            self.assertIn("skeptical, direct, builder-native", final_voice)
            self.assertIn("Keep `profiles/default/Humanizer.md` as the last-pass short-form constraint layer.", final_voice)
            self._close_service_logger(service)


if __name__ == "__main__":
    unittest.main()
