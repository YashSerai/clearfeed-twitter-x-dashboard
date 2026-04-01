from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import hashlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

from .config import load_config
from .service import XAgentService

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STARTUP_TASK_NAME = "ClearfeedWorker"


def run_dashboard(host: str = "127.0.0.1", port: int = 8787) -> None:
    service = XAgentService(load_config())
    service.bootstrap()
    database_path = service.config.database_path
    runtime_path = service.config.root / "data" / "runtime" / "worker_status.json"
    root = service.config.root
    python_exe = root / ".venv" / "Scripts" / "python.exe"
    worker_python_exe = root / ".venv" / "Scripts" / "pythonw.exe"
    draft_text_limit = service.dashboard_draft_text_limit

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/queue-fragment":
                snapshot = _queue_snapshot(
                    database_path,
                    draft_text_limit,
                    drafting_enabled=service.config.drafting_enabled,
                    image_generation_enabled=bool(service.config.setup_status().get("image_generation", {}).get("ok", False)),
                )
                body = json.dumps(snapshot).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/hero-status":
                status = _read_status(runtime_path, persisted_next_run_at=service._load_worker_next_run_at())
                payload = _hero_status_snapshot(status)
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path not in {"/", "/index.html"}:
                self.send_response(404)
                self.end_headers()
                return

            flash = parse_qs(parsed.query).get("flash", [""])[0]
            error = parse_qs(parsed.query).get("error", [""])[0]
            status = _read_status(runtime_path, persisted_next_run_at=service._load_worker_next_run_at())
            archive_voice = service.archive_voice_status()
            voice_review = service.voice_review_status()
            page = _render_dashboard(
                root,
                database_path,
                status,
                draft_text_limit,
                setup_status=service.config.setup_status(),
                archive_voice=archive_voice,
                voice_review=voice_review,
                drafting_enabled=service.config.drafting_enabled,
                worker_ready=service.config.session_ready and service.config.sources_ready,
                telegram_enabled=service.config.telegram_enabled,
                worker_min_delay_minutes=service.config.worker.min_delay_minutes,
                worker_max_delay_minutes=service.config.worker.max_delay_minutes,
                flash=flash,
                error=error,
            )
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            form = parse_qs(raw, keep_blank_values=True)
            parsed = urlparse(self.path)

            try:
                if parsed.path == "/candidate":
                    candidate_id = int(form["candidate_id"][0])
                    action = form["action"][0]
                    draft_guidance = form.get("draft_guidance", [""])[0]
                    result = service.candidate_action(
                        candidate_id,
                        action,
                        notify_telegram=False,
                        draft_guidance=draft_guidance,
                    )
                    self._redirect(
                        result["message"],
                        anchor=result.get("anchor"),
                        params=_redirect_params(result),
                    )
                    return
                if parsed.path == "/draft":
                    draft_id = int(form["draft_id"][0])
                    action = form["action"][0]
                    draft_text = form.get("draft_text", [""])[0]
                    result = service.draft_action(draft_id, action, notify_telegram=False, draft_text=draft_text)
                    self._redirect(
                        result["message"],
                        anchor=result.get("anchor"),
                        params=_redirect_params(result),
                    )
                    return
                if parsed.path == "/original":
                    topic = form.get("topic", [""])[0]
                    draft_ids = service.create_original_drafts(topic, notify_telegram=False)
                    self._redirect(f"Created {len(draft_ids)} original draft(s).", anchor="latest-drafts")
                    return
                if parsed.path == "/reset":
                    details = service.reset_state(clear_telegram=service.config.telegram_enabled)
                    self._redirect(f"Reset complete. Deleted {details['deleted_messages']} Telegram message(s).")
                    return
                if parsed.path == "/system":
                    action = form["action"][0]
                    message = _handle_system_action(root, python_exe, worker_python_exe, runtime_path, action)
                    self._redirect(message)
                    return
                if parsed.path == "/voice-review":
                    action = form["action"][0]
                    if action == "run":
                        result = service.maybe_run_voice_review(force=True)
                        self._redirect(result["message"], anchor="voice-review")
                        return
                    proposal_id = int(form["proposal_id"][0])
                    if action == "approve":
                        result = service.approve_voice_review(proposal_id)
                        self._redirect(result["message"], anchor="voice-review")
                        return
                    if action == "reject":
                        result = service.reject_voice_review(proposal_id)
                        self._redirect(result["message"], anchor="voice-review")
                        return
                if parsed.path == "/archive":
                    action = form["action"][0]
                    if action == "import":
                        archive_dir = form.get("archive_dir", [""])[0]
                        result = service.import_x_archive(archive_dir)
                        self._redirect(result["message"], anchor="archive-voice")
                        return
                    if action == "run":
                        result = service.maybe_run_archive_voice_build()
                        self._redirect(result["message"], anchor="archive-voice")
                        return
                    proposal_id = int(form["proposal_id"][0])
                    if action == "approve":
                        result = service.approve_archive_voice_proposal(proposal_id)
                        self._redirect(result["message"], anchor="archive-voice")
                        return
                    if action == "reject":
                        result = service.reject_archive_voice_proposal(proposal_id)
                        self._redirect(result["message"], anchor="archive-voice")
                        return
                self._redirect("Unknown action.", error=True)
            except Exception as exc:
                self._redirect(str(exc), error=True)

        def _redirect(
            self,
            message: str,
            error: bool = False,
            anchor: str | None = None,
            params: dict[str, str] | None = None,
        ) -> None:
            key = "error" if error else "flash"
            query_pairs = [(key, message)]
            for param_key, param_value in (params or {}).items():
                query_pairs.append((param_key, param_value))
            query = "&".join(f"{quote_plus(name)}={quote_plus(value)}" for name, value in query_pairs)
            suffix = f"#{anchor}" if anchor else ""
            self.send_response(303)
            self.send_header("Location", f"/?{query}{suffix}")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Dashboard listening on http://{host}:{port}")
    server.serve_forever()


def _read_status(path: Path, persisted_next_run_at: datetime | None = None) -> dict[str, Any]:
    if not path.exists():
        if persisted_next_run_at:
            return {
                "pid": None,
                "state": "stopped",
                "updated_at": None,
                "next_run_at": persisted_next_run_at.isoformat(),
            }
        return {}
    status = json.loads(path.read_text(encoding="utf-8"))
    if not status.get("next_run_at") and persisted_next_run_at:
        status["next_run_at"] = persisted_next_run_at.isoformat()
    pid = status.get("pid")
    if pid and not _pid_is_running(pid):
        status["pid"] = None
        status["state"] = "stopped"
        if persisted_next_run_at:
            status["next_run_at"] = persisted_next_run_at.isoformat()
    return status


