from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .types import ScrapedPost


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def managed_connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(database_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def bootstrap(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS run_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS source_pages (
            source_key TEXT PRIMARY KEY,
            last_scraped_at TEXT,
            last_seen_tweet_at TEXT,
            last_run_notes TEXT
        );

        CREATE TABLE IF NOT EXISTS scraped_posts (
            tweet_id TEXT PRIMARY KEY,
            source_key TEXT NOT NULL,
            source_url TEXT NOT NULL,
            author_handle TEXT NOT NULL,
            author_name TEXT NOT NULL,
            text TEXT NOT NULL,
            posted_at TEXT,
            url TEXT NOT NULL,
            linked_url TEXT,
            raw_metrics TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tweet_id TEXT NOT NULL UNIQUE,
            source_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            heuristic_score REAL NOT NULL,
            llm_score REAL NOT NULL DEFAULT 0,
            total_score REAL NOT NULL,
            opportunity_bucket TEXT NOT NULL DEFAULT 'core',
            recommended_action TEXT NOT NULL,
            why TEXT NOT NULL,
            score_json TEXT,
            alert_message_id INTEGER,
            alert_sent_at TEXT,
            last_updated_at TEXT NOT NULL,
            FOREIGN KEY(tweet_id) REFERENCES scraped_posts(tweet_id)
        );

        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER,
            draft_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'drafted',
            draft_text TEXT NOT NULL,
            original_draft_text TEXT,
            rationale TEXT NOT NULL,
            image_prompt TEXT,
            image_reason TEXT,
            image_path TEXT,
            model_name TEXT,
            telegram_message_id INTEGER,
            posted_tweet_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(candidate_id) REFERENCES candidates(id)
        );

        CREATE TABLE IF NOT EXISTS approval_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            action TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS telegram_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runtime_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS voice_learning_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id INTEGER NOT NULL,
            draft_type TEXT NOT NULL,
            decision TEXT NOT NULL,
            source_channel TEXT NOT NULL,
            source_tweet_id TEXT,
            source_url TEXT,
            source_text TEXT,
            generated_text TEXT NOT NULL,
            final_text TEXT NOT NULL,
            was_edited INTEGER NOT NULL DEFAULT 0,
            rationale TEXT,
            generation_notes TEXT,
            model_name TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(draft_id) REFERENCES drafts(id)
        );

        CREATE TABLE IF NOT EXISTS voice_review_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL DEFAULT 'pending',
            proposal_type TEXT NOT NULL DEFAULT 'learning',
            summary_text TEXT NOT NULL,
            proposal_text TEXT NOT NULL,
            diff_text TEXT NOT NULL,
            sample_count INTEGER NOT NULL DEFAULT 0,
            reviewed_until_event_id INTEGER NOT NULL DEFAULT 0,
            source_import_id INTEGER,
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            review_notes TEXT
        );

        CREATE TABLE IF NOT EXISTS archive_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            archive_path TEXT NOT NULL,
            archive_name TEXT NOT NULL,
            item_count INTEGER NOT NULL DEFAULT 0,
            latest INTEGER NOT NULL DEFAULT 0,
            imported_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS archive_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            item_kind TEXT NOT NULL,
            text TEXT NOT NULL,
            normalized_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(import_id, normalized_text),
            FOREIGN KEY(import_id) REFERENCES archive_imports(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS archive_voice_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            summary_text TEXT NOT NULL,
            summary_path TEXT,
            item_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(import_id) REFERENCES archive_imports(id) ON DELETE CASCADE
        );
        """
    )
    draft_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(drafts)").fetchall()}
    if "generation_notes" not in draft_columns:
        conn.execute("ALTER TABLE drafts ADD COLUMN generation_notes TEXT")
    if "original_draft_text" not in draft_columns:
        conn.execute("ALTER TABLE drafts ADD COLUMN original_draft_text TEXT")
        conn.execute("UPDATE drafts SET original_draft_text = draft_text WHERE original_draft_text IS NULL")
    proposal_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(voice_review_proposals)").fetchall()}
    if "proposal_type" not in proposal_columns:
        conn.execute("ALTER TABLE voice_review_proposals ADD COLUMN proposal_type TEXT NOT NULL DEFAULT 'learning'")
        conn.execute("UPDATE voice_review_proposals SET proposal_type = 'learning' WHERE proposal_type IS NULL OR proposal_type = ''")
    if "source_import_id" not in proposal_columns:
        conn.execute("ALTER TABLE voice_review_proposals ADD COLUMN source_import_id INTEGER")
    candidate_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(candidates)").fetchall()}
    if "opportunity_bucket" not in candidate_columns:
        conn.execute("ALTER TABLE candidates ADD COLUMN opportunity_bucket TEXT NOT NULL DEFAULT 'core'")
    if "score_json" not in candidate_columns:
        conn.execute("ALTER TABLE candidates ADD COLUMN score_json TEXT")


def start_run(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "INSERT INTO run_logs(started_at, status) VALUES (?, ?)",
        (utc_now_iso(), "running"),
    )
    return int(cursor.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, notes: str = "") -> None:
    conn.execute(
        "UPDATE run_logs SET finished_at = ?, status = ?, notes = ? WHERE id = ?",
        (utc_now_iso(), status, notes, run_id),
    )


def upsert_scraped_post(conn: sqlite3.Connection, post: ScrapedPost) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO scraped_posts(
            tweet_id, source_key, source_url, author_handle, author_name, text, posted_at, url,
            linked_url, raw_metrics, raw_json, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tweet_id) DO UPDATE SET
            source_key=excluded.source_key,
            source_url=excluded.source_url,
            author_handle=excluded.author_handle,
            author_name=excluded.author_name,
            text=excluded.text,
            posted_at=excluded.posted_at,
            url=excluded.url,
            linked_url=excluded.linked_url,
            raw_metrics=excluded.raw_metrics,
            raw_json=excluded.raw_json,
            last_seen_at=excluded.last_seen_at
        """,
        (
            post.tweet_id,
            post.source_key,
            post.source_url,
            post.author_handle,
            post.author_name,
            post.text,
            post.posted_at.isoformat() if post.posted_at else None,
            post.url,
            post.linked_url,
            json.dumps(post.metrics),
            json.dumps(post.raw),
            now,
            now,
        ),
    )


