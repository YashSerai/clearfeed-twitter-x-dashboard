from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from x_signal_dashboard.config import load_config


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env_backup = os.environ.copy()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.env_backup)

    def _write_repo(
        self,
        root: Path,
        source_type: str = "list",
        source_url: str = "https://x.com/i/lists/1",
        include_home: bool = False,
    ) -> None:
        (root / "profiles" / "default").mkdir(parents=True, exist_ok=True)
        (root / "data" / "sources").mkdir(parents=True, exist_ok=True)
        (root / "profiles" / "default" / "WhoAmI.md").write_text("name", encoding="utf-8")
        (root / "profiles" / "default" / "Voice.md").write_text("voice", encoding="utf-8")
        (root / "profiles" / "default" / "Humanizer.md").write_text("rules", encoding="utf-8")
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
                """
            ).strip(),
            encoding="utf-8",
        )
        source_yaml = textwrap.dedent(
            f"""
            sources:
              - key: test
                label: Test
                type: {source_type}
                cadence_minutes: 15
                source_weight: 1.0
                weight_env_var: TEST_WEIGHT
                preferred_action: reply
                url: {source_url}
                use_for_original_posts: true
            """
        ).strip()
        if include_home:
            source_yaml += (
                "\n"
                "  - key: home_timeline\n"
                "    label: Home Timeline\n"
                "    type: home\n"
                "    enabled_env_var: HOME_TIMELINE_ENABLED\n"
                "    weight_env_var: HOME_TIMELINE_WEIGHT\n"
                "    cadence_minutes: 10\n"
                "    source_weight: 0.88\n"
                "    preferred_action: reply\n"
                "    use_for_original_posts: false\n"
                "    max_age_minutes: 240\n"
            )
        (root / "data" / "sources" / "x_sources.yaml").write_text(source_yaml, encoding="utf-8")

    def test_optional_integrations_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_repo(root)
            config = load_config(root)
            self.assertFalse(config.telegram_enabled)
            self.assertFalse(config.posting_enabled)
            self.assertFalse(config.drafting_enabled)
            self.assertEqual(len(config.sources), 1)
            self.assertEqual(config.sources[0].source_weight, 1.0)

    def test_source_weight_env_override_and_home_support(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_repo(root, include_home=True)
            os.environ["GOOGLE_CLOUD_PROJECT"] = "demo-project"
            os.environ["TEST_WEIGHT"] = "1.4"
            os.environ["HOME_TIMELINE_ENABLED"] = "true"
            os.environ["HOME_TIMELINE_WEIGHT"] = "0.77"
            config = load_config(root)
            self.assertEqual(len(config.sources), 2)
            self.assertEqual(config.sources[0].source_weight, 1.4)
            self.assertEqual(config.sources[1].type, "home")
            self.assertEqual(config.sources[1].source_weight, 0.77)

    def test_unsupported_source_type_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_repo(root, source_type="mentions")
            os.environ["GOOGLE_CLOUD_PROJECT"] = "demo-project"
            with self.assertRaises(RuntimeError):
                load_config(root)

    def test_missing_profile_files_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_repo(root)
            (root / "profiles" / "default" / "Voice.md").unlink()
            os.environ["GOOGLE_CLOUD_PROJECT"] = "demo-project"
            with self.assertRaises(RuntimeError):
                load_config(root)


if __name__ == "__main__":
    unittest.main()