def _query_rows(database_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _redirect_params(result: dict[str, Any]) -> dict[str, str]:
    return {}


def _hero_state_badge(worker_state: str) -> str:
    normalized = str(worker_state or "unknown")
    if normalized == "sleeping":
        return '<span class="badge badge-ok">Worker Sleeping</span>'
    if normalized == "running":
        return '<span class="badge badge-ok">Worker Running</span>'
    if normalized == "stopped":
        return '<span class="badge badge-warn">Worker Stopped</span>'
    if normalized == "error":
        return '<span class="badge badge-bad">Worker Error</span>'
    if normalized == "starting":
        return '<span class="badge badge-warn">Worker Starting</span>'
    return '<span class="badge badge-warn">Worker Unknown</span>'


def _hero_status_snapshot(status: dict[str, Any]) -> dict[str, str]:
    worker_state = str(status.get("state", "unknown"))
    next_run_at = str(status.get("next_run_at") or "")
    return {
        "badge_html": _hero_state_badge(worker_state),
        "next_run_at": next_run_at,
        "next_run_text": _countdown_text(next_run_at),
    }


def _queue_candidates(database_path: Path) -> list[sqlite3.Row]:
    return _query_rows(
        database_path,
        """
        select c.id, c.source_key, c.status, c.recommended_action, c.total_score, c.why,
               s.author_handle, s.author_name, s.text, s.posted_at, s.url, s.raw_metrics,
               d.id as draft_id, d.draft_type, d.status as draft_status, d.draft_text,
               d.updated_at as draft_updated_at, d.posted_tweet_id as draft_posted_tweet_id,
               d.image_prompt as draft_image_prompt, d.image_path as draft_image_path,
               d.generation_notes as draft_generation_notes,
               (
                   select count(*)
                   from drafts d_count
                   where d_count.candidate_id = c.id
               ) as draft_count
        from candidates c
        join scraped_posts s on s.tweet_id = c.tweet_id
        left join drafts d on d.id = (
            select d_latest.id
            from drafts d_latest
            where d_latest.candidate_id = c.id
            order by d_latest.id desc
            limit 1
        )
        where c.status in ('new', 'alerted', 'watched', 'drafted')
        order by
            case
                when d.id is not null and d.status = 'drafted' then 0
                when c.status = 'drafted' then 1
                when c.status = 'watched' then 2
                else 3
            end,
            coalesce(d.updated_at, s.posted_at) desc,
            c.total_score desc
        limit 20
        """,
    )


def _queue_snapshot(
    database_path: Path,
    draft_text_limit: int,
    drafting_enabled: bool,
    image_generation_enabled: bool,
) -> dict[str, str | int]:
    queue_candidates = _queue_candidates(database_path)
    queue_cards_html = "".join(
        _candidate_card(
            row,
            draft_text_limit,
            drafting_enabled,
            image_generation_enabled,
        )
        for row in queue_candidates
    )
    queue_nav_html = "".join(_queue_jump_button(row) for row in queue_candidates)
    signature_parts = [
        f"{row['id']}:{row['status']}:{row['draft_id'] or ''}:{row['draft_status'] or ''}:{row['draft_updated_at'] or ''}"
        for row in queue_candidates
    ]
    version = hashlib.sha1("|".join(signature_parts).encode("utf-8")).hexdigest()
    stage_html = (
        queue_cards_html
        or '<div class="queue-empty" data-queue-empty><div><h3>No tweets waiting for review</h3><p class="empty-note">Run a cycle or wait for the worker to surface fresh posts from your configured sources.</p></div></div>'
    )
    rail_html = queue_nav_html or '<p class="empty-note">No cards waiting.</p>'
    return {
        "count": len(queue_candidates),
        "version": version,
        "stage_html": stage_html,
        "rail_html": rail_html,
    }


def _render_dashboard(
    root: Path,
    database_path: Path,
    status: dict[str, Any],
    draft_text_limit: int,
    setup_status: dict[str, dict[str, str | bool]],
    archive_voice: dict[str, Any],
    voice_review: dict[str, Any],
    drafting_enabled: bool,
    worker_ready: bool,
    telegram_enabled: bool,
    worker_min_delay_minutes: int,
    worker_max_delay_minutes: int,
    flash: str = "",
    error: str = "",
) -> str:
    latest_runs = _query_rows(
        database_path,
        "select id, status, started_at, finished_at, notes from run_logs order by id desc limit 5",
    )
    queue_candidates = _queue_candidates(database_path)
    latest_original_drafts = _query_rows(
        database_path,
        """
        select d.id, d.draft_type, d.status, d.draft_text, d.updated_at, d.posted_tweet_id, d.image_prompt, d.image_path
        from drafts d
        where d.candidate_id is null
        order by d.id desc
        limit 12
        """,
    )
    overview_stats = _overview_stats(database_path, live_queue_count=len(queue_candidates))

    worker_state = status.get("state", "unknown")
    updated_at = status.get("updated_at")
    next_run_at = status.get("next_run_at")
    next_run_countdown = _countdown_text(next_run_at)
    last_error = status.get("last_error")
    process_rows = _process_rows()
    worker_log = _tail_file(root / "logs" / "worker.log")
    launcher_log = _tail_file(root / "logs" / "launcher.log")
    commands = _command_snippets(root)

    state_badge = _hero_state_badge(str(worker_state))

    worker_state_labels = {
        "sleeping": "Sleeping",
        "running": "Running",
        "stopped": "Stopped",
        "error": "Error",
        "starting": "Starting",
    }
    worker_state_label = worker_state_labels.get(str(worker_state), "Unknown")
    cadence_label = _fmt_cadence_range(worker_min_delay_minutes, worker_max_delay_minutes)
    cadence_copy = (
        f"Windows launches the worker at sign-in. After that it checks for fresh tweets every "
        f"{cadence_label}."
    )
    overview_html = "".join(
        _overview_stat_card(item["label"], item["value"], item["detail"]) for item in overview_stats
    )
    runs_html = "".join(
        f"<tr><td>{row['id']}</td><td>{row['status']}</td><td>{_fmt_time(row['started_at'])}</td><td>{_fmt_time(row['finished_at'])}</td><td>{_escape(row['notes'] or '')}</td></tr>"
        for row in latest_runs
    )
    queue_snapshot = _queue_snapshot(
        database_path,
        draft_text_limit,
        drafting_enabled=drafting_enabled,
        image_generation_enabled=bool(setup_status.get("image_generation", {}).get("ok", False)),
    )
    original_drafts_html = "".join(
        _original_draft_card(
            row,
            draft_text_limit,
            drafting_enabled,
            bool(setup_status.get("image_generation", {}).get("ok", False)),
        )
        for row in latest_original_drafts
    )
    flash_html = (
        f'<div class="notice ok" data-notice="flash"><span>{_escape(flash)}</span><button type="button" class="notice-close" data-dismiss-notice aria-label="Dismiss notice">Dismiss</button></div>'
        if flash
        else ""
    )
    error_html = (
        f'<div class="notice bad" data-notice="error"><span>{_escape(error)}</span><button type="button" class="notice-close" data-dismiss-notice aria-label="Dismiss error">Dismiss</button></div>'
        if error
        else ""
    )
    process_html = "".join(
        f"<tr><td>{row['pid']}</td><td>{_escape(row['name'])}</td><td>{_escape(row['command'])}</td></tr>"
        for row in process_rows
    ) or "<tr><td colspan='3'>No agent processes found.</td></tr>"
    commands_html = "".join(
        f"<div class='command-block'><div class='command-label'>{_escape(label)}</div><pre>{_escape(cmd)}</pre></div>"
        for label, cmd in commands
    )
    workflow_html = "".join(
        f"<li><strong>{_escape(key)}</strong><span>{_escape(value)}</span></li>"
        for key, value in [
            ("Status", worker_state_label),
            ("Last update", _fmt_time(updated_at)),
            ("Next scan", next_run_countdown),
            ("Cadence", cadence_label),
        ]
    )
    setup_html = "".join(
        _setup_status_row(str(item["label"]), bool(item["ok"]), str(item["detail"])) for item in setup_status.values()
    )
    archive_voice_html = _archive_voice_card(archive_voice=archive_voice, drafting_enabled=drafting_enabled)
    voice_review_html = _voice_review_card(voice_review=voice_review, drafting_enabled=drafting_enabled)

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Clearfeed</title>
  <style>
    :root {{
      --bg: #0a0d12;
      --panel: #101923;
      --panel-2: #142535;
      --panel-3: #0d141d;
      --text: #eef6ff;
      --muted: #96aac0;
      --accent: #77e1ff;
      --ok: #34d399;
      --warn: #f7c75f;
      --bad: #fb7185;
      --border: #244056;
      --shadow: 0 22px 60px rgba(0,0,0,.28);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(73, 180, 255, .18) 0%, transparent 28%),
        radial-gradient(circle at right top, rgba(52, 211, 153, .12) 0%, transparent 24%),
        linear-gradient(180deg, #081018 0%, var(--bg) 45%, #060a10 100%);
      color: var(--text);
      font-family: "Aptos", "Segoe UI Variable Text", "Segoe UI", sans-serif;
    }}
    .wrap {{ max-width: 1460px; margin: 0 auto; padding: 28px; }}
    .hero {{
      display: flex; justify-content: space-between; align-items: end; gap: 24px; margin-bottom: 24px;
      padding: 26px; border: 1px solid var(--border);
      background: linear-gradient(135deg, rgba(119,225,255,.16), rgba(16,25,35,.96));
      border-radius: 24px;
      box-shadow: var(--shadow);
    }}
    .hero h1 {{
        margin: 0 0 8px;
        font-size: 34px;
        font-family: "Aptos Display", "Aptos", "Segoe UI Variable Display", sans-serif;
        letter-spacing: -.03em;
      }}
    .hero-kicker {{
      margin-bottom: 10px;
      color: var(--accent);
      font-size: 12px;
      letter-spacing: .12em;
      text-transform: uppercase;
      font-weight: 700;
    }}
    .hero p {{ margin: 0; color: var(--muted); max-width: 720px; line-height: 1.5; }}
    .hero-meta-row {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:16px; }}
    .hero-meta-pill {{
      display:inline-flex;
      align-items:center;
      gap:6px;
      padding:8px 12px;
      border-radius:999px;
      border:1px solid rgba(255,255,255,.10);
      background: rgba(255,255,255,.05);
      color: var(--text);
      font-size: 12px;
    }}
    .badge {{ display:inline-block; padding: 8px 12px; border-radius: 999px; font-weight: 700; font-size: 13px; }}
    .countdown {{ margin-top: 10px; color: var(--muted); font-size: 13px; text-align: right; }}
    .hero-side {{
      display: grid;
      gap: 14px;
      justify-items: end;
      align-content: start;
      padding-top: 4px;
    }}
    .live-note {{
      color: var(--muted);
      font-size: 12px;
      text-align: right;
    }}
    .badge-ok {{ background: rgba(52,211,153,.16); color: var(--ok); }}
    .badge-warn {{ background: rgba(251,191,36,.16); color: var(--warn); }}
    .badge-bad {{ background: rgba(251,113,133,.16); color: var(--bad); }}
    .grid {{ display:grid; grid-template-columns: repeat(12, 1fr); gap: 16px; }}
    .card {{
      background: linear-gradient(180deg, var(--panel), var(--panel-2));
      border: 1px solid var(--border);
      border-radius: 22px;
      padding: 18px;
      box-shadow: var(--shadow);
    }}
    .span-4 {{ grid-column: span 4; }}
    .span-6 {{ grid-column: span 6; }}
    .span-8 {{ grid-column: span 8; }}
    .span-5 {{ grid-column: span 5; }}
    .span-7 {{ grid-column: span 7; }}
    .span-12 {{ grid-column: span 12; }}
    h2 {{ margin: 0 0 14px; font-size: 18px; }}
    .meta {{ color: var(--muted); font-size: 14px; line-height: 1.6; }}
    ul.stats {{ list-style:none; padding:0; margin:0; }}
    ul.stats li {{ display:flex; justify-content:space-between; padding:10px 0; border-bottom:1px solid rgba(255,255,255,.06); }}
    ul.stats li:last-child {{ border-bottom:none; }}
    .setup-grid {{ display:grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap:10px; }}
    .setup-item {{
      min-width: 0;
      padding:12px 12px 10px;
      border-radius:16px;
      border:1px solid var(--border);
      background: rgba(255,255,255,.02);
      display:flex;
      flex-direction:column;
      gap:8px;
    }}
    .setup-item-head {{ display:flex; justify-content:space-between; align-items:flex-start; gap:10px; }}
    .setup-item strong {{ display:block; line-height:1.2; }}
    .setup-item p {{
      margin:0;
      color: var(--muted);
      line-height: 1.35;
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
    }}
    .badge-mini {{ padding:4px 8px; border-radius:999px; font-size:12px; font-weight:700; }}
    .badge-mini-ok {{ color: var(--ok); background: rgba(52,211,153,.12); }}
    .badge-mini-warn {{ color: var(--warn); background: rgba(247,199,95,.12); }}
    .overview-grid {{ display:grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap:12px; }}
    .overview-card {{
      min-width:0;
      padding:14px;
      border-radius:18px;
      border:1px solid rgba(255,255,255,.08);
      background: rgba(255,255,255,.03);
    }}
    .overview-label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
      margin-bottom: 8px;
    }}
    .overview-value {{
      font-size: 34px;
      font-weight: 700;
      line-height: 1;
      margin-bottom: 8px;
      color: var(--text);
    }}
    .overview-detail {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
      margin: 0;
    }}
    table {{ width:100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ text-align:left; padding: 10px 8px; border-bottom:1px solid rgba(255,255,255,.06); vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; }}
    a {{ color: var(--accent); text-decoration: none; }}
    .error {{ margin-top: 12px; color: var(--bad); white-space: pre-wrap; }}
    .controls {{ display:flex; gap:8px; flex-wrap:wrap; }}
    .controls form {{ margin:0; }}
    .worker-controls {{
      display:grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap:10px;
      margin-top: 10px;
    }}
    .worker-controls form {{
      margin:0;
    }}
    .worker-controls button {{
      width: 100%;
    }}
    .original-drafts-card {{
      display:flex;
      flex-direction:column;
    }}
    .original-draft-form {{
      display:flex;
      flex-direction:column;
      gap:12px;
      flex: 1 1 auto;
    }}
    .original-topic-input {{
      min-height: 148px;
      flex: 1 1 auto;
      resize: vertical;
      line-height: 1.55;
    }}
    .section-note {{ margin: -6px 0 14px; color: var(--muted); font-size: 13px; }}
    button {{
      border: 1px solid var(--border);
      background: #0f1823;
      color: var(--text);
      padding: 9px 13px;
      border-radius: 12px;
      cursor: pointer;
      font-weight: 600;
      white-space: nowrap;
    }}
    button:hover {{ border-color: var(--accent); }}
    button.bad {{ border-color: rgba(251,113,133,.35); color: var(--bad); }}
    button.ok {{ border-color: rgba(52,211,153,.35); color: var(--ok); }}
    button.ghost-button {{
      background: transparent;
      color: var(--muted);
    }}
    button:disabled {{
      opacity: .48;
      cursor: not-allowed;
      border-color: rgba(255,255,255,.08);
    }}
    .inline-warning {{ margin-top: 10px; color: var(--warn); font-size: 13px; line-height: 1.45; }}
    .notice {{
      padding: 12px 14px;
      border-radius: 14px;
      margin-bottom: 16px;
      border: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }}
    .notice.ok {{ background: rgba(52,211,153,.12); color: var(--ok); }}
    .notice.bad {{ background: rgba(251,113,133,.12); color: var(--bad); }}
    .notice-close {{
      background: transparent;
      color: inherit;
      border-color: rgba(255,255,255,.18);
      padding: 6px 10px;
      flex: 0 0 auto;
    }}
    input[type=text] {{
      width: 100%;
      background: #0d1520;
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 12px;
      margin-bottom: 10px;
    }}
    textarea {{
      width: 100%;
      background: #0d1520;
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 12px;
      font: inherit;
      line-height: 1.45;
    }}
    pre {{
      white-space: pre-wrap;
      background: #0b1119;
      border: 1px solid rgba(255,255,255,.06);
      border-radius: 12px;
      padding: 12px;
      margin: 0;
      color: #dce8f5;
      font-size: 13px;
    }}
    .command-block {{ margin-bottom: 12px; }}
    .command-block:last-child {{ margin-bottom: 0; }}
    .command-label {{ color: var(--muted); font-size: 13px; margin-bottom: 6px; }}
    .dev-details {{
      border: 1px solid rgba(255,255,255,.07);
      border-radius: 20px;
      background: rgba(8,14,21,.45);
      overflow: hidden;
    }}
    .dev-details summary {{
      cursor: pointer;
      font-weight: 700;
      list-style: none;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 18px 20px;
    }}
    .dev-details summary::-webkit-details-marker {{ display:none; }}
    .dev-summary-copy {{
      display: grid;
      gap: 4px;
      min-width: 0;
    }}
    .dev-summary-title {{
      color: var(--text);
      font-size: 16px;
      line-height: 1.2;
    }}
    .dev-summary-subtitle {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }}
    .dev-chevron {{
      color: var(--muted);
      font-size: 14px;
      transition: transform 160ms ease, color 160ms ease;
      flex: 0 0 auto;
    }}
    .dev-details[open] .dev-chevron {{
      transform: rotate(180deg);
      color: var(--accent);
    }}
    .dev-details-body {{
      padding: 0 20px 20px;
    }}
    .dev-details-note {{
      margin: 0 0 18px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .dev-stack {{ display:grid; gap:16px; margin-top:16px; }}
    .dev-split {{ display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:16px; }}
    .dev-panel {{
      padding:16px;
      border-radius:18px;
      border:1px solid rgba(255,255,255,.07);
      background: rgba(8,14,21,.45);
    }}
    .dev-panel h3 {{
      margin: 0 0 12px;
      font-size: 16px;
    }}
    .command-details {{
      border: 1px solid rgba(255,255,255,.07);
      border-radius: 16px;
      background: rgba(8,14,21,.35);
      overflow: hidden;
    }}
    .command-details summary {{
      cursor: pointer;
      list-style: none;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      font-weight: 700;
    }}
    .command-details summary::-webkit-details-marker {{ display:none; }}
    .command-details-body {{
      padding: 0 16px 16px;
    }}
    .command-details[open] .dev-chevron {{
      transform: rotate(180deg);
      color: var(--accent);
    }}
    .voice-review-card {{
      border: 1px solid rgba(119,225,255,.14);
      border-radius: 24px;
      padding: 22px;
      background:
        linear-gradient(180deg, rgba(8,13,20,.96), rgba(13,20,29,.94)),
        radial-gradient(circle at top right, rgba(119,225,255,.10), transparent 32%);
    }}
    .voice-review-top {{
      display:grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap:18px;
      align-items:start;
      margin-bottom:16px;
    }}
    .voice-review-heading {{
      display:grid;
      gap:8px;
    }}
    .voice-review-title {{
      font-size: 30px;
      font-weight: 700;
      line-height: 1.05;
      margin: 0;
    }}
    .voice-review-kicker {{
      color: var(--accent);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .1em;
    }}
    .voice-review-actions {{
      display:flex;
      flex-wrap:wrap;
      gap:10px;
      justify-content:flex-end;
      align-items:flex-start;
    }}
    .voice-review-meta {{
      display:grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap:10px;
      margin: 14px 0 0;
    }}
    .voice-review-pill {{
      display:grid;
      gap:4px;
      padding:12px 14px;
      border-radius:16px;
      border:1px solid rgba(255,255,255,.08);
      background: rgba(255,255,255,.04);
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }}
    .voice-review-pill strong {{
      color: var(--text);
      font-size: 15px;
      font-weight: 700;
      line-height: 1.2;
    }}
    .voice-review-summary {{
      margin: 0;
      color: var(--text);
      line-height: 1.55;
      white-space: pre-wrap;
    }}
    .voice-review-empty {{
      color: var(--muted);
      font-size: 14px;
      line-height: 1.55;
      margin-top: 14px;
    }}
    .archive-details {{
      border: 1px solid rgba(255,255,255,.08);
      border-radius: 16px;
      background: rgba(8,14,21,.35);
      overflow: hidden;
    }}
    .archive-details summary {{
      cursor: pointer;
      list-style: none;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      font-weight: 700;
    }}
    .archive-details summary::-webkit-details-marker {{ display:none; }}
    .archive-details[open] .dev-chevron {{
      transform: rotate(180deg);
      color: var(--accent);
    }}
    .archive-body {{
      padding: 0 16px 16px;
      display:grid;
      gap:12px;
    }}
    .archive-inline-meta {{
      display:flex;
      flex-wrap:wrap;
      gap:8px;
    }}
    .archive-inline-chip {{
      padding:8px 10px;
      border-radius:999px;
      border:1px solid rgba(255,255,255,.08);
      background: rgba(255,255,255,.04);
      color: var(--muted);
      font-size: 12px;
      line-height: 1.2;
    }}
    .archive-inline-chip strong {{
      color: var(--text);
      font-weight: 700;
    }}
    .voice-compare {{
      display:grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap:12px;
      margin-top: 18px;
    }}
    .voice-compare-panel {{
      min-width:0;
      border:1px solid rgba(255,255,255,.08);
      border-radius:16px;
      background: rgba(8,14,21,.4);
      padding:14px 16px;
      display:grid;
      gap:10px;
    }}
    .voice-compare-label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
    }}
    .voice-compare-path {{
      color: var(--accent);
      font-size: 12px;
      word-break: break-word;
    }}
    .voice-compare pre {{
      margin:0;
      max-height: 420px;
      overflow:auto;
    }}
    .voice-diff details {{
      margin-top: 18px;
      border: 1px solid rgba(255,255,255,.08);
      border-radius: 16px;
      background: rgba(8,14,21,.4);
      padding: 14px 16px;
    }}
    .voice-diff summary {{
      cursor: pointer;
      color: var(--accent);
      font-weight: 700;
    }}
    .voice-diff pre {{
      margin-top: 12px;
    }}
    .reset-grid {{
      display:grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap:10px;
      margin: 12px 0 14px;
    }}
    .reset-item {{
      padding:12px;
      border-radius:16px;
      border:1px solid rgba(255,255,255,.08);
      background: rgba(255,255,255,.03);
    }}
    .reset-item strong {{
      display:block;
      color: var(--text);
      margin-bottom: 4px;
      font-size: 14px;
    }}
    .reset-item small {{
      color: var(--muted);
      line-height: 1.4;
    }}
    .draft-cell {{ min-width: 340px; }}
    .draft-form {{ display: grid; gap: 8px; }}
    .draft-editor {{
      min-height: 172px;
      resize: vertical;
      overflow-y: hidden;
    }}
    .draft-editor[readonly] {{
      opacity: .92;
    }}
    .draft-meta {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      color: var(--muted);
      font-size: 12px;
    }}
    .draft-meta button {{
      padding: 6px 10px;
      font-size: 12px;
    }}
    .draft-row-focus {{
      outline: 1px solid rgba(125,211,252,.65);
      box-shadow: inset 0 0 0 9999px rgba(125,211,252,.08);
    }}
    .queue-shell {{
      padding: 22px;
      background:
        linear-gradient(180deg, rgba(16,25,35,.97), rgba(20,37,53,.97)),
        radial-gradient(circle at top left, rgba(119,225,255,.12), transparent 32%);
    }}
    .queue-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      margin-bottom: 18px;
    }}
    .queue-toolbar {{
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      align-items: center;
      gap: 10px;
    }}
    .queue-counter {{
      color: var(--muted);
      font-size: 13px;
      min-width: 120px;
      text-align: right;
    }}
    .queue-layout {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 280px;
      gap: 18px;
      align-items: start;
    }}
    .queue-layout > * {{
      min-width: 0;
    }}
    .queue-stage {{
      min-height: 560px;
      min-width: 0;
    }}
    .queue-empty {{
      min-height: 320px;
      border: 1px dashed rgba(119,225,255,.28);
      border-radius: 24px;
      display: grid;
      place-items: center;
      text-align: center;
      padding: 28px;
      color: var(--muted);
      background: rgba(8,16,24,.55);
    }}
    .queue-card {{
      display: none;
      grid-template-columns: minmax(0, 1fr) 312px;
      gap: 18px;
      align-items: start;
      min-width: 0;
    }}
    .queue-card.is-active {{ display: grid; }}
    .queue-card-main,
    .queue-card-side {{
      display: grid;
      gap: 14px;
      min-width: 0;
    }}
    .tweet-shell {{
      position: relative;
      overflow: hidden;
      border: 1px solid rgba(119,225,255,.16);
      border-radius: 26px;
      padding: 22px;
      background:
        linear-gradient(180deg, rgba(9,16,24,.98), rgba(16,25,35,.9)),
        radial-gradient(circle at top right, rgba(119,225,255,.1), transparent 28%);
    }}
    .tweet-shell::before {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background: linear-gradient(120deg, rgba(255,255,255,.05), transparent 22%, transparent 78%, rgba(255,255,255,.04));
    }}
    .eyebrow {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 16px;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 7px 11px;
      border-radius: 999px;
      font-size: 12px;
      letter-spacing: .02em;
      text-transform: uppercase;
      border: 1px solid rgba(255,255,255,.08);
      color: var(--muted);
      background: rgba(255,255,255,.03);
    }}
    .pill-accent {{ color: var(--accent); border-color: rgba(119,225,255,.26); background: rgba(119,225,255,.10); }}
    .pill-ok {{ color: var(--ok); border-color: rgba(52,211,153,.24); background: rgba(52,211,153,.10); }}
    .pill-warn {{ color: var(--warn); border-color: rgba(247,199,95,.24); background: rgba(247,199,95,.10); }}
    .pill-bad {{ color: var(--bad); border-color: rgba(251,113,133,.24); background: rgba(251,113,133,.10); }}
    .tweet-head {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      margin-bottom: 14px;
    }}
    .tweet-author {{
      font-size: 20px;
      font-weight: 700;
      color: var(--text);
    }}
    .tweet-age {{
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }}
    .tweet-body {{
      margin: 0;
      font-size: 18px;
      line-height: 1.6;
      white-space: pre-wrap;
    }}
    .tweet-footer {{
      margin-top: 18px;
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      color: var(--muted);
      font-size: 13px;
      flex-wrap: wrap;
    }}
    .candidate-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }}
    .candidate-form {{
      display: grid;
      gap: 10px;
    }}
    .candidate-guidance-label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
    }}
    .candidate-guidance {{
      min-height: 94px;
      resize: vertical;
    }}
    .candidate-guidance-help {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      margin-top: -2px;
    }}
    .draft-guidance-note {{
      margin: 0 0 10px;
      padding: 10px 12px;
      border-radius: 14px;
      background: rgba(119,225,255,.08);
      border: 1px solid rgba(119,225,255,.16);
      color: var(--text);
      font-size: 13px;
      line-height: 1.5;
      white-space: pre-wrap;
    }}
    .candidate-actions form,
    .draft-inline-actions form {{
      margin: 0;
    }}
    .draft-inline {{
      border: 1px solid rgba(255,255,255,.08);
      border-radius: 22px;
      padding: 16px;
      background: linear-gradient(180deg, rgba(8,13,20,.92), rgba(13,20,29,.92));
    }}
    .draft-inline.draft-focus,
    .original-card.draft-focus {{
      border-color: rgba(119,225,255,.5);
      box-shadow: 0 0 0 1px rgba(119,225,255,.2), 0 0 0 10px rgba(119,225,255,.06);
    }}
    .draft-inline-empty {{
      border-style: dashed;
      color: var(--muted);
    }}
    .draft-inline-header {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 10px;
      align-items: flex-start;
      flex-wrap: wrap;
    }}
    .draft-inline-header h3,
    .original-drafts-grid h3 {{
      margin: 0 0 4px;
      font-size: 18px;
    }}
    .draft-inline-note {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .draft-inline-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }}
    .side-panel {{
      border: 1px solid rgba(255,255,255,.06);
      border-radius: 20px;
      padding: 16px;
      background: rgba(8,14,21,.55);
    }}
    .side-kicker {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
      margin-bottom: 8px;
    }}
    .score-value {{
      font-size: 42px;
      font-weight: 700;
      line-height: 1;
      margin-bottom: 8px;
      color: var(--accent);
    }}
    .side-copy {{
      margin: 0;
      color: var(--text);
      line-height: 1.55;
      white-space: pre-wrap;
    }}
    .queue-rail {{
      display: grid;
      gap: 10px;
      align-content: start;
      min-width: 0;
    }}
    .queue-rail-label {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: var(--muted);
      margin-bottom: 2px;
    }}
    .queue-jump {{
      width: 100%;
      min-width: 0;
      text-align: left;
      border: 1px solid rgba(255,255,255,.08);
      border-radius: 18px;
      padding: 12px 14px;
      background: rgba(8,13,20,.75);
      white-space: normal;
    }}
    .queue-jump strong {{
      display: block;
      color: var(--text);
      margin-bottom: 4px;
      white-space: normal;
      overflow-wrap: anywhere;
    }}
    .queue-jump small {{
      display: block;
      color: var(--muted);
      line-height: 1.45;
      white-space: normal;
      overflow-wrap: anywhere;
    }}
    .queue-jump.is-active {{
      border-color: rgba(119,225,255,.48);
      background: rgba(119,225,255,.10);
    }}
    .queue-jump.is-skipped {{
      opacity: .5;
    }}
    .queue-card.queue-card-focus .tweet-shell {{
      border-color: rgba(119,225,255,.42);
      box-shadow: 0 0 0 1px rgba(119,225,255,.16), 0 20px 50px rgba(0,0,0,.28);
    }}
    .original-drafts-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }}
    .original-card {{
      border: 1px solid rgba(255,255,255,.07);
      border-radius: 22px;
      padding: 16px;
      background: linear-gradient(180deg, rgba(9,15,22,.92), rgba(15,23,34,.92));
    }}
    .empty-note {{
      color: var(--muted);
      margin: 0;
    }}
    .busy-overlay {{
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 20px;
      background: rgba(4, 8, 14, .72);
      backdrop-filter: blur(8px);
      z-index: 999;
    }}
    .busy-overlay.is-visible {{
      display: flex;
    }}
    .busy-card {{
      min-width: min(420px, 100%);
      max-width: 520px;
      padding: 24px;
      border-radius: 22px;
      border: 1px solid rgba(119,225,255,.22);
      background: linear-gradient(180deg, rgba(9,16,24,.98), rgba(16,25,35,.96));
      box-shadow: var(--shadow);
      display: grid;
      gap: 14px;
      text-align: center;
    }}
    .busy-spinner {{
      width: 40px;
      height: 40px;
      margin: 0 auto;
      border-radius: 999px;
      border: 3px solid rgba(255,255,255,.12);
      border-top-color: var(--accent);
      animation: spin 900ms linear infinite;
    }}
    .busy-title {{
      font-size: 18px;
      font-weight: 700;
    }}
    .busy-copy {{
      color: var(--muted);
      line-height: 1.55;
      margin: 0;
    }}
    .toast {{
      position: fixed;
      right: 24px;
      bottom: 24px;
      padding: 12px 16px;
      border-radius: 14px;
      border: 1px solid rgba(95, 205, 255, 0.35);
      background: rgba(10, 18, 27, 0.94);
      color: var(--text);
      box-shadow: 0 18px 38px rgba(0, 0, 0, 0.35);
      opacity: 0;
      transform: translateY(10px);
      pointer-events: none;
      transition: opacity 120ms ease, transform 120ms ease;
      z-index: 80;
    }}
    .toast.is-visible {{
      opacity: 1;
      transform: translateY(0);
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    @media (max-width: 1100px) {{
      .span-4, .span-5, .span-6, .span-7, .span-8 {{ grid-column: span 12; }}
      .overview-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
      .setup-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
      .dev-split {{ grid-template-columns: 1fr; }}
      .queue-layout,
      .queue-card,
      .original-drafts-grid {{ grid-template-columns: 1fr; }}
      .queue-toolbar {{ justify-content: flex-start; }}
      .queue-counter {{ text-align: left; min-width: 0; }}
      .worker-controls {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .voice-review-top {{ grid-template-columns: 1fr; }}
      .voice-review-actions {{ justify-content: flex-start; }}
      .voice-review-meta {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .voice-compare {{ grid-template-columns: 1fr; }}
      .archive-summary {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 720px) {{
      .wrap {{ padding: 16px; }}
      .hero {{ padding: 20px; border-radius: 20px; flex-direction: column; align-items: flex-start; }}
      .hero h1 {{ font-size: 28px; }}
      .overview-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .setup-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .tweet-body {{ font-size: 16px; }}
      .tweet-head {{ flex-direction: column; align-items: flex-start; }}
      .queue-shell {{ padding: 16px; }}
      .queue-header {{ flex-direction: column; }}
      .candidate-actions,
      .draft-inline-actions {{ gap: 8px; }}
      button {{ width: 100%; }}
      .candidate-actions form,
      .draft-inline-actions form {{ width: 100%; }}
      .candidate-actions form button,
      .draft-inline-actions form button {{ width: 100%; }}
      .queue-toolbar button {{ width: auto; }}
      .worker-controls {{ grid-template-columns: 1fr; }}
      .voice-review-meta,
      .reset-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
  <body>
    <div class="wrap">
      <div class="hero">
        <div>
          <div class="hero-kicker">Local X Workflow</div>
          <h1>Clearfeed</h1>
          <p>Review high-signal tweets from your weighted lists, draft replies in your own voice, and move the best ones into a clean copy-and-post workflow. Tweets, drafts, and decisions stay local so you can refine the workflow over time.</p>
          <div class="hero-meta-row">
            <span class="hero-meta-pill">Discovery: weighted lists + optional home</span>
            <span class="hero-meta-pill">Workflow: draft, copy, and post manually</span>
            <span class="hero-meta-pill">{'Telegram mirror: on' if telegram_enabled else 'Telegram mirror: off'}</span>
          </div>
        </div>
        <div class="hero-side">
          <div data-worker-badge>{state_badge}</div>
        <div class="countdown" id="next-run-countdown" data-next-run="{_escape(next_run_at or '')}">
          Next run: {_escape(next_run_countdown)}
        </div>
        <div class="controls">
          <button type="button" class="ghost-button" data-soft-refresh>Refresh Dashboard</button>
        </div>
        <div class="live-note" data-live-note>Reply queue and worker status update live. Use Refresh Dashboard for everything else.</div>
      </div>
    </div>
    {flash_html}
    {error_html}
    <div class="grid">
      <section class="card span-12">
        <h2>Overview</h2>
        <div class="section-note">A quick read on how much signal the dashboard has collected and what happened to your drafts.</div>
        <div class="overview-grid">{overview_html}</div>
      </section>
      <section class="card span-12">
        <h2>Setup Status</h2>
        <div class="setup-grid">{setup_html}</div>
      </section>
      <section class="card span-12" id="voice-review">
        <h2>Adaptive Voice</h2>
        <div class="section-note">Clearfeed learns from what you edit, keep, and reject in the dashboard, then suggests a better <code>Voice.md</code> over time. <code>Humanizer.md</code> stays fixed.</div>
        {voice_review_html}
      </section>
      <div class="span-12" id="archive-voice">
        {archive_voice_html}
      </div>
      <section class="card span-4 original-drafts-card">
        <h2>Create Original Drafts</h2>
        <div class="section-note">Use this for standalone posts that are not tied to a tweet in the reply queue.</div>
        <form method="post" action="/original" class="original-draft-form">
          <textarea name="topic" class="original-topic-input" placeholder="Optional topic, angle, or context for the post. Example: new OpenAI parameter changes and why they matter for builders."></textarea>
          <button class="ok" type="submit" data-busy-label="Generating original drafts..."{' disabled title="Configure the selected AI provider in .env to enable original drafting."' if not drafting_enabled else ''}>Generate Original Drafts</button>
        </form>
        {'' if drafting_enabled else '<div class="inline-warning">Original drafting is disabled until the selected AI provider is configured in <code>.env</code>.</div>'}
      </section>
      <section class="card span-4">
        <h2>Worker Flow</h2>
        <div class="section-note">{_escape(cadence_copy)}</div>
        <ul class="stats">{workflow_html}</ul>
        <div class="worker-controls">
          {_post_button('/system', 'action', 'start', None, None, 'Start Worker', 'ok', 'Starting worker...', disabled=not worker_ready, disabled_reason='Configure at least one source and capture an X session before starting the worker.')}
          {_post_button('/system', 'action', 'stop', None, None, 'Stop Worker', 'bad', 'Stopping worker...')}
          {_post_button('/system', 'action', 'restart', None, None, 'Restart Worker', '', 'Restarting worker...', disabled=not worker_ready, disabled_reason='Configure at least one source and capture an X session before restarting the worker.')}
          {_post_button('/system', 'action', 'run_cycle', None, None, 'Run Cycle Now', '', 'Running a cycle now...', disabled=not worker_ready, disabled_reason='Configure at least one source and capture an X session before running a cycle.')}
        </div>
        {f'<div class="error"><strong>Last error:</strong> {_escape(last_error)}</div>' if last_error else ''}
        {'' if worker_ready else '<div class="inline-warning">Worker actions are disabled until an X session is captured and at least one source is configured.</div>'}
      </section>
      <section class="card span-4">
        <h2>Reset History</h2>
        <div class="section-note">Clears local queue, drafts, posted history, archive summaries, and voice proposal history. Keeps your <code>.env</code>, sources, and profile files.</div>
        <div class="reset-grid">
          <div class="reset-item"><strong>Queue</strong><small>Clears candidate review state.</small></div>
          <div class="reset-item"><strong>Drafts</strong><small>Removes saved reply and original drafts.</small></div>
          <div class="reset-item"><strong>Posted</strong><small>Clears drafts you already copied out or marked as posted.</small></div>
          <div class="reset-item"><strong>Voice Data</strong><small>Removes archive imports, proposals, and learning events.</small></div>
        </div>
        <form method="post" action="/reset" onsubmit="return confirm('Reset local state and clear tracked drafts, candidates, and optional Telegram message references?');">
          <button class="bad" type="submit" data-busy-label="Resetting local state...">Clear History</button>
        </form>
      </section>
        <section class="card span-12 queue-shell" id="reply-queue">
        <div class="queue-header">
            <div>
              <h2>Reply Queue</h2>
              <div class="section-note">Work through one candidate at a time. Each tweet keeps its draft, edit box, and follow-up actions attached so you can review without losing context. Edit drafts here before you save or mark them posted if you want Adaptive Voice to learn from your changes. {'' if drafting_enabled else 'Draft generation is disabled until the selected AI provider is configured.'}</div>
            </div>
            <div class="queue-toolbar">
              <span class="queue-counter" data-queue-counter>{len(queue_candidates)} in queue</span>
              <button type="button" class="ghost-button" data-refresh-queue hidden>Refresh Queue</button>
              <button type="button" data-queue-prev>Prev</button>
              <button type="button" data-queue-next>Next</button>
              <button type="button" class="ghost-button" data-restart-deck>Back to first</button>
            </div>
          </div>
          <div class="queue-layout" data-queue-root data-queue-version="{queue_snapshot['version']}">
            <div class="queue-stage">
              {queue_snapshot['stage_html']}
            </div>
            <aside class="queue-rail">
              <div class="queue-rail-label">Jump List</div>
              {queue_snapshot['rail_html']}
            </aside>
          </div>
        </section>
      <section class="card span-12" id="latest-drafts">
        <h2>Original Drafts</h2>
        <div class="section-note">Standalone drafts created from the topic box stay here. Reply drafts stay inside the queue cards above.</div>
        <div class="original-drafts-grid">
          {original_drafts_html or '<p class="empty-note">No standalone drafts yet.</p>'}
        </div>
      </section>
      <section class="card span-12">
        <details class="dev-details">
          <summary>
            <span class="dev-summary-copy">
              <span class="dev-summary-title">Developer Tools</span>
              <span class="dev-summary-subtitle">Optional logs and process details for debugging local runs.</span>
            </span>
            <span class="dev-chevron" aria-hidden="true">&#9662;</span>
          </summary>
          <div class="dev-details-body">
            <div class="dev-details-note">Most users can ignore this section.</div>
            <div class="dev-stack">
              <div class="dev-panel">
                <h3>Recent Runs</h3>
                <table>
                  <thead>
                    <tr><th>ID</th><th>Status</th><th>Started</th><th>Finished</th><th>Notes</th></tr>
                </thead>
                <tbody>{runs_html}</tbody>
              </table>
            </div>
            <div class="dev-panel">
              <h3>Live Processes</h3>
              <table>
                <thead>
                  <tr><th>PID</th><th>Name</th><th>Command</th></tr>
                </thead>
                <tbody>{process_html}</tbody>
              </table>
            </div>
            <div class="dev-split">
              <div class="dev-panel">
                <h3>Worker Log Tail</h3>
                <pre>{_escape(worker_log)}</pre>
              </div>
              <div class="dev-panel">
                <h3>Launcher Log Tail</h3>
                <pre>{_escape(launcher_log)}</pre>
              </div>
            </div>
          </div>
        </details>
      </section>
      <section class="card span-12">
        <details class="command-details">
          <summary>
            <span class="dev-summary-copy">
              <span class="dev-summary-title">Helpful Commands</span>
              <span class="dev-summary-subtitle">Quick run, stop, and maintenance commands for this repo.</span>
            </span>
            <span class="dev-chevron" aria-hidden="true">&#9662;</span>
          </summary>
          <div class="command-details-body">
            {commands_html}
          </div>
        </details>
      </section>
    </div>
  </div>
  <div class="busy-overlay" data-busy-overlay aria-hidden="true">
    <div class="busy-card" role="status" aria-live="polite">
      <div class="busy-spinner" aria-hidden="true"></div>
      <div class="busy-title" data-busy-title>Working...</div>
      <p class="busy-copy">The dashboard is still processing your request. This can take a few seconds when it is scraping, grounding, or drafting.</p>
    </div>
  </div>
  <div class="toast" data-toast aria-hidden="true"></div>
</body>
<script>
(() => {{
  const scrollKey = `dashboard-scroll:${{window.location.pathname}}`;
  const saveScroll = () => {{
    window.sessionStorage.setItem(scrollKey, String(window.scrollY));
  }};
  const notices = Array.from(document.querySelectorAll('[data-dismiss-notice]'));
  notices.forEach((button) => {{
    button.addEventListener('click', () => {{
      const notice = button.closest('.notice');
      if (notice) {{
        notice.remove();
      }}
    }});
  }});
    const url = new URL(window.location.href);
    if (!window.location.hash) {{
      const savedScroll = window.sessionStorage.getItem(scrollKey);
      if (savedScroll) {{
        window.requestAnimationFrame(() => {{
          window.scrollTo({{ top: Number(savedScroll), behavior: 'auto' }});
        }});
      }}
    }}
  if (url.searchParams.has('flash') || url.searchParams.has('error')) {{
      url.searchParams.delete('flash');
      url.searchParams.delete('error');
      window.history.replaceState({{}}, '', url.pathname + (url.search ? `?${{url.searchParams.toString()}}` : ''));
    }}
  window.addEventListener('pagehide', saveScroll);
  window.addEventListener('beforeunload', saveScroll);
  const busyOverlay = document.querySelector('[data-busy-overlay]');
  const busyTitle = document.querySelector('[data-busy-title]');
  const toast = document.querySelector('[data-toast]');
  let toastTimer = null;
  const showBusy = (message) => {{
    if (!busyOverlay || !busyTitle) {{
      return;
    }}
    busyTitle.textContent = message || 'Working...';
    busyOverlay.classList.add('is-visible');
    busyOverlay.setAttribute('aria-hidden', 'false');
  }};
  const hideBusy = () => {{
    if (!busyOverlay || !busyTitle) {{
      return;
    }}
    busyOverlay.classList.remove('is-visible');
    busyOverlay.setAttribute('aria-hidden', 'true');
  }};
  const showToast = (message) => {{
    if (!toast) {{
      return;
    }}
    toast.textContent = message;
    toast.classList.add('is-visible');
    toast.setAttribute('aria-hidden', 'false');
    if (toastTimer) {{
      window.clearTimeout(toastTimer);
    }}
    toastTimer = window.setTimeout(() => {{
      toast.classList.remove('is-visible');
      toast.setAttribute('aria-hidden', 'true');
    }}, 1800);
  }};
  const editors = Array.from(document.querySelectorAll('[data-draft-editor]'));
  const maxEditorHeight = 240;
  const syncEditor = (editor) => {{
    editor.style.height = 'auto';
    const nextHeight = Math.min(editor.scrollHeight, maxEditorHeight);
    editor.style.height = `${{nextHeight}}px`;
    editor.style.overflowY = editor.scrollHeight > maxEditorHeight ? 'auto' : 'hidden';
    const counter = document.querySelector(`[data-char-count-for="${{editor.id}}"]`);
    if (counter) {{
      const limit = editor.dataset.limit || '';
      counter.textContent = limit ? `${{editor.value.length}} / ${{limit}}` : `${{editor.value.length}} chars`;
    }}
  }};
  editors.forEach((editor) => {{
    syncEditor(editor);
    editor.addEventListener('input', () => {{
      editor.dataset.dirty = 'true';
      syncEditor(editor);
    }});
  }});
  const postForms = Array.from(document.querySelectorAll('form[method="post"]'));
  postForms.forEach((form) => {{
    form.addEventListener('submit', (event) => {{
      let editor = form.querySelector('[data-draft-editor]');
      const draftIdField = form.querySelector('input[name="draft_id"]');
      const hiddenDraftText = form.querySelector('input[name="draft_text"]');
      if (!editor && draftIdField && hiddenDraftText) {{
        editor = document.getElementById(`draft-text-${{draftIdField.value}}`);
        if (editor) {{
          hiddenDraftText.value = editor.value;
        }}
      }}
      if (editor) {{
        editor.dataset.dirty = 'false';
      }}
      saveScroll();
      const submitter = event.submitter;
      const busyLabel = (submitter && submitter.dataset.busyLabel) || form.dataset.busyLabel || 'Working...';
      showBusy(busyLabel);
    }});
  }});
  document.addEventListener('click', async (event) => {{
    const button = event.target.closest('[data-copy-draft]');
    if (!button) {{
      return;
    }}
    const draftId = button.dataset.draftId;
    const editor = draftId ? document.getElementById(`draft-text-${{draftId}}`) : null;
    if (!editor) {{
      showToast('Draft text not found');
      return;
    }}
    try {{
      await navigator.clipboard.writeText(editor.value);
      showToast('Draft copied');
    }} catch (_err) {{
      editor.focus();
      editor.select();
      showToast('Select text and copy manually');
    }}
  }});
  const queueStateKey = `dashboard-queue:${{window.location.pathname}}`;
  const queueRefreshButton = document.querySelector('[data-refresh-queue]');
  let pendingQueueRefresh = false;

  const hasDirtyDraftEditors = () => Array.from(document.querySelectorAll('[data-draft-editor]')).some((editor) => {{
    return editor.dataset.dirty === 'true' || document.activeElement === editor;
  }});

  const setQueueRefreshPrompt = (visible) => {{
    pendingQueueRefresh = visible;
    if (queueRefreshButton) {{
      queueRefreshButton.hidden = !visible;
    }}
  }};

  const saveDraftInBackground = async (draftId, draftText) => {{
    const body = new URLSearchParams();
    body.set('draft_id', String(draftId));
    body.set('action', 'save_text');
    body.set('draft_text', draftText);
    const response = await fetch('/draft', {{
      method: 'POST',
      headers: {{
        'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
      }},
      body: body.toString(),
      credentials: 'same-origin',
    }});
    if (!response.ok) {{
      throw new Error(`Draft save failed with status ${{response.status}}`);
    }}
  }};

  const initializeQueue = () => {{
    const queueRoot = document.querySelector('[data-queue-root]');
    if (!queueRoot) {{
      return null;
    }}
    const queueCards = Array.from(document.querySelectorAll('[data-queue-card]'));
    const queueJumps = Array.from(document.querySelectorAll('[data-queue-jump]'));
    const queueRail = queueRoot.querySelector('.queue-rail');
    const queueEmpty = document.querySelector('[data-queue-empty]');
    const queueCounter = document.querySelector('[data-queue-counter]');
    const defaultOrder = queueCards.map((_, index) => index);
    const idToIndex = new Map(queueCards.map((card, index) => [card.dataset.cardId, index]));
    const jumpById = new Map(queueJumps.map((button) => [button.dataset.jumpTo, button]));
    const buildQueueOrder = (rawOrder) => {{
      const nextOrder = [];
      const seen = new Set();
      (rawOrder || []).forEach((cardId) => {{
        const key = String(cardId);
        const match = idToIndex.get(key);
        if (match === undefined || seen.has(match)) {{
          return;
        }}
        seen.add(match);
        nextOrder.push(match);
      }});
      defaultOrder.forEach((index) => {{
        if (!seen.has(index)) {{
          nextOrder.push(index);
        }}
      }});
      return nextOrder;
    }};
    let queueOrder = defaultOrder.slice();
    let activeIndex = 0;
    try {{
      const savedQueue = JSON.parse(window.sessionStorage.getItem(queueStateKey) || 'null');
      if (savedQueue && Array.isArray(savedQueue.order)) {{
        queueOrder = buildQueueOrder(savedQueue.order);
      }}
      if (savedQueue && typeof savedQueue.activeCardId === 'string') {{
        const savedCardIndex = idToIndex.get(savedQueue.activeCardId);
        const savedOrderPosition = savedCardIndex === undefined ? -1 : queueOrder.indexOf(savedCardIndex);
        if (savedOrderPosition >= 0) {{
          activeIndex = savedOrderPosition;
        }} else if (typeof savedQueue.activePosition === 'number') {{
          activeIndex = savedQueue.activePosition;
        }}
      }} else if (savedQueue && typeof savedQueue.activePosition === 'number') {{
        activeIndex = savedQueue.activePosition;
      }}
    }} catch (_err) {{
      queueOrder = defaultOrder.slice();
      activeIndex = 0;
    }}
    const persistQueue = () => {{
      if (!queueOrder.length) {{
        window.sessionStorage.removeItem(queueStateKey);
        return;
      }}
      const currentCardIndex = queueOrder[Math.min(Math.max(activeIndex, 0), queueOrder.length - 1)];
      const currentCardId = queueCards[currentCardIndex]?.dataset.cardId || '';
      window.sessionStorage.setItem(
        queueStateKey,
        JSON.stringify({{
          order: queueOrder.map((index) => queueCards[index]?.dataset.cardId || ''),
          activeCardId: currentCardId,
          activePosition: activeIndex,
        }}),
      );
    }};
    const renderQueueRail = () => {{
      if (!queueRail) {{
        return;
      }}
      queueOrder.forEach((cardIndex) => {{
        const cardId = queueCards[cardIndex]?.dataset.cardId;
        const button = cardId ? jumpById.get(cardId) : null;
        if (button) {{
          queueRail.appendChild(button);
        }}
      }});
    }};
    const syncQueue = () => {{
      if (!queueOrder.length) {{
        queueCards.forEach((card) => card.classList.remove('is-active'));
        if (queueEmpty) {{
          queueEmpty.hidden = false;
        }}
        if (queueCounter) {{
          queueCounter.textContent = `0 of 0 in queue`;
        }}
        persistQueue();
      }} else {{
        activeIndex = Math.min(Math.max(activeIndex, 0), queueOrder.length - 1);
        const currentCardIndex = queueOrder[activeIndex];
        queueCards.forEach((card, index) => {{
          card.classList.toggle('is-active', index === currentCardIndex);
          card.classList.remove('is-skipped');
        }});
        queueJumps.forEach((button) => {{
          const isActive = queueCards[currentCardIndex] && queueCards[currentCardIndex].dataset.cardId === button.dataset.jumpTo;
          button.classList.toggle('is-active', isActive);
          button.classList.remove('is-skipped');
        }});
        renderQueueRail();
        if (queueEmpty) {{
          queueEmpty.hidden = true;
        }}
        if (queueCounter) {{
          queueCounter.textContent = `${{activeIndex + 1}} of ${{queueOrder.length}} in queue`;
        }}
        persistQueue();
      }}
    }};
    const activateCard = (cardId) => {{
      const nextIndex = queueCards.findIndex((card) => card.dataset.cardId === String(cardId));
      if (nextIndex === -1) {{
        syncQueue();
        return;
      }}
      const orderPosition = queueOrder.indexOf(nextIndex);
      activeIndex = orderPosition === -1 ? 0 : orderPosition;
      syncQueue();
    }};
    const moveBy = (direction) => {{
      if (!queueOrder.length) {{
        return;
      }}
      const nextOffset = Math.min(Math.max(activeIndex + direction, 0), queueOrder.length - 1);
      activeIndex = nextOffset;
      syncQueue();
    }};
    queueJumps.forEach((button) => {{
      button.onclick = () => {{
        activateCard(button.dataset.jumpTo);
        saveScroll();
        const card = document.getElementById(`candidate-${{button.dataset.jumpTo}}`);
        if (card) {{
          card.scrollIntoView({{ block: 'start', behavior: 'smooth' }});
        }}
      }};
    }});
    document.querySelectorAll('[data-skip-card]').forEach((button) => {{
      button.onclick = async () => {{
        const card = button.closest('[data-queue-card]');
        if (!card) {{
          return;
        }}
        const editor = card.querySelector('[data-draft-editor]');
        const draftIdField = card.querySelector('input[name="draft_id"]');
        if (editor && draftIdField && editor.dataset.dirty === 'true') {{
          try {{
            showBusy('Saving draft and skipping...');
            await saveDraftInBackground(draftIdField.value, editor.value);
            editor.dataset.dirty = 'false';
          }} catch (_err) {{
            hideBusy();
            return;
          }}
        }}
        if (queueOrder.length > 1) {{
          const currentCardIndex = queueCards.findIndex((item) => item === card);
          const orderPosition = queueOrder.indexOf(currentCardIndex);
          if (orderPosition !== -1) {{
            const [rotated] = queueOrder.splice(orderPosition, 1);
            queueOrder.push(rotated);
            activeIndex = orderPosition >= queueOrder.length ? 0 : orderPosition;
          }}
        }}
        syncQueue();
        hideBusy();
      }};
    }});
    document.querySelectorAll('[data-queue-prev]').forEach((button) => {{
      button.onclick = () => moveBy(-1);
    }});
    document.querySelectorAll('[data-queue-next]').forEach((button) => {{
      button.onclick = () => moveBy(1);
    }});
    const restartDeck = document.querySelector('[data-restart-deck]');
    if (restartDeck) {{
      restartDeck.onclick = () => {{
        queueOrder.sort((left, right) => left - right);
        activeIndex = 0;
        syncQueue();
      }};
    }}
    syncQueue();
    return {{
      version: queueRoot.dataset.queueVersion || '',
    }};
  }};

  const applyQueueSnapshot = (snapshot) => {{
    const queueRoot = document.querySelector('[data-queue-root]');
    if (!queueRoot) {{
      return;
    }}
    queueRoot.dataset.queueVersion = snapshot.version || '';
    const stage = queueRoot.querySelector('.queue-stage');
    const rail = queueRoot.querySelector('.queue-rail');
    if (stage) {{
      stage.innerHTML = snapshot.stage_html || '';
    }}
    if (rail) {{
      rail.innerHTML = `<div class="queue-rail-label">Jump List</div>${{snapshot.rail_html || ''}}`;
    }}
    initializeQueue();
    setQueueRefreshPrompt(false);
  }};

  const fetchQueueSnapshot = async () => {{
    const response = await fetch('/queue-fragment', {{ cache: 'no-store' }});
    if (!response.ok) {{
      throw new Error(`Queue refresh failed with status ${{response.status}}`);
    }}
    return await response.json();
  }};

  const fetchHeroSnapshot = async () => {{
    const response = await fetch('/hero-status', {{ cache: 'no-store' }});
    if (!response.ok) {{
      throw new Error(`Hero refresh failed with status ${{response.status}}`);
    }}
    return await response.json();
  }};

  const maybeRefreshQueue = async (force = false) => {{
    const queueRoot = document.querySelector('[data-queue-root]');
    if (!queueRoot) {{
      return;
    }}
    const currentVersion = queueRoot.dataset.queueVersion || '';
    const snapshot = await fetchQueueSnapshot();
    if ((snapshot.version || '') === currentVersion) {{
      if (force) {{
        setQueueRefreshPrompt(false);
      }}
      return;
    }}
    if (!force && hasDirtyDraftEditors()) {{
      setQueueRefreshPrompt(true);
      return;
    }}
    applyQueueSnapshot(snapshot);
  }};

  initializeQueue();
  if (queueRefreshButton) {{
    queueRefreshButton.onclick = () => {{
      maybeRefreshQueue(true).catch(() => {{}});
    }};
  }}
  window.setInterval(() => {{
    if (document.visibilityState !== 'visible') {{
      return;
    }}
    maybeRefreshQueue(false).catch(() => {{}});
  }}, 7000);
  const workerBadge = document.querySelector('[data-worker-badge]');
  const el = document.getElementById('next-run-countdown');
  let nextRunTarget = el?.dataset.nextRun ? new Date(el.dataset.nextRun).getTime() : Number.NaN;
  const renderCountdown = () => {{
    if (!el) {{
      return;
    }}
    if (!Number.isFinite(nextRunTarget)) {{
      el.textContent = 'Next run: n/a';
      return;
    }}
    const delta = Math.max(0, nextRunTarget - Date.now());
    if (delta === 0) {{
      el.textContent = 'Next run: due now';
      return;
    }}
    const totalSeconds = Math.floor(delta / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    const hours = Math.floor(minutes / 60);
    const remMinutes = minutes % 60;
    if (hours > 0) {{
      el.textContent = `Next run: ${{hours}}h ${{remMinutes}}m ${{seconds}}s`;
    }} else {{
      el.textContent = `Next run: ${{minutes}}m ${{seconds}}s`;
    }}
  }};
  renderCountdown();
  window.setInterval(renderCountdown, 1000);
  const refreshHero = async () => {{
    const snapshot = await fetchHeroSnapshot();
    if (workerBadge && snapshot.badge_html) {{
      workerBadge.innerHTML = snapshot.badge_html;
    }}
    if (el) {{
      el.dataset.nextRun = snapshot.next_run_at || '';
      nextRunTarget = snapshot.next_run_at ? new Date(snapshot.next_run_at).getTime() : Number.NaN;
      if (!Number.isFinite(nextRunTarget)) {{
        el.textContent = `Next run: ${{snapshot.next_run_text || 'n/a'}}`;
      }} else {{
        renderCountdown();
      }}
    }}
  }};
  window.setInterval(() => {{
    if (document.visibilityState !== 'visible') {{
      return;
    }}
    refreshHero().catch(() => {{}});
  }}, 7000);
  const liveNote = document.querySelector('[data-live-note]');
  const refreshButton = document.querySelector('[data-soft-refresh]');
  const pageLoadedAt = Date.now();
  const renderLiveNote = () => {{
    if (!liveNote) {{
      return;
    }}
    const elapsedSeconds = Math.floor((Date.now() - pageLoadedAt) / 1000);
    if (elapsedSeconds < 60) {{
      liveNote.textContent = `Reply queue and worker status update live. Full dashboard snapshot age: ${{elapsedSeconds}}s.`;
      return;
    }}
    const elapsedMinutes = Math.floor(elapsedSeconds / 60);
    liveNote.textContent = `Reply queue and worker status update live. Full dashboard snapshot age: ${{elapsedMinutes}}m. Use Refresh Dashboard when you want everything else refreshed.`;
  }};
  renderLiveNote();
  window.setInterval(renderLiveNote, 1000);
  if (refreshButton) {{
    refreshButton.addEventListener('click', () => {{
      saveScroll();
      window.location.reload();
    }});
  }}
}})();
</script>
</html>"""


def _queue_jump_button(row: sqlite3.Row) -> str:
    preview = (row["text"] or "").strip().replace("\n", " ")
    if len(preview) > 96:
        preview = f"{preview[:93]}..."
    badge = f"{_escape(_status_label(row['status']))} | {_fmt_age(row['posted_at'])}"
    if row["draft_id"]:
        badge = f"{badge} | draft #{row['draft_id']}"
    return (
        f'<button type="button" class="queue-jump" data-queue-jump data-jump-to="{row["id"]}">'
        f"<strong>@{_escape(row['author_handle'])}</strong>"
        f"<small>{_escape(badge)}</small>"
        f"<small>{_escape(preview or 'No text captured for this tweet.')}</small>"
        "</button>"
    )


def _overview_stats(database_path: Path, live_queue_count: int) -> list[dict[str, str]]:
    conn = sqlite3.connect(database_path)
    try:
        collected = int(conn.execute("select count(*) from scraped_posts").fetchone()[0])
        reply_drafts = int(
            conn.execute("select count(*) from drafts where draft_type in ('reply', 'quote_reply')").fetchone()[0]
        )
        original_drafts = int(
            conn.execute(
                """
                select count(*)
                from drafts
                where draft_type = 'original'
                """
            ).fetchone()[0]
        )
        handled_manually = int(
            conn.execute(
                """
                select count(*)
                from drafts
                where status in ('manual_posted', 'approved_local', 'posted')
                """
            ).fetchone()[0]
        )
        rejected = int(conn.execute("select count(*) from drafts where status = 'rejected'").fetchone()[0])
    finally:
        conn.close()

    return [
        {"label": "Tweets Collected", "value": str(collected), "detail": "Saved from your sources."},
        {"label": "Live Queue", "value": str(live_queue_count), "detail": "Waiting for review."},
        {"label": "Reply Drafts", "value": str(reply_drafts), "detail": "Generated from candidate tweets."},
        {"label": "Original Drafts", "value": str(original_drafts), "detail": "Standalone posts ready to polish."},
        {"label": "Handled Manually", "value": str(handled_manually), "detail": "Copied out and finished on X."},
        {"label": "Rejected Drafts", "value": str(rejected), "detail": "Useful for voice tuning later."},
    ]


def _overview_stat_card(label: str, value: str, detail: str) -> str:
    return (
        '<div class="overview-card">'
        f'<div class="overview-label">{_escape(label)}</div>'
        f'<div class="overview-value">{_escape(value)}</div>'
        f'<p class="overview-detail">{_escape(detail)}</p>'
        "</div>"
    )


def _voice_compare_html(current_text: str, proposal_text: str, current_label: str) -> str:
    return (
        '<div class="voice-compare">'
        '<section class="voice-compare-panel">'
        '<div class="voice-compare-label">Current Voice</div>'
        f'<div class="voice-compare-path">{_escape(current_label)}</div>'
        f'<pre>{_escape(current_text.strip() or "No current voice text found.")}</pre>'
        '</section>'
        '<section class="voice-compare-panel">'
        '<div class="voice-compare-label">Proposed Update</div>'
        '<div class="voice-compare-path">Review before applying</div>'
        f'<pre>{_escape(proposal_text.strip() or "No proposed voice text found.")}</pre>'
        '</section>'
        '</div>'
    )


def _fmt_cadence_range(min_minutes: int, max_minutes: int) -> str:
    if min_minutes == max_minutes:
        return f"{min_minutes} min"
    return f"{min_minutes}-{max_minutes} min"


def _voice_review_card(voice_review: dict[str, Any], drafting_enabled: bool) -> str:
    latest = voice_review.get("latest") or {}
    pending = voice_review.get("pending") or {}
    meta_bits = [
        (
            '<span class="voice-review-pill">'
            f'<strong>{_escape(str(voice_review.get("new_examples", 0)))}</strong>'
            '<span>New examples waiting</span>'
            "</span>"
        ),
    ]
    if latest:
        meta_bits.append(
            (
                '<span class="voice-review-pill">'
                f'<strong>{_escape(_fmt_time(str(latest.get("created_at") or "")))}</strong>'
                '<span>Last review ran</span>'
                "</span>"
            )
        )
        meta_bits.append(
            (
                '<span class="voice-review-pill">'
                f'<strong>{_escape(str(latest.get("status") or "unknown"))}</strong>'
                '<span>Last proposal status</span>'
                "</span>"
            )
        )
    meta_html = "".join(meta_bits)
    run_button = _post_button(
        "/voice-review",
        "action",
        "run",
        None,
        None,
        "Build Voice Update",
        "ok",
        "Building voice update...",
        disabled=not drafting_enabled,
        disabled_reason="Configure the selected AI provider before building a voice update.",
    )

    if not pending:
        return (
            '<div class="voice-review-card">'
            '<div class="voice-review-top">'
            '<div class="voice-review-heading">'
            '<h3 class="voice-review-title">No voice update waiting</h3>'
            '<p class="voice-review-summary">When enough examples build up, Clearfeed can turn your edits and decisions into a reviewed <code>Voice.md</code> update.</p>'
            "</div>"
            f'<div class="voice-review-actions">{run_button}</div>'
            "</div>"
            '<div class="voice-review-empty">Best results come from editing drafts here before you save them or mark them posted. Those edits give Adaptive Voice something concrete to learn from.</div>'
            f'<div class="voice-review-meta">{meta_html}</div>'
            "</div>"
        )

    approve_button = _post_button(
        "/voice-review",
        "action",
        "approve",
        "proposal_id",
        str(pending["id"]),
        "Apply Voice Update",
        "ok",
        "Applying voice update...",
    )
    reject_button = _post_button(
        "/voice-review",
        "action",
        "reject",
        "proposal_id",
        str(pending["id"]),
        "Reject Proposal",
        "bad",
        "Rejecting proposal...",
    )
    diff_html = (
        '<div class="voice-diff">'
        '<details>'
        '<summary>Raw diff</summary>'
        f'<pre>{_escape(str(pending.get("diff_text") or ""))}</pre>'
        "</details>"
        "</div>"
    )
    compare_html = _voice_compare_html(
        str(voice_review.get("current_voice_text") or ""),
        str(pending.get("proposal_text") or ""),
        str(voice_review.get("current_voice_path") or "profiles/local/Voice.md"),
    )
    return (
        '<div class="voice-review-card">'
        '<div class="voice-review-top">'
        '<div class="voice-review-heading">'
        f'<h3 class="voice-review-title">Voice update #{_escape(str(pending["id"]))} is ready</h3>'
        f'<p class="voice-review-summary">{_escape(str(pending.get("summary_text") or ""))}</p>'
        "</div>"
        f'<div class="voice-review-actions">{approve_button}{reject_button}</div>'
        "</div>"
        f'<div class="voice-review-meta">{meta_html}'
        '<span class="voice-review-pill">'
        f'<strong>{_escape(str(pending.get("sample_count") or 0))}</strong>'
        '<span>Samples used</span>'
        "</span>"
        '<span class="voice-review-pill">'
        f'<strong>{_escape(_fmt_time(str(pending.get("created_at") or "")))}</strong>'
        '<span>Created at</span>'
        "</span>"
        "</div>"
        f"{compare_html}"
        f"{diff_html}"
        "</div>"
    )


def _archive_voice_card(archive_voice: dict[str, Any], drafting_enabled: bool) -> str:
    latest_import = archive_voice.get("latest_import") or {}
    latest_summary = archive_voice.get("latest_summary") or {}
    latest = archive_voice.get("latest") or {}
    pending = archive_voice.get("pending") or {}
    latest_status = str(latest.get("status") or "not run yet").replace("_", " ")
    latest_archive_name = str(latest_import.get("archive_name") or "No archive imported")
    latest_item_count = int(latest_import.get("item_count") or 0)
    latest_summary_count = int(latest_summary.get("item_count") or latest_item_count or 0)
    imported_at = _fmt_time(str(latest_import.get("imported_at") or "")) if latest_import else "Not imported yet"
    summary_built_at = _fmt_time(str(latest_summary.get("created_at") or "")) if latest_summary else "Not built yet"
    latest_reviewed_at = _fmt_time(str(latest.get("reviewed_at") or latest.get("created_at") or "")) if latest else "Not run yet"

    summary_title = "Craft Voice.md from X Archive"
    summary_copy = "Optional. Import an unzipped X archive to build a stronger starting Voice.md from your real posts."
    if pending:
        summary_title = "Archive-based Voice.md update ready"
        summary_copy = "A reviewable archive-based update is ready. Open this to compare it and decide whether to apply it."
    elif latest_import:
        summary_title = "Refresh Voice.md from X Archive"
        summary_copy = f"{latest_item_count} posts imported from {latest_archive_name}. Open this when you want to refresh your base voice."
    open_attr = " open" if (not latest_import or pending) else ""

    import_button = _post_button("/archive", "action", "import", None, None, "Import Archive", "ok", "Importing archive...")
    run_button = _post_button(
        "/archive",
        "action",
        "run",
        None,
        None,
        "Craft Voice.md Update",
        "",
        "Crafting archive-based Voice.md update...",
        disabled=(not drafting_enabled or not latest_import),
        disabled_reason=(
            "Import an archive and configure the selected AI provider first."
            if not drafting_enabled or not latest_import
            else ""
        ),
    )

    input_block = (
        '<form method="post" action="/archive" class="candidate-form">'
        '<input type="hidden" name="action" value="import">'
        '<input type="text" name="archive_dir" placeholder="Paste the path to your unzipped X archive folder">'
        '<div class="controls">'
        f'{import_button}'
        f'{run_button}'
        "</div>"
        "</form>"
    )

    summary_block = (
        f'<details class="archive-details"{open_attr}>'
        '<summary>'
        '<span class="dev-summary-copy">'
        f'<span class="dev-summary-title">{_escape(summary_title)}</span>'
        f'<span class="dev-summary-subtitle">{_escape(summary_copy)}</span>'
        '</span>'
        '<span class="dev-chevron" aria-hidden="true">&#9662;</span>'
        '</summary>'
        '<div class="archive-body">'
    )

    if not pending:
        summary_excerpt = _escape(str(latest_summary.get("summary_text") or ""))
        if len(summary_excerpt) > 420:
            summary_excerpt = summary_excerpt[:417] + "..."
        summary_lines = []
        if latest_import:
            summary_lines.append(
                f'<span class="voice-review-pill"><strong>{latest_item_count}</strong><span>Imported posts</span></span>'
            )
            summary_lines.append(
                f'<span class="voice-review-pill"><strong>{latest_summary_count}</strong><span>Used in latest summary</span></span>'
            )
            summary_lines.append(
                f'<span class="voice-review-pill"><strong>{_escape(latest_reviewed_at)}</strong><span>Last archive proposal</span></span>'
            )
        elif not latest_import:
            summary_lines.append(
                '<span class="archive-inline-chip"><strong>Optional</strong> You only need this if you want Clearfeed to learn from your older post history.</span>'
            )
        summary_html = (
            '<div class="voice-review-empty">'
            '<strong>Latest archive read:</strong> '
            f'{summary_excerpt}'
            "</div>"
            if latest_summary
            else '<div class="voice-review-empty">No archive imported yet. Paste the path to an unzipped X archive when you want Clearfeed to learn from your historical posts.</div>'
        )
        summary_lines_html = f'<div class="voice-review-meta">{"".join(summary_lines)}</div>' if summary_lines else ""
        return (
            f"{summary_block}"
            f"{input_block}"
            f"{summary_lines_html}"
            f"{summary_html}"
            '</details>'
        )

    approve_button = _post_button("/archive", "action", "approve", "proposal_id", str(pending["id"]), "Apply Archive Voice", "ok", "Applying archive voice...")
    reject_button = _post_button("/archive", "action", "reject", "proposal_id", str(pending["id"]), "Reject Proposal", "bad", "Rejecting proposal...")
    compare_html = _voice_compare_html(
        str(archive_voice.get("current_voice_text") or ""),
        str(pending.get("proposal_text") or ""),
        str(archive_voice.get("current_voice_path") or "profiles/local/Voice.md"),
    )
    return (
        f"{summary_block}"
        '<div class="voice-review-top">'
        '<div class="voice-review-heading">'
        f'<h3 class="voice-review-title">Archive-based Voice.md update #{_escape(str(pending["id"]))} is ready</h3>'
        f'<p class="voice-review-summary">{_escape(str(pending.get("summary_text") or ""))}</p>'
        "</div>"
        f'<div class="voice-review-actions">{approve_button}{reject_button}</div>'
        "</div>"
        f"{input_block}"
        f'<div class="voice-review-meta"><span class="voice-review-pill"><strong>{_escape(str(pending.get("sample_count") or 0))}</strong><span>Archive items used</span></span></div>'
        f"{compare_html}"
        f'<div class="voice-diff"><details><summary>Raw diff</summary><pre>{_escape(str(pending.get("diff_text") or ""))}</pre></details></div>'
        '</details>'
    )


def _setup_status_row(label: str, ok: bool, detail: str) -> str:
    badge_class = "badge-mini-ok" if ok else "badge-mini-warn"
    badge_text = "Ready" if ok else "Needs Setup"
    return (
        '<div class="setup-item">'
        '<div class="setup-item-head">'
        f"<strong>{_escape(label)}</strong>"
        f'<span class="badge-mini {badge_class}">{badge_text}</span>'
        "</div>"
        f"<p>{_escape(detail)}</p>"
        "</div>"
    )


def _candidate_card(
    row: sqlite3.Row,
    draft_text_limit: int,
    drafting_enabled: bool,
    image_generation_enabled: bool,
) -> str:
    metrics = _metrics_text(row["raw_metrics"])
    draft_badge = ""
    if row["draft_id"]:
        draft_badge = f'<span class="pill {_pill_class(row["draft_status"])}">draft #{row["draft_id"]} | {_escape(row["draft_status"])}</span>'
    draft_count = int(row["draft_count"] or 0)
    attempts = "No drafts yet" if draft_count == 0 else f"{draft_count} draft{'s' if draft_count != 1 else ''} on this tweet"
    tweet_text = _escape((row["text"] or "").strip() or "No text captured for this tweet.")
    return (
        f'<article class="queue-card" id="candidate-{row["id"]}" data-queue-card data-card-id="{row["id"]}">'
        '<div class="queue-card-main">'
        '<section class="tweet-shell">'
        '<div class="eyebrow">'
        f'<span class="pill">{_escape(row["source_key"])}</span>'
        f'<span class="pill pill-accent">{_escape(row["recommended_action"])}</span>'
        f'<span class="pill {_pill_class(row["status"])}">{_escape(row["status"])}</span>'
        f"{draft_badge}"
        '</div>'
        '<div class="tweet-head">'
        f'<a class="tweet-author" href="{row["url"]}" target="_blank">@{_escape(row["author_handle"])}</a>'
        f'<span class="tweet-age">{_fmt_age(row["posted_at"])} old</span>'
        '</div>'
        f'<p class="tweet-body">{tweet_text}</p>'
        '<div class="tweet-footer">'
        f"<span>{_escape(metrics)}</span>"
        f'<a href="{row["url"]}" target="_blank">Open tweet</a>'
        '</div>'
        '</section>'
        f"{_candidate_action_form(row, drafting_enabled)}"
        f"{_candidate_draft_panel(row, draft_text_limit, drafting_enabled, image_generation_enabled)}"
        '</div>'
        '<aside class="queue-card-side">'
        '<section class="side-panel">'
        '<div class="side-kicker">Priority</div>'
        f'<div class="score-value">{row["total_score"]:.1f}</div>'
        f'<div class="meta"><strong>Status:</strong> {_escape(row["status"])}<br><strong>Suggested:</strong> {_escape(row["recommended_action"])}<br><strong>Attempts:</strong> {_escape(attempts)}</div>'
        '</section>'
        '<section class="side-panel">'
        '<div class="side-kicker">Why This Surfaced</div>'
        f'<p class="side-copy">{_escape(row["why"] or "No rationale recorded.")}</p>'
        '</section>'
        '</aside>'
        '</article>'
    )


def _candidate_action_form(row: sqlite3.Row, drafting_enabled: bool) -> str:
    guidance_value = _escape(str(row["draft_generation_notes"] or ""))
    drafting_disabled_attr = (
        ' disabled title="Configure the selected AI provider in .env to enable drafting."'
        if not drafting_enabled
        else ""
    )
    drafting_note = (
        ""
        if drafting_enabled
        else '<div class="inline-warning">Drafting is disabled until the selected AI provider is configured.</div>'
    )
    return (
        '<form method="post" action="/candidate" class="candidate-form">'
        f'<input type="hidden" name="candidate_id" value="{row["id"]}">'
        f'<label class="candidate-guidance-label" for="candidate-guidance-{row["id"]}">Draft Brief</label>'
        f'<textarea id="candidate-guidance-{row["id"]}" name="draft_guidance" class="candidate-guidance" placeholder="What are you thinking? Add the angle, objection, or structure you want the draft to lean into.">{guidance_value}</textarea>'
        '<div class="candidate-guidance-help">This steers the next draft only. Leave it blank to let the agent choose the strongest angle from the tweet context.</div>'
        '<div class="candidate-actions">'
        f'<button class="ok" type="submit" name="action" value="draft_reply" data-busy-label="Drafting reply..."{drafting_disabled_attr}>Draft Reply</button>'
        f'<button type="submit" name="action" value="draft_quote" data-busy-label="Drafting quote reply..."{drafting_disabled_attr}>Draft Quote</button>'
        '<button type="submit" name="action" value="watch" data-busy-label="Saving watch status...">Watch</button>'
        '<button class="bad" type="submit" name="action" value="ignore" data-busy-label="Dismissing candidate...">Dismiss</button>'
        '<button type="button" class="ghost-button" data-skip-card>Skip For Now</button>'
        '</div>'
        f"{drafting_note}"
        '</form>'
    )


def _candidate_draft_panel(
    row: sqlite3.Row,
    draft_text_limit: int,
    drafting_enabled: bool,
    image_generation_enabled: bool,
) -> str:
    if not row["draft_id"]:
        return (
            '<section class="draft-inline draft-inline-empty">'
            '<div class="draft-inline-header">'
            '<div><h3>Reply Draft</h3><div class="draft-inline-note">Nothing drafted yet. Use Draft Reply or Draft Quote and the response will appear here under the tweet.</div></div>'
            '</div>'
            '</section>'
        )
    draft_id = int(row["draft_id"])
    draft_status = str(row["draft_status"] or "")
    note_bits = [
        _escape(row["draft_type"] or "draft"),
        _escape(_status_label(draft_status or "unknown")),
        _fmt_time(row["draft_updated_at"]),
    ]
    draft_count = int(row["draft_count"] or 0)
    if draft_count > 1:
        note_bits.append(f"{draft_count} attempts")
    if row["draft_image_path"]:
        note_bits.append("image attached")
    guidance_html = ""
    if row["draft_generation_notes"]:
        guidance_html = (
            '<div class="draft-guidance-note"><strong>Custom brief used:</strong><br>'
            f'{_escape(row["draft_generation_notes"])}'
            "</div>"
        )
    controls = _draft_action_buttons(
        draft_id=draft_id,
        status=draft_status,
        image_prompt=row["draft_image_prompt"],
        image_path=row["draft_image_path"],
        drafting_enabled=drafting_enabled,
        image_generation_enabled=image_generation_enabled,
    )
    return (
        f'<section class="draft-inline" id="draft-{draft_id}">'
        '<div class="draft-inline-header">'
        f'<div><h3>Latest Draft #{draft_id}</h3><div class="draft-inline-note">{" | ".join(note_bits)}</div></div>'
        f'<span class="pill {_pill_class(draft_status)}">{_escape(_status_label(draft_status))}</span>'
        '</div>'
        f"{guidance_html}"
        f"{_draft_text_editor(draft_id, row['draft_text'] or '', draft_status, draft_text_limit)}"
        f"{controls}"
        '</section>'
    )


def _original_draft_card(
    row: sqlite3.Row,
    draft_text_limit: int,
    drafting_enabled: bool,
    image_generation_enabled: bool,
) -> str:
    controls = _draft_action_buttons(
        draft_id=int(row["id"]),
        status=str(row["status"] or ""),
        image_prompt=row["image_prompt"],
        image_path=row["image_path"],
        drafting_enabled=drafting_enabled,
        image_generation_enabled=image_generation_enabled,
    )
    note_bits = [
        _escape(row["draft_type"] or "original"),
        _escape(_status_label(row["status"] or "unknown")),
        _fmt_time(row["updated_at"]),
    ]
    if row["image_path"]:
        note_bits.append("image attached")
    return (
        f'<article class="original-card" id="draft-{row["id"]}">'
        '<div class="draft-inline-header">'
        f'<div><h3>Original Draft #{row["id"]}</h3><div class="draft-inline-note">{" | ".join(note_bits)}</div></div>'
        f'<span class="pill {_pill_class(row["status"])}">{_escape(_status_label(row["status"]))}</span>'
        '</div>'
        f"{_draft_text_editor(int(row['id']), row['draft_text'] or '', str(row['status'] or ''), draft_text_limit)}"
        f"{controls}"
        '</article>'
    )


def _draft_action_buttons(
    draft_id: int,
    status: str,
    image_prompt: Any,
    image_path: Any,
    drafting_enabled: bool,
    image_generation_enabled: bool,
) -> str:
    if status != "drafted":
        return ""
    controls = ['<div class="draft-inline-actions">']
    controls.append(_copy_button(draft_id, "Copy Draft"))
    controls.append(
        _post_button("/draft", "draft_id", draft_id, "action", "manual", "Mark Posted", "ok", "Marking posted...")
    )
    controls.append(_post_button("/draft", "draft_id", draft_id, "action", "reject", "Reject", "bad", "Rejecting draft..."))
    if image_prompt and not image_path:
        controls.append(
            _post_button(
                "/draft",
                "draft_id",
                draft_id,
                "action",
                "image",
                "Generate Image",
                "",
                "Generating image...",
                disabled=(not drafting_enabled or not image_generation_enabled),
                disabled_reason="Configure a provider and AI_IMAGE_MODEL to enable image generation.",
            )
        )
    controls.append("</div>")
    return "".join(controls)


def _post_button(
    path: str,
    id_key: str,
    id_value: Any,
    action_key: str,
    action_value: str,
    label: str,
    css: str = "",
    busy_label: str | None = None,
    disabled: bool = False,
    disabled_reason: str | None = None,
) -> str:
    hidden_fields = [f'<input type="hidden" name="{id_key}" value="{id_value}">']
    if action_key is not None:
        hidden_fields.append(f'<input type="hidden" name="{action_key}" value="{action_value}">')
    if path == "/draft":
        hidden_fields.append('<input type="hidden" name="draft_text" value="">')
    css_class = f' class="{css}"' if css else ""
    busy_attr = f' data-busy-label="{_escape(busy_label)}"' if busy_label else ""
    disabled_attr = " disabled" if disabled else ""
    title_attr = f' title="{_escape(disabled_reason or "")}"' if disabled and disabled_reason else ""
    return (
        f'<form method="post" action="{path}">'
        f"{''.join(hidden_fields)}"
        f'<button{css_class}{busy_attr}{disabled_attr}{title_attr} type="submit">{label}</button>'
        "</form>"
    )


def _copy_button(draft_id: int, label: str) -> str:
    return (
        '<button type="button" class="ghost-button" '
        f'data-copy-draft data-draft-id="{draft_id}" data-copy-label="{_escape(label)}">{label}</button>'
    )


def _draft_text_editor(draft_id: int, draft_text: str, status: str, draft_text_limit: int) -> str:
    editor_id = f"draft-text-{draft_id}"
    limit_attr = f' data-limit="{draft_text_limit}"' if draft_text_limit else ""
    counter_text = f"{len(draft_text)} / {draft_text_limit}" if draft_text_limit else f"{len(draft_text)} chars"
    if status != "drafted":
        return (
            '<div class="draft-form">'
            f'<textarea id="{editor_id}" class="draft-editor" rows="8" data-draft-editor{limit_attr} readonly>{_escape(draft_text)}</textarea>'
            '<div class="draft-meta">'
            '<span>Read only</span>'
            f'<span data-char-count-for="{editor_id}">{counter_text}</span>'
            "</div>"
            "</div>"
        )
    maxlength_attr = f' maxlength="{draft_text_limit}"' if draft_text_limit else ""
    return (
        f'<form method="post" action="/draft" class="draft-form" data-draft-form>'
        f'<input type="hidden" name="draft_id" value="{draft_id}">'
        '<input type="hidden" name="action" value="save_text">'
        f'<textarea id="{editor_id}" name="draft_text" class="draft-editor" rows="8" data-draft-editor{limit_attr}{maxlength_attr}>{_escape(draft_text)}</textarea>'
        '<div class="draft-meta">'
        f'<span data-char-count-for="{editor_id}">{counter_text}</span>'
        '<button type="submit" data-busy-label="Saving draft...">Save For Later</button>'
        "</div>"
        "</form>"
    )


def _pill_class(status: Any) -> str:
    normalized = str(status or "").lower()
    if normalized in {"posted", "drafted", "running", "sleeping", "approved_local"}:
        return "pill-ok"
    if normalized in {"manual_posted", "watched", "alerted", "new"}:
        return "pill-warn"
    if normalized in {"ignored", "rejected", "failed", "error"}:
        return "pill-bad"
    if normalized in {"reply", "quote_reply"}:
        return "pill-accent"
    return ""


def _status_label(status: Any) -> str:
    normalized = str(status or "").lower()
    labels = {
        "new": "New",
        "alerted": "Queued",
        "watched": "Watched",
        "drafted": "Draft Ready",
        "manual_posted": "Marked Posted",
        "rejected": "Rejected",
        "ignored": "Dismissed",
        "approved_local": "Handled",
        "posted": "Handled",
        "running": "Running",
        "sleeping": "Sleeping",
        "error": "Error",
        "reply": "Reply",
        "quote_reply": "Quote Reply",
        "original": "Original",
    }
    return labels.get(normalized, str(status or "unknown"))


def _tail_file(path: Path, lines: int = 25) -> str:
    if not path.exists():
        return "Not available."
    content = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if not content:
        return "Empty."
    return "\n".join(content[-lines:])


def _process_rows() -> list[dict[str, str]]:
    script = r"""
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
Where-Object {
  ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and
  $_.CommandLine -and
  ($_.CommandLine -like '*run_worker.py*' -or $_.CommandLine -like '*run_dashboard.py*')
} | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress
"""
    output = _run_powershell(script)
    if not output.strip():
        return []
    payload = json.loads(output)
    if isinstance(payload, dict):
        payload = [payload]
    return [
        {
            "pid": str(item.get("ProcessId", "")),
            "name": str(item.get("Name", "")),
            "command": str(item.get("CommandLine", "")),
        }
        for item in payload
    ]


def _scheduled_task_info(
    status_next_run_at: str | None,
    worker_min_delay_minutes: int,
    worker_max_delay_minutes: int,
) -> dict[str, str]:
    script = """
$task = Get-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue
$info = Get-ScheduledTaskInfo -TaskName '{task_name}' -ErrorAction SilentlyContinue
if ($task -and $info) {{
  [pscustomobject]@{{
    state = [string]$task.State
    lastRunTime = [string]$info.LastRunTime
    lastTaskResult = [string]$info.LastTaskResult
    nextRunTime = [string]$info.NextRunTime
  }} | ConvertTo-Json -Compress
}}
""".format(task_name=STARTUP_TASK_NAME)
    output = _run_powershell(script)
    if not output.strip():
        payload = {"state": "not found", "lastRunTime": "n/a", "lastTaskResult": "n/a", "nextRunTime": "n/a"}
    else:
        payload = {key: str(value) for key, value in json.loads(output).items()}
    next_run_time = str(payload.get("nextRunTime", "")).strip()
    if not next_run_time or next_run_time in {"N/A", "n/a", "None", "0001-01-01T00:00:00"}:
        payload["nextRunTime"] = "startup/logon trigger only"
        if status_next_run_at:
            payload["internalLoopNextRun"] = _fmt_time(status_next_run_at)
        else:
            estimated_next_run = _estimate_worker_next_run(
                payload.get("lastRunTime"),
                worker_min_delay_minutes,
                worker_max_delay_minutes,
            )
            if estimated_next_run:
                payload["internalLoopNextRun"] = estimated_next_run
        payload["scheduleMode"] = "Windows only launches the worker; recurring cadence is inside the worker loop."
    else:
        payload["internalLoopNextRun"] = _fmt_time(status_next_run_at) if status_next_run_at else "n/a"
        payload["scheduleMode"] = "Windows trigger plus internal worker loop"
    return payload


def _command_snippets(root: Path) -> list[tuple[str, str]]:
    root_str = str(root)
    return [
        (
            "Project Root",
            f'Set-Location "{root_str}"',
        ),
        (
            "Read Worker Runtime",
            r'Get-Content .\data\runtime\worker_status.json',
        ),
        (
            "Tail Worker Log",
            r'Get-Content .\logs\worker.log -Tail 50 -Wait',
        ),
        (
            "Read Startup Errors",
            r'Get-Content .\logs\dashboard-startup.err.log -Tail 50' + "\n" + r'Get-Content .\logs\worker-startup.err.log -Tail 50',
        ),
        (
            "Start Services",
            r'.\scripts\start_services.ps1',
        ),
        (
            "Stop Worker",
            r'.\scripts\stop_services.ps1',
        ),
        (
            "Stop Worker And Dashboard",
            r'.\scripts\stop_all_services.ps1',
        ),
        (
            "Run Cycle Now",
            r'.\.venv\Scripts\python.exe .\scripts\run_cycle.py',
        ),
        (
            "Register Startup Task",
            r'.\scripts\register_windows_task.ps1',
        ),
        (
            "Check Startup Task",
            f'Get-ScheduledTask -TaskName "{STARTUP_TASK_NAME}"' + "\n" + f'Get-ScheduledTaskInfo -TaskName "{STARTUP_TASK_NAME}"',
        ),
    ]


def _handle_system_action(
    root: Path,
    python_exe: Path,
    worker_python_exe: Path,
    runtime_path: Path,
    action: str,
) -> str:
    if action == "start":
        if _worker_process_rows():
            return "Worker already running."
        worker_launcher = worker_python_exe if worker_python_exe.exists() else python_exe
        _popen_hidden([str(worker_launcher), str(root / "scripts" / "run_worker.py")], cwd=root)
        _write_worker_runtime_status(runtime_path, state="starting")
        return "Started worker."
    if action == "stop":
        stopped = _stop_worker_processes()
        _write_worker_runtime_status(runtime_path, state="stopped")
        if stopped:
            return "Stopped worker service. Dashboard is still running."
        return "No worker process was running. Dashboard is still running."
    if action == "restart":
        _stop_worker_processes()
        worker_launcher = worker_python_exe if worker_python_exe.exists() else python_exe
        _popen_hidden([str(worker_launcher), str(root / "scripts" / "run_worker.py")], cwd=root)
        _write_worker_runtime_status(runtime_path, state="starting")
        return "Restarting worker."
    if action == "run_cycle":
        _popen_hidden([str(python_exe), str(root / "scripts" / "run_cycle.py")], cwd=root)
        return "Triggered one run_cycle.py process."
    raise RuntimeError(f"Unknown system action: {action}")


def _run_powershell(script: str) -> str:
    result = _run_hidden(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _worker_process_rows() -> list[dict[str, str]]:
    script = r"""
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
Where-Object {
  ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and
  $_.CommandLine -and
  $_.CommandLine -like '*run_worker.py*'
} | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress
"""
    output = _run_powershell(script)
    if not output.strip():
        return []
    payload = json.loads(output)
    if isinstance(payload, dict):
        payload = [payload]
    return [
        {
            "pid": str(item.get("ProcessId", "")),
            "name": str(item.get("Name", "")),
            "command": str(item.get("CommandLine", "")),
        }
        for item in payload
    ]


def _stop_worker_processes() -> int:
    rows = _worker_process_rows()
    for row in rows:
        pid = row.get("pid")
        if not pid:
            continue
        _run_hidden(
            ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    return len(rows)


def _write_worker_runtime_status(runtime_path: Path, state: str) -> None:
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": None,
        "state": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "next_run_at": None,
        "last_error": None,
        "traceback": None,
    }
    runtime_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _pid_is_running(pid: Any) -> bool:
    try:
        numeric_pid = int(pid)
    except (TypeError, ValueError):
        return False
    result = _run_hidden(
        ["tasklist", "/FI", f"PID eq {numeric_pid}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return False
    output = result.stdout.strip()
    return bool(output) and not output.startswith("INFO:")


def _startupinfo() -> subprocess.STARTUPINFO | None:
    if sys.platform != "win32":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return startupinfo


def _run_hidden(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    kwargs.setdefault("startupinfo", _startupinfo())
    kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.run(args, **kwargs)


def _popen_hidden(args: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
    kwargs.setdefault("startupinfo", _startupinfo())
    kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.Popen(args, **kwargs)


def _fmt_time(value: str | None) -> str:
    if not value:
        return "n/a"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %I:%M:%S %p")
    except Exception:
        for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(value, fmt).astimezone()
                return dt.strftime("%Y-%m-%d %I:%M:%S %p")
            except Exception:
                continue
        return value


def _fmt_age(value: str | None) -> str:
    if not value:
        return "n/a"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        minutes = max(int(delta.total_seconds() // 60), 0)
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        rem = minutes % 60
        return f"{hours}h {rem}m"
    except Exception:
        return value


def _countdown_text(value: str | None) -> str:
    if not value:
        return "n/a"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        delta = max(int((dt.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()), 0)
        if delta == 0:
            return "due now"
        minutes, seconds = divmod(delta, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes}m {seconds}s"
        return f"{minutes}m {seconds}s"
    except Exception:
        return value


def _estimate_worker_next_run(
    last_run_time: str | None,
    worker_min_delay_minutes: int,
    worker_max_delay_minutes: int,
) -> str | None:
    if not last_run_time:
        return None
    parsed: datetime | None = None
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(last_run_time, fmt).astimezone()
            break
        except Exception:
            continue
    if parsed is None:
        return None
    earliest = parsed + timedelta(minutes=worker_min_delay_minutes)
    latest = parsed + timedelta(minutes=worker_max_delay_minutes)
    if worker_min_delay_minutes == worker_max_delay_minutes:
        return earliest.strftime("%Y-%m-%d %I:%M:%S %p")
    return f"{earliest.strftime('%Y-%m-%d %I:%M:%S %p')} to {latest.strftime('%Y-%m-%d %I:%M:%S %p')}"


def _metrics_text(raw_metrics: str) -> str:
    try:
        metrics = json.loads(raw_metrics)
    except Exception:
        return "n/a"
    return (
        f"V {metrics.get('view_count', 0)} | "
        f"L {metrics.get('like_count', 0)} | "
        f"R {metrics.get('reply_count', 0)} | "
        f"RP {metrics.get('repost_count', 0)}"
    )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

