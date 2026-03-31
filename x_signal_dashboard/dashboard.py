from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

from .config import load_config
from .service import XAgentService

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
            if parsed.path not in {"/", "/index.html"}:
                self.send_response(404)
                self.end_headers()
                return

            flash = parse_qs(parsed.query).get("flash", [""])[0]
            error = parse_qs(parsed.query).get("error", [""])[0]
            status = _read_status(runtime_path)
            page = _render_dashboard(
                root,
                database_path,
                status,
                draft_text_limit,
                setup_status=service.config.setup_status(),
                drafting_enabled=service.config.drafting_enabled,
                worker_ready=service.config.session_ready and service.config.sources_ready,
                posting_enabled=service.config.posting_enabled,
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


def _read_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    status = json.loads(path.read_text(encoding="utf-8"))
    pid = status.get("pid")
    if pid and not _pid_is_running(pid):
        status["pid"] = None
        status["next_run_at"] = None
        status["state"] = "stopped"
    return status


def _query_rows(database_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _redirect_params(result: dict[str, Any]) -> dict[str, str]:
    params: dict[str, str] = {}
    if result.get("focus_draft_id") is not None:
        params["focus_draft"] = str(result["focus_draft_id"])
    return params


def _render_dashboard(
    root: Path,
    database_path: Path,
    status: dict[str, Any],
    draft_text_limit: int,
    setup_status: dict[str, dict[str, str | bool]],
    drafting_enabled: bool,
    worker_ready: bool,
    posting_enabled: bool,
    telegram_enabled: bool,
    worker_min_delay_minutes: int,
    worker_max_delay_minutes: int,
    flash: str = "",
    error: str = "",
) -> str:
    summary_rows = _query_rows(database_path, "select status, count(*) as c from candidates group by status order by status")
    draft_rows = _query_rows(database_path, "select status, count(*) as c from drafts group by status order by status")
    latest_runs = _query_rows(
        database_path,
        "select id, status, started_at, finished_at, notes from run_logs order by id desc limit 5",
    )
    queue_candidates = _query_rows(
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

    worker_state = status.get("state", "unknown")
    updated_at = status.get("updated_at")
    next_run_at = status.get("next_run_at")
    next_run_countdown = _countdown_text(next_run_at)
    last_error = status.get("last_error")
    process_rows = _process_rows()
    task_info = _scheduled_task_info(
        status_next_run_at=status.get("next_run_at"),
        worker_min_delay_minutes=worker_min_delay_minutes,
        worker_max_delay_minutes=worker_max_delay_minutes,
    )
    worker_log = _tail_file(root / "logs" / "worker.log")
    launcher_log = _tail_file(root / "logs" / "launcher.log")
    commands = _command_snippets(root)

    def badge(text: str, tone: str) -> str:
        return f'<span class="badge badge-{tone}">{text}</span>'

    if worker_state == "sleeping":
        state_badge = badge("Worker Sleeping", "ok")
    elif worker_state == "running":
        state_badge = badge("Worker Running", "ok")
    elif worker_state == "stopped":
        state_badge = badge("Worker Stopped", "warn")
    elif worker_state == "error":
        state_badge = badge("Worker Error", "bad")
    else:
        state_badge = badge("Worker Unknown", "warn")

    summary_html = "".join(
        f"<li><strong>{row['status']}</strong><span>{row['c']}</span></li>" for row in summary_rows
    ) or "<li><strong>none</strong><span>0</span></li>"
    draft_html = "".join(
        f"<li><strong>{row['status']}</strong><span>{row['c']}</span></li>" for row in draft_rows
    ) or "<li><strong>none</strong><span>0</span></li>"
    runs_html = "".join(
        f"<tr><td>{row['id']}</td><td>{row['status']}</td><td>{_fmt_time(row['started_at'])}</td><td>{_fmt_time(row['finished_at'])}</td><td>{_escape(row['notes'] or '')}</td></tr>"
        for row in latest_runs
    )
    queue_cards_html = "".join(
        _candidate_card(row, draft_text_limit, posting_enabled, drafting_enabled) for row in queue_candidates
    )
    queue_nav_html = "".join(_queue_jump_button(row) for row in queue_candidates)
    original_drafts_html = "".join(
        _original_draft_card(row, draft_text_limit, posting_enabled, drafting_enabled) for row in latest_original_drafts
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
    task_html = "".join(
        f"<li><strong>{_escape(key)}</strong><span>{_escape(value)}</span></li>" for key, value in task_info.items()
    )
    setup_html = "".join(
        _setup_status_row(str(item["label"]), bool(item["ok"]), str(item["detail"])) for item in setup_status.values()
    )

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>X Signal Dashboard</title>
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
    .hero p {{ margin: 0; color: var(--muted); max-width: 720px; line-height: 1.5; }}
    .badge {{ display:inline-block; padding: 8px 12px; border-radius: 999px; font-weight: 700; font-size: 13px; }}
    .countdown {{ margin-top: 10px; color: var(--muted); font-size: 13px; text-align: right; }}
    .hero-side {{
      display: grid;
      gap: 10px;
      justify-items: end;
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
    table {{ width:100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ text-align:left; padding: 10px 8px; border-bottom:1px solid rgba(255,255,255,.06); vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; }}
    a {{ color: var(--accent); text-decoration: none; }}
    .error {{ margin-top: 12px; color: var(--bad); white-space: pre-wrap; }}
    .controls {{ display:flex; gap:8px; flex-wrap:wrap; }}
    .controls form {{ margin:0; }}
    .section-note {{ margin: -6px 0 14px; color: var(--muted); font-size: 13px; }}
    button {{
      border: 1px solid var(--border);
      background: #0f1823;
      color: var(--text);
      padding: 9px 13px;
      border-radius: 12px;
      cursor: pointer;
      font-weight: 600;
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
    .command-label {{ color: var(--muted); font-size: 13px; margin-bottom: 6px; }}
    .draft-cell {{ min-width: 340px; }}
    .draft-form {{ display: grid; gap: 8px; }}
    .draft-editor {{
      min-height: 68px;
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
    .queue-stage {{
      min-height: 560px;
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
    }}
    .queue-card.is-active {{ display: grid; }}
    .queue-card-main,
    .queue-card-side {{
      display: grid;
      gap: 14px;
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
      text-align: left;
      border: 1px solid rgba(255,255,255,.08);
      border-radius: 18px;
      padding: 12px 14px;
      background: rgba(8,13,20,.75);
    }}
    .queue-jump strong {{
      display: block;
      color: var(--text);
      margin-bottom: 4px;
    }}
    .queue-jump small {{
      display: block;
      color: var(--muted);
      line-height: 1.45;
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
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    @media (max-width: 1100px) {{
      .span-4, .span-5, .span-6, .span-7, .span-8 {{ grid-column: span 12; }}
      .setup-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
      .queue-layout,
      .queue-card,
      .original-drafts-grid {{ grid-template-columns: 1fr; }}
      .queue-toolbar {{ justify-content: flex-start; }}
      .queue-counter {{ text-align: left; min-width: 0; }}
    }}
    @media (max-width: 720px) {{
      .wrap {{ padding: 16px; }}
      .hero {{ padding: 20px; border-radius: 20px; flex-direction: column; align-items: flex-start; }}
      .hero h1 {{ font-size: 28px; }}
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
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div>
        <h1>X Signal Dashboard</h1>
        <p>Local control surface for high-signal list discovery, optional home timeline ranking, draft generation, and human-in-the-loop approvals. {'Telegram is enabled as an optional mirror.' if telegram_enabled else 'Telegram is optional and currently disabled.'}</p>
      </div>
      <div class="hero-side">
        {state_badge}
        <div class="countdown" id="next-run-countdown" data-next-run="{_escape(next_run_at or '')}">
          Next run: {_escape(next_run_countdown)}
        </div>
        <div class="controls">
          <button type="button" class="ghost-button" data-soft-refresh>Refresh Dashboard</button>
        </div>
        <div class="live-note" data-live-note>Auto-refresh is paused while you work.</div>
      </div>
    </div>
    {flash_html}
    {error_html}
    <div class="grid">
      <section class="card span-4">
        <h2>Worker</h2>
        <div class="meta">
          <div><strong>Updated:</strong> {_fmt_time(updated_at)}</div>
          <div><strong>Next run:</strong> {_fmt_time(next_run_at)}</div>
          <div><strong>PID:</strong> {_escape(str(status.get('pid', 'unknown')))}</div>
        </div>
        {f'<div class="error"><strong>Last error:</strong> {_escape(last_error)}</div>' if last_error else ''}
      </section>
      <section class="card span-4">
        <h2>Candidates</h2>
        <ul class="stats">{summary_html}</ul>
      </section>
      <section class="card span-4">
        <h2>Drafts</h2>
        <ul class="stats">{draft_html}</ul>
      </section>
      <section class="card span-12">
        <h2>Setup Status</h2>
        <div class="setup-grid">{setup_html}</div>
      </section>
      <section class="card span-8">
        <h2>System Controls</h2>
        <div class="controls">
          {_post_button('/system', 'action', 'start', None, None, 'Start Worker', 'ok', 'Starting worker...', disabled=not worker_ready, disabled_reason='Configure at least one source and capture an X session before starting the worker.')}
          {_post_button('/system', 'action', 'stop', None, None, 'Stop Worker', 'bad', 'Stopping worker...')}
          {_post_button('/system', 'action', 'restart', None, None, 'Restart Worker', '', 'Restarting worker...', disabled=not worker_ready, disabled_reason='Configure at least one source and capture an X session before restarting the worker.')}
          {_post_button('/system', 'action', 'run_cycle', None, None, 'Run Cycle Now', '', 'Running a cycle now...', disabled=not worker_ready, disabled_reason='Configure at least one source and capture an X session before running a cycle.')}
        </div>
        <div class="meta" style="margin-top:10px;">The dashboard stays online. These controls only stop or restart the worker loop.</div>
        {'' if worker_ready else '<div class="inline-warning">Worker actions are disabled until an X session is captured and at least one source is configured.</div>'}
      </section>
      <section class="card span-4">
        <h2>Scheduled Task</h2>
        <ul class="stats">{task_html}</ul>
      </section>
      <section class="card span-8">
        <h2>Create Original Drafts</h2>
        <form method="post" action="/original">
          <input type="text" name="topic" placeholder="Optional topic, for example: new OpenAI parameter changes">
          <button class="ok" type="submit" data-busy-label="Generating original drafts..."{' disabled title="Set GOOGLE_CLOUD_PROJECT and GOOGLE_APPLICATION_CREDENTIALS to enable original drafting."' if not drafting_enabled else ''}>Generate Original Drafts</button>
        </form>
        {'' if drafting_enabled else '<div class="inline-warning">Original drafting is disabled until Google drafting credentials are configured in <code>.env</code>.</div>'}
      </section>
      <section class="card span-4">
        <h2>Maintenance</h2>
        <form method="post" action="/reset" onsubmit="return confirm('Reset local state and clear tracked drafts, candidates, and optional Telegram message references?');">
          <button class="bad" type="submit" data-busy-label="Resetting local state...">Reset Local State</button>
        </form>
      </section>
      <section class="card span-12 queue-shell" id="reply-queue">
        <div class="queue-header">
          <div>
            <h2>Reply Queue</h2>
            <div class="section-note">Review one tweet at a time. Drafts stay attached to the tweet card, so pressing <strong>Draft Reply</strong> drops the editable reply directly underneath. {'Posting is live through the X API.' if posting_enabled else 'Posting creds are not configured, so approvals stay local and copy-ready.'} {'' if drafting_enabled else 'Draft generation is disabled until Google drafting credentials are configured.'}</div>
          </div>
          <div class="queue-toolbar">
            <span class="queue-counter" data-queue-counter>{len(queue_candidates)} in queue</span>
            <button type="button" data-queue-prev>Prev</button>
            <button type="button" data-queue-next>Next</button>
            <button type="button" class="ghost-button" data-restart-deck>Back to first</button>
          </div>
        </div>
        <div class="queue-layout" data-queue-root>
          <div class="queue-stage">
            {queue_cards_html or '<div class="queue-empty" data-queue-empty><div><h3>No live candidates</h3><p class="empty-note">Run a new cycle or wait for the worker to surface fresh tweets.</p></div></div>'}
          </div>
          <aside class="queue-rail">
            <div class="queue-rail-label">Queue rail</div>
            {queue_nav_html or '<p class="empty-note">No cards waiting.</p>'}
          </aside>
        </div>
      </section>
      <section class="card span-12" id="latest-drafts">
        <h2>Original Drafts</h2>
        <div class="section-note">Standalone drafts created from the topic box stay here. Reply drafts now live inside the reply queue cards above.</div>
        <div class="original-drafts-grid">
          {original_drafts_html or '<p class="empty-note">No standalone drafts yet.</p>'}
        </div>
      </section>
      <section class="card span-12">
        <h2>Recent Runs</h2>
        <table>
          <thead>
            <tr><th>ID</th><th>Status</th><th>Started</th><th>Finished</th><th>Notes</th></tr>
          </thead>
          <tbody>{runs_html}</tbody>
        </table>
      </section>
      <section class="card span-12">
        <h2>Live Processes</h2>
        <table>
          <thead>
            <tr><th>PID</th><th>Name</th><th>Command</th></tr>
          </thead>
          <tbody>{process_html}</tbody>
        </table>
      </section>
      <section class="card span-6">
        <h2>Worker Log Tail</h2>
        <pre>{_escape(worker_log)}</pre>
      </section>
      <section class="card span-6">
        <h2>Launcher Log Tail</h2>
        <pre>{_escape(launcher_log)}</pre>
      </section>
      <section class="card span-12">
        <h2>Helpful Commands</h2>
        {commands_html}
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
  const focusDraft = url.searchParams.get('focus_draft');
  if (!window.location.hash) {{
    const savedScroll = window.sessionStorage.getItem(scrollKey);
    if (savedScroll) {{
      window.requestAnimationFrame(() => {{
        window.scrollTo({{ top: Number(savedScroll), behavior: 'auto' }});
      }});
    }}
  }}
  if (url.searchParams.has('flash') || url.searchParams.has('error') || focusDraft) {{
    url.searchParams.delete('flash');
    url.searchParams.delete('error');
    url.searchParams.delete('focus_draft');
    window.history.replaceState({{}}, '', url.pathname + (url.search ? `?${{url.searchParams.toString()}}` : ''));
  }}
  window.addEventListener('pagehide', saveScroll);
  window.addEventListener('beforeunload', saveScroll);
  const busyOverlay = document.querySelector('[data-busy-overlay]');
  const busyTitle = document.querySelector('[data-busy-title]');
  const showBusy = (message) => {{
    if (!busyOverlay || !busyTitle) {{
      return;
    }}
    busyTitle.textContent = message || 'Working...';
    busyOverlay.classList.add('is-visible');
    busyOverlay.setAttribute('aria-hidden', 'false');
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
      counter.textContent = `${{editor.value.length}} / ${{editor.dataset.limit || ''}}`;
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
      const editor = form.querySelector('[data-draft-editor]');
      if (editor) {{
        editor.dataset.dirty = 'false';
      }}
      saveScroll();
      const submitter = event.submitter;
      const busyLabel = (submitter && submitter.dataset.busyLabel) || form.dataset.busyLabel || 'Working...';
      showBusy(busyLabel);
    }});
  }});
  const queueRoot = document.querySelector('[data-queue-root]');
  if (queueRoot) {{
    const queueCards = Array.from(document.querySelectorAll('[data-queue-card]'));
    const queueJumps = Array.from(document.querySelectorAll('[data-queue-jump]'));
    const queueRail = queueRoot.querySelector('.queue-rail');
    const queueEmpty = document.querySelector('[data-queue-empty]');
    const queueCounter = document.querySelector('[data-queue-counter]');
    const queueStateKey = `dashboard-queue:${{window.location.pathname}}`;
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
      button.addEventListener('click', () => {{
        activateCard(button.dataset.jumpTo);
        saveScroll();
        const card = document.getElementById(`candidate-${{button.dataset.jumpTo}}`);
        if (card) {{
          card.scrollIntoView({{ block: 'start', behavior: 'smooth' }});
        }}
      }});
    }});
    document.querySelectorAll('[data-skip-card]').forEach((button) => {{
      button.addEventListener('click', () => {{
        const card = button.closest('[data-queue-card]');
        if (!card) {{
          return;
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
      }});
    }});
    document.querySelectorAll('[data-queue-prev]').forEach((button) => {{
      button.addEventListener('click', () => moveBy(-1));
    }});
    document.querySelectorAll('[data-queue-next]').forEach((button) => {{
      button.addEventListener('click', () => moveBy(1));
    }});
    const restartDeck = document.querySelector('[data-restart-deck]');
    if (restartDeck) {{
      restartDeck.addEventListener('click', () => {{
        queueOrder.sort((left, right) => left - right);
        activeIndex = 0;
        syncQueue();
      }});
    }}
    let focusTarget = null;
    if (focusDraft) {{
      focusTarget = document.getElementById(`draft-${{focusDraft}}`);
    }}
    if (!focusTarget && window.location.hash) {{
      focusTarget = document.getElementById(window.location.hash.slice(1));
    }}
    if (focusTarget) {{
      const containingCard = focusTarget.closest('[data-queue-card]');
      if (containingCard) {{
        activateCard(containingCard.dataset.cardId);
        containingCard.classList.add('queue-card-focus');
        window.setTimeout(() => containingCard.classList.remove('queue-card-focus'), 5000);
      }} else {{
        syncQueue();
      }}
      focusTarget.classList.add('draft-focus');
      window.setTimeout(() => focusTarget.classList.remove('draft-focus'), 5000);
      window.requestAnimationFrame(() => {{
        focusTarget.scrollIntoView({{ block: 'start', behavior: 'smooth' }});
      }});
    }} else {{
      syncQueue();
    }}
  }} else if (focusDraft) {{
    const target = document.getElementById(`draft-${{focusDraft}}`);
    if (target) {{
      target.classList.add('draft-row-focus');
      window.setTimeout(() => target.classList.remove('draft-row-focus'), 5000);
    }}
  }}
  const el = document.getElementById('next-run-countdown');
  if (el) {{
    const raw = el.dataset.nextRun;
    if (raw) {{
      const target = new Date(raw).getTime();
      if (!Number.isNaN(target)) {{
        const render = () => {{
          const delta = Math.max(0, target - Date.now());
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
        render();
        window.setInterval(render, 1000);
      }}
    }}
  }}
  const liveNote = document.querySelector('[data-live-note]');
  const refreshButton = document.querySelector('[data-soft-refresh]');
  const pageLoadedAt = Date.now();
  const renderLiveNote = () => {{
    if (!liveNote) {{
      return;
    }}
    const elapsedSeconds = Math.floor((Date.now() - pageLoadedAt) / 1000);
    if (elapsedSeconds < 60) {{
      liveNote.textContent = `Auto-refresh is paused while you work. Snapshot age: ${{elapsedSeconds}}s.`;
      return;
    }}
    const elapsedMinutes = Math.floor(elapsedSeconds / 60);
    liveNote.textContent = `Auto-refresh is paused while you work. Snapshot age: ${{elapsedMinutes}}m. Use Refresh Dashboard when you want the latest queue state.`;
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
    badge = f"{_escape(row['status'])} | {_fmt_age(row['posted_at'])}"
    if row["draft_id"]:
        badge = f"{badge} | draft #{row['draft_id']}"
    return (
        f'<button type="button" class="queue-jump" data-queue-jump data-jump-to="{row["id"]}">'
        f"<strong>@{_escape(row['author_handle'])}</strong>"
        f"<small>{_escape(badge)}</small>"
        f"<small>{_escape(preview or 'No text captured for this tweet.')}</small>"
        "</button>"
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


def _candidate_card(row: sqlite3.Row, draft_text_limit: int, posting_enabled: bool, drafting_enabled: bool) -> str:
    metrics = _metrics_text(row["raw_metrics"])
    draft_badge = ""
    if row["draft_id"]:
        draft_badge = f'<span class="pill {_pill_class(row["draft_status"])}">draft #{row["draft_id"]} · {_escape(row["draft_status"])}</span>'
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
        f"{_candidate_draft_panel(row, draft_text_limit, posting_enabled, drafting_enabled)}"
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
        ' disabled title="Set GOOGLE_CLOUD_PROJECT and GOOGLE_APPLICATION_CREDENTIALS to enable drafting."'
        if not drafting_enabled
        else ""
    )
    drafting_note = (
        ""
        if drafting_enabled
        else '<div class="inline-warning">Drafting is disabled until Google drafting credentials are configured.</div>'
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
    posting_enabled: bool,
    drafting_enabled: bool,
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
        _escape(draft_status or "unknown"),
        _fmt_time(row["draft_updated_at"]),
    ]
    draft_count = int(row["draft_count"] or 0)
    if draft_count > 1:
        note_bits.append(f"{draft_count} attempts")
    if row["draft_posted_tweet_id"]:
        note_bits.append(f"posted as {row['draft_posted_tweet_id']}")
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
        posting_enabled=posting_enabled,
        drafting_enabled=drafting_enabled,
    )
    return (
        f'<section class="draft-inline" id="draft-{draft_id}">'
        '<div class="draft-inline-header">'
        f'<div><h3>Latest Draft #{draft_id}</h3><div class="draft-inline-note">{" · ".join(note_bits)}</div></div>'
        f'<span class="pill {_pill_class(draft_status)}">{_escape(draft_status)}</span>'
        '</div>'
        f"{guidance_html}"
        f"{_draft_text_editor(draft_id, row['draft_text'] or '', draft_status, draft_text_limit)}"
        f"{controls}"
        '</section>'
    )


def _original_draft_card(
    row: sqlite3.Row,
    draft_text_limit: int,
    posting_enabled: bool,
    drafting_enabled: bool,
) -> str:
    controls = _draft_action_buttons(
        draft_id=int(row["id"]),
        status=str(row["status"] or ""),
        image_prompt=row["image_prompt"],
        image_path=row["image_path"],
        posting_enabled=posting_enabled,
        drafting_enabled=drafting_enabled,
    )
    note_bits = [
        _escape(row["draft_type"] or "original"),
        _escape(row["status"] or "unknown"),
        _fmt_time(row["updated_at"]),
    ]
    if row["posted_tweet_id"]:
        note_bits.append(f"posted as {row['posted_tweet_id']}")
    if row["image_path"]:
        note_bits.append("image attached")
    return (
        f'<article class="original-card" id="draft-{row["id"]}">'
        '<div class="draft-inline-header">'
        f'<div><h3>Original Draft #{row["id"]}</h3><div class="draft-inline-note">{" · ".join(note_bits)}</div></div>'
        f'<span class="pill {_pill_class(row["status"])}">{_escape(row["status"])}</span>'
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
    posting_enabled: bool,
    drafting_enabled: bool,
) -> str:
    if status != "drafted":
        return ""
    controls = ['<div class="draft-inline-actions">']
    controls.append(
        _post_button(
            "/draft",
            "draft_id",
            draft_id,
            "action",
            "approve",
            "Post Now" if posting_enabled else "Approve Draft",
            "ok",
            "Posting to X..." if posting_enabled else "Approving draft locally...",
        )
    )
    controls.append(
        _post_button("/draft", "draft_id", draft_id, "action", "manual", "Mark Posted", "", "Marking posted...")
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
                disabled=not drafting_enabled,
                disabled_reason="Set GOOGLE_CLOUD_PROJECT and GOOGLE_APPLICATION_CREDENTIALS to enable image generation.",
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


def _draft_text_editor(draft_id: int, draft_text: str, status: str, draft_text_limit: int) -> str:
    editor_id = f"draft-text-{draft_id}"
    if status != "drafted":
        return (
            '<div class="draft-form">'
            f'<textarea id="{editor_id}" class="draft-editor" data-draft-editor data-limit="{draft_text_limit}" readonly>{_escape(draft_text)}</textarea>'
            '<div class="draft-meta">'
            '<span>Read only</span>'
            f'<span data-char-count-for="{editor_id}">{len(draft_text)} / {draft_text_limit}</span>'
            "</div>"
            "</div>"
        )
    return (
        f'<form method="post" action="/draft" class="draft-form" data-draft-form>'
        f'<input type="hidden" name="draft_id" value="{draft_id}">'
        '<input type="hidden" name="action" value="save_text">'
        f'<textarea id="{editor_id}" name="draft_text" class="draft-editor" data-draft-editor data-limit="{draft_text_limit}" maxlength="{draft_text_limit}">{_escape(draft_text)}</textarea>'
        '<div class="draft-meta">'
        f'<span data-char-count-for="{editor_id}">{len(draft_text)} / {draft_text_limit}</span>'
        '<button type="submit" data-busy-label="Saving draft text...">Save Text</button>'
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
    script = r"""
$task = Get-ScheduledTask -TaskName 'YashXAgentWorker' -ErrorAction SilentlyContinue
$info = Get-ScheduledTaskInfo -TaskName 'YashXAgentWorker' -ErrorAction SilentlyContinue
if ($task -and $info) {
  [pscustomobject]@{
    state = [string]$task.State
    lastRunTime = [string]$info.LastRunTime
    lastTaskResult = [string]$info.LastTaskResult
    nextRunTime = [string]$info.NextRunTime
  } | ConvertTo-Json -Compress
}
"""
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
            "Read Worker Status",
            r'Get-Content .\data\runtime\worker_status.json',
        ),
        (
            "Tail Worker Log",
            r'Get-Content .\logs\worker.log -Tail 50 -Wait',
        ),
        (
            "Start Worker",
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
            "Reset Local State",
            r'.\.venv\Scripts\python.exe .\scripts\reset_state.py',
        ),
        (
            "Check Scheduled Task",
            'Get-ScheduledTask -TaskName "YashXAgentWorker"\nGet-ScheduledTaskInfo -TaskName "YashXAgentWorker"',
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
