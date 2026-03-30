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
            recommended_action TEXT NOT NULL,
            why TEXT NOT NULL,
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
        """
    )
    draft_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(drafts)").fetchall()}
    if "generation_notes" not in draft_columns:
        conn.execute("ALTER TABLE drafts ADD COLUMN generation_notes TEXT")


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
    recommended_action: str,
    why: str,
) -> int:
    conn.execute(
        """
        INSERT INTO candidates(
            tweet_id, source_key, heuristic_score, llm_score, total_score, recommended_action, why, last_updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tweet_id) DO UPDATE SET
            heuristic_score=excluded.heuristic_score,
            llm_score=excluded.llm_score,
            total_score=excluded.total_score,
            recommended_action=excluded.recommended_action,
            why=excluded.why,
            last_updated_at=excluded.last_updated_at
        """,
        (tweet_id, source_key, heuristic_score, llm_score, total_score, recommended_action, why, utc_now_iso()),
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
            candidate_id, draft_type, draft_text, rationale, model_name, image_prompt, image_reason, generation_notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (candidate_id, draft_type, draft_text, rationale, model_name, image_prompt, image_reason, generation_notes, now, now),
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