def update_source_page(conn: sqlite3.Connection, source_key: str, notes: str = "") -> None:
    conn.execute(
        """
        INSERT INTO source_pages(source_key, last_scraped_at, last_run_notes)
        VALUES (?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            last_scraped_at=excluded.last_scraped_at,
            last_run_notes=excluded.last_run_notes
        """,
        (source_key, utc_now_iso(), notes),
    )


def upsert_candidate(
    conn: sqlite3.Connection,
    tweet_id: str,
    source_key: str,
    heuristic_score: float,
    llm_score: float,
    total_score: float,
    opportunity_bucket: str,
    recommended_action: str,
    why: str,
    score_payload: dict[str, Any] | None = None,
) -> int:
    conn.execute(
        """
        INSERT INTO candidates(
            tweet_id, source_key, heuristic_score, llm_score, total_score, opportunity_bucket, recommended_action, why, score_json, last_updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tweet_id) DO UPDATE SET
            heuristic_score=excluded.heuristic_score,
            llm_score=excluded.llm_score,
            total_score=excluded.total_score,
            opportunity_bucket=excluded.opportunity_bucket,
            recommended_action=excluded.recommended_action,
            why=excluded.why,
            score_json=excluded.score_json,
            last_updated_at=excluded.last_updated_at
        """,
        (
            tweet_id,
            source_key,
            heuristic_score,
            llm_score,
            total_score,
            opportunity_bucket,
            recommended_action,
            why,
            json.dumps(score_payload or {}),
            utc_now_iso(),
        ),
    )
    row = conn.execute("SELECT id FROM candidates WHERE tweet_id = ?", (tweet_id,)).fetchone()
    return int(row["id"])


def get_candidate(conn: sqlite3.Connection, candidate_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*, s.author_handle, s.author_name, s.text, s.posted_at, s.url, s.linked_url, s.raw_metrics, s.raw_json
        FROM candidates c
        JOIN scraped_posts s ON s.tweet_id = c.tweet_id
        WHERE c.id = ?
        """,
        (candidate_id,),
    ).fetchone()


def get_candidate_by_tweet_id(conn: sqlite3.Connection, tweet_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*, s.author_handle, s.author_name, s.text, s.posted_at, s.url, s.linked_url, s.raw_metrics, s.raw_json
        FROM candidates c
        JOIN scraped_posts s ON s.tweet_id = c.tweet_id
        WHERE c.tweet_id = ?
        """,
        (tweet_id,),
    ).fetchone()


def get_scraped_post(conn: sqlite3.Connection, tweet_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM scraped_posts WHERE tweet_id = ?",
        (tweet_id,),
    ).fetchone()


def get_top_unalerted_candidates(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.*, s.author_handle, s.author_name, s.text, s.posted_at, s.url, s.linked_url, s.raw_metrics, s.raw_json
        FROM candidates c
        JOIN scraped_posts s ON s.tweet_id = c.tweet_id
        WHERE c.status = 'new'
        ORDER BY c.total_score DESC, s.posted_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def list_unalerted_candidates(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.*, s.author_handle, s.author_name, s.text, s.posted_at, s.url, s.linked_url, s.raw_metrics, s.raw_json
        FROM candidates c
        JOIN scraped_posts s ON s.tweet_id = c.tweet_id
        WHERE c.status = 'new'
        ORDER BY c.total_score DESC, s.posted_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def expire_stale_candidates_for_source(conn: sqlite3.Connection, source_key: str, max_age_minutes: int) -> None:
    conn.execute(
        """
        UPDATE candidates
        SET status = 'expired', last_updated_at = ?
        WHERE status IN ('new', 'alerted', 'watched')
          AND source_key = ?
          AND tweet_id IN (
            SELECT tweet_id
            FROM scraped_posts
            WHERE posted_at IS NOT NULL
              AND source_key = ?
              AND posted_at < datetime('now', ?)
          )
        """,
        (utc_now_iso(), source_key, source_key, f"-{max_age_minutes} minutes"),
    )


def mark_candidate_alerted(conn: sqlite3.Connection, candidate_id: int, message_id: int) -> None:
    conn.execute(
        "UPDATE candidates SET status = 'alerted', alert_message_id = ?, alert_sent_at = ?, last_updated_at = ? WHERE id = ?",
        (message_id, utc_now_iso(), utc_now_iso(), candidate_id),
    )


def set_candidate_status(conn: sqlite3.Connection, candidate_id: int, status: str) -> None:
    conn.execute(
        "UPDATE candidates SET status = ?, last_updated_at = ? WHERE id = ?",
        (status, utc_now_iso(), candidate_id),
    )


def fetch_recent_author_rows(conn: sqlite3.Connection, lookback_hours: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT author_handle, source_key
        FROM scraped_posts
        WHERE COALESCE(posted_at, last_seen_at) >= datetime('now', ?)
        """,
        (f"-{lookback_hours} hours",),
    ).fetchall()


def insert_draft(
    conn: sqlite3.Connection,
    candidate_id: int | None,
    draft_type: str,
    draft_text: str,
    rationale: str,
    model_name: str,
    image_prompt: str | None = None,
    image_reason: str | None = None,
    generation_notes: str | None = None,
) -> int:
    now = utc_now_iso()
    cursor = conn.execute(
        """
        INSERT INTO drafts(
            candidate_id, draft_type, draft_text, original_draft_text, rationale, model_name, image_prompt, image_reason, generation_notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            draft_type,
            draft_text,
            draft_text,
            rationale,
            model_name,
            image_prompt,
            image_reason,
            generation_notes,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def get_draft(conn: sqlite3.Connection, draft_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT d.*, c.tweet_id, s.author_handle, s.author_name, s.text AS source_text, s.url AS source_url
        FROM drafts d
        LEFT JOIN candidates c ON c.id = d.candidate_id
        LEFT JOIN scraped_posts s ON s.tweet_id = c.tweet_id
        WHERE d.id = ?
        """,
        (draft_id,),
    ).fetchone()


def set_draft_message_id(conn: sqlite3.Connection, draft_id: int, message_id: int) -> None:
    conn.execute(
        "UPDATE drafts SET telegram_message_id = ?, updated_at = ? WHERE id = ?",
        (message_id, utc_now_iso(), draft_id),
    )


def update_draft_text(conn: sqlite3.Connection, draft_id: int, draft_text: str) -> None:
    conn.execute(
        "UPDATE drafts SET draft_text = ?, updated_at = ? WHERE id = ?",
        (draft_text, utc_now_iso(), draft_id),
    )


def update_draft_image(conn: sqlite3.Connection, draft_id: int, image_path: str) -> None:
    conn.execute(
        "UPDATE drafts SET image_path = ?, updated_at = ? WHERE id = ?",
        (image_path, utc_now_iso(), draft_id),
    )


def mark_draft_status(conn: sqlite3.Connection, draft_id: int, status: str, posted_tweet_id: str | None = None) -> None:
    conn.execute(
        "UPDATE drafts SET status = ?, posted_tweet_id = COALESCE(?, posted_tweet_id), updated_at = ? WHERE id = ?",
        (status, posted_tweet_id, utc_now_iso(), draft_id),
    )


def get_runtime_value(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM runtime_state WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_runtime_value(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM runtime_state WHERE key = ?", (key,))
        return
    conn.execute(
        """
        INSERT INTO runtime_state(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def get_worker_next_run_at(conn: sqlite3.Connection) -> str | None:
    return get_runtime_value(conn, "worker.next_run_at")


def set_worker_next_run_at(conn: sqlite3.Connection, value: str | None) -> None:
    set_runtime_value(conn, "worker.next_run_at", value)


def set_worker_last_run_started_at(conn: sqlite3.Connection, value: str | None) -> None:
    set_runtime_value(conn, "worker.last_run_started_at", value)


def set_worker_last_run_finished_at(conn: sqlite3.Connection, value: str | None) -> None:
    set_runtime_value(conn, "worker.last_run_finished_at", value)


def record_event(conn: sqlite3.Connection, entity_type: str, entity_id: int | None, action: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO approval_events(entity_type, entity_id, action, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (entity_type, entity_id, action, json.dumps(payload), utc_now_iso()),
    )


def record_voice_learning_event(
    conn: sqlite3.Connection,
    draft_id: int,
    decision: str,
    source_channel: str,
) -> None:
    row = get_draft(conn, draft_id)
    if not row:
        raise RuntimeError(f"Draft {draft_id} not found")
    generated_text = str(row["original_draft_text"] or row["draft_text"] or "").strip()
    final_text = str(row["draft_text"] or "").strip()
    conn.execute(
        """
        INSERT INTO voice_learning_events(
            draft_id, draft_type, decision, source_channel, source_tweet_id, source_url, source_text,
            generated_text, final_text, was_edited, rationale, generation_notes, model_name, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            draft_id,
            row["draft_type"],
            decision,
            source_channel,
            row["tweet_id"],
            row["source_url"],
            row["source_text"],
            generated_text,
            final_text,
            1 if generated_text != final_text else 0,
            row["rationale"],
            row["generation_notes"],
            row["model_name"],
            utc_now_iso(),
        ),
    )


def count_voice_learning_events_since(conn: sqlite3.Connection, after_event_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM voice_learning_events
        WHERE id > ?
        """,
        (after_event_id,),
    ).fetchone()
    return int(row["c"] if row else 0)


def list_latest_voice_learning_events(conn: sqlite3.Connection, after_event_id: int, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT vle.*
        FROM voice_learning_events vle
        JOIN (
            SELECT draft_id, MAX(id) AS max_id
            FROM voice_learning_events
            GROUP BY draft_id
        ) latest ON latest.max_id = vle.id
        WHERE vle.id > ?
        ORDER BY vle.id DESC
        LIMIT ?
        """,
        (after_event_id, limit),
    ).fetchall()


def get_latest_voice_review_proposal(
    conn: sqlite3.Connection,
    status: str | None = None,
    proposal_type: str | None = None,
) -> sqlite3.Row | None:
    if status and proposal_type:
        return conn.execute(
            """
            SELECT *
            FROM voice_review_proposals
            WHERE status = ? AND proposal_type = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (status, proposal_type),
        ).fetchone()
    if status:
        return conn.execute(
            """
            SELECT *
            FROM voice_review_proposals
            WHERE status = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (status,),
        ).fetchone()
    if proposal_type:
        return conn.execute(
            """
            SELECT *
            FROM voice_review_proposals
            WHERE proposal_type = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (proposal_type,),
        ).fetchone()
    return conn.execute(
        """
        SELECT *
        FROM voice_review_proposals
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()


def create_voice_review_proposal(
    conn: sqlite3.Connection,
    summary_text: str,
    proposal_text: str,
    diff_text: str,
    sample_count: int,
    reviewed_until_event_id: int,
    proposal_type: str = "learning",
    source_import_id: int | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO voice_review_proposals(
            status, proposal_type, summary_text, proposal_text, diff_text, sample_count, reviewed_until_event_id, source_import_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "pending",
            proposal_type,
            summary_text,
            proposal_text,
            diff_text,
            sample_count,
            reviewed_until_event_id,
            source_import_id,
            utc_now_iso(),
        ),
    )
    return int(cursor.lastrowid)


def set_voice_review_proposal_status(
    conn: sqlite3.Connection,
    proposal_id: int,
    status: str,
    review_notes: str = "",
) -> None:
    conn.execute(
        """
        UPDATE voice_review_proposals
        SET status = ?, reviewed_at = ?, review_notes = ?
        WHERE id = ?
        """,
        (status, utc_now_iso(), review_notes, proposal_id),
    )


def create_archive_import(conn: sqlite3.Connection, archive_path: str, archive_name: str, item_count: int) -> int:
    conn.execute("UPDATE archive_imports SET latest = 0")
    cursor = conn.execute(
        """
        INSERT INTO archive_imports(archive_path, archive_name, item_count, latest, imported_at)
        VALUES (?, ?, ?, 1, ?)
        """,
        (archive_path, archive_name, item_count, utc_now_iso()),
    )
    return int(cursor.lastrowid)


def insert_archive_items(conn: sqlite3.Connection, import_id: int, items: list[dict[str, str]]) -> None:
    now = utc_now_iso()
    conn.executemany(
        """
        INSERT OR IGNORE INTO archive_posts(import_id, item_kind, text, normalized_text, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                import_id,
                item["kind"],
                item["text"],
                " ".join(item["text"].split()).strip(),
                now,
            )
            for item in items
        ],
    )


def create_archive_voice_summary(
    conn: sqlite3.Connection,
    import_id: int,
    summary_text: str,
    summary_path: str | None,
    item_count: int,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO archive_voice_summaries(import_id, summary_text, summary_path, item_count, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (import_id, summary_text, summary_path, item_count, utc_now_iso()),
    )
    return int(cursor.lastrowid)


def get_latest_archive_import(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM archive_imports
        WHERE latest = 1
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()


def get_latest_archive_voice_summary(conn: sqlite3.Connection, import_id: int | None = None) -> sqlite3.Row | None:
    if import_id is not None:
        return conn.execute(
            """
            SELECT *
            FROM archive_voice_summaries
            WHERE import_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (import_id,),
        ).fetchone()
    return conn.execute(
        """
        SELECT avs.*
        FROM archive_voice_summaries avs
        JOIN archive_imports ai ON ai.id = avs.import_id
        WHERE ai.latest = 1
        ORDER BY avs.id DESC
        LIMIT 1
        """
    ).fetchone()


def count_archive_items(conn: sqlite3.Connection, import_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM archive_posts WHERE import_id = ?",
        (import_id,),
    ).fetchone()
    return int(row["c"] if row else 0)


def list_archive_items_preview(conn: sqlite3.Connection, import_id: int, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT item_kind, text
        FROM archive_posts
        WHERE import_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (import_id, limit),
    ).fetchall()


def get_telegram_offset(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM telegram_state WHERE key = 'update_offset'").fetchone()
    return int(row["value"]) if row else 0


def set_telegram_offset(conn: sqlite3.Connection, offset: int) -> None:
    conn.execute(
        """
        INSERT INTO telegram_state(key, value) VALUES ('update_offset', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(offset),),
    )


def fetch_recent_posts_for_originals(conn: sqlite3.Connection, source_keys: list[str], limit: int) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in source_keys)
    return conn.execute(
        f"""
        SELECT *
        FROM scraped_posts
        WHERE source_key IN ({placeholders})
        ORDER BY COALESCE(posted_at, last_seen_at) DESC
        LIMIT ?
        """,
        (*source_keys, limit),
    ).fetchall()


def fetch_recent_original_draft_texts(conn: sqlite3.Connection, limit: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT draft_text
        FROM drafts
        WHERE draft_type = 'original'
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [str(row["draft_text"] or "").strip() for row in rows if str(row["draft_text"] or "").strip()]


def count_original_drafts_today(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM drafts
        WHERE draft_type = 'original'
          AND created_at >= datetime('now', 'start of day')
        """
    ).fetchone()
    return int(row["c"]) if row else 0
