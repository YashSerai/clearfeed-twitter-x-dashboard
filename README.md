# Clearfeed: AI X/Twitter Feed Curator & Drafter

Local high-signal X/Twitter dashboard for builders who want better discovery, faster drafting, and a cleaner way to monitor Twitter without mindlessly scrolling all day.

Clearfeed monitors your weighted X/Twitter Lists and, if you enable it, your home timeline. It ranks the strongest posts, helps you find relevant conversations to reply to, drafts replies in your voice, lets you edit or replace those drafts, and keeps the final approval with a human.

## Why This Exists
- Most feeds are noisy.
- Most drafting tools are generic.
- Most "growth bots" optimize for volume instead of judgment.

This repo takes the opposite approach: better source selection, stronger context, and a human-in-the-loop workflow.

## Feature Highlights
- Weighted discovery across multiple X Lists.
- Optional home timeline scraping as an extra signal source.
- Local dashboard for ranking, reviewing, and drafting.
- Voice-aware drafting using your own `WhoAmI.md`, `Voice.md`, and `Humanizer.md`.
- Local voice memory that learns from approved, rejected, and dashboard-edited drafts.
- Reviewed `Voice.md` upgrade proposals generated from your real decisions over time.
- AI-assisted profile setup with questionnaire templates and prompt packs.
- Editable drafts so you can replace or steer the AI instead of accepting whatever it generated.
- Local-first approvals by default.
- Optional Telegram mirroring.
- Optional direct posting through the official X API.

## How It Works
1. You choose the feeds that matter: list 1, list 2, list 3, and optional home timeline.
2. Each source gets its own weight.
3. The worker scrapes recent posts, scores them, and pushes the best candidates into the local dashboard.
4. You draft a reply, quote reply, or original post in your own voice.
5. You edit in the dashboard, approve, reject, or mark it as manually posted.
6. The app saves those decisions locally and uses them to propose better `Voice.md` updates over time.
7. If posting credentials are configured, the app can post through the X API. If not, the draft stays local and copy-ready.

## Who This Is For
- Builders who actively post on X.
- Founders who want a cleaner signal feed than the default timeline.
- Operators who want AI to help with drafting, not replace judgment.

## What It Does Not Do
- It does not scrape mentions in public v1.
- It does not post by scripting the X website.
- It does not parse X archives automatically.
- It does not run as a cloud SaaS.
- It does not try to be an unattended engagement bot.

## Requirements
- Windows with PowerShell.
- Git.
- Python 3 with `venv` support available as `py` or `python`.
- A Google Cloud project and application credentials for Vertex/Gemini drafting.
- An X account you can log into locally for Playwright list access and optional home timeline access.

The bootstrap script installs Python dependencies and Playwright Chromium for you. You do not need to preinstall Playwright separately.
You do not need to fill in `.env` before running `bootstrap.ps1` or `setup.ps1`.

## Quickstart
```powershell
git clone https://github.com/YashSerai/clearfeed-twitter-x-dashboard.git "Clearfeed Twitter X Dashboard"
cd "Clearfeed Twitter X Dashboard"
.\scripts\bootstrap.ps1
.\scripts\setup.ps1
```

`setup.ps1` creates:
- `.env`
- the local data folders
- the local SQLite database

You can run `bootstrap.ps1` and `setup.ps1` before filling any credentials. Credentials are only needed when you actually start drafting, scraping, or posting.

Then:

1. Fill in `.env`.
2. Build `profiles/default/WhoAmI.md`.
3. Build `profiles/default/Voice.md`.
4. Add your feed URLs and weights in `.env` or `data/sources/x_sources.yaml`.
5. Optionally set `HOME_TIMELINE_ENABLED=true`.
6. Save a logged-in X session using the default real-browser capture flow:

```powershell
.\scripts\capture-x-session.ps1
```

This launches a normal Chrome or Edge window with remote debugging enabled, lets you log into X there, and then captures storage state from that real browser session.

Regular scraping runs headless by default. If you need visible browser debugging later, set `PLAYWRIGHT_HEADLESS=false` in `.env`.

If you explicitly want to use the managed Playwright browser instead, use:

```powershell
.\scripts\capture-x-session.ps1 -UseManagedBrowser
```

7. Start the dashboard:

```powershell
.\scripts\run-dashboard.ps1
```

8. In a second terminal, start the worker:

```powershell
.\scripts\run-worker.ps1
```

## Run Commands
Start the dashboard:

```powershell
.\scripts\run-dashboard.ps1
```

Start the worker:

```powershell
.\scripts\run-worker.ps1
```

Start dashboard and worker together in separate PowerShell windows:

```powershell
.\scripts\start_services.ps1
```

If you started either process in the current terminal, press `Ctrl+C` in that terminal to stop it.

Stop any background worker process for this repo:

```powershell
.\scripts\stop_services.ps1
```

Stop both dashboard and worker background processes for this repo:

```powershell
.\scripts\stop_all_services.ps1
```

Restart the worker:

```powershell
.\scripts\stop_services.ps1
.\scripts\run-worker.ps1
```

## Source Configuration
The starter config supports three weighted list sources plus an optional home timeline source.

Env-based setup:
- `LIST_1_URL`, `LIST_1_WEIGHT`
- `LIST_2_URL`, `LIST_2_WEIGHT`
- `LIST_3_URL`, `LIST_3_WEIGHT`
- `HOME_TIMELINE_ENABLED`, `HOME_TIMELINE_WEIGHT`
- `WORKER_MIN_DELAY_MINUTES`, `WORKER_MAX_DELAY_MINUTES`

You can also edit [`data/sources/x_sources.yaml`](data/sources/x_sources.yaml) directly if you want more feeds, different labels, or different source behavior.

The default worker cadence is randomized between `25` and `35` minutes. If you want a tighter or slower loop, change those two env vars.

## Voice Setup
This repo uses three local files as the voice packet:
- `profiles/default/WhoAmI.md`
- `profiles/default/Voice.md`
- `profiles/default/Humanizer.md`

`WhoAmI.md` and `Voice.md` now include a fixed `## Active Guardrails` block at the bottom. Fill the editable sections above that block, but leave the guardrails unchanged unless you are intentionally changing how the system composes identity, voice, and humanizer context.

There are two setup paths:

### Option 1: AI-Assisted Fill
Use these files:
- `profiles/templates/WhoAmI.Questionnaire.md`
- `profiles/templates/Voice.Questionnaire.md`
- `profiles/templates/AI-Assisted-Profile-Fill.md`

Give the AI-assisted prompt to ChatGPT, Gemini, or another agent and have it fill the questionnaires based on what it already knows about you plus anything you correct.

### Option 2: Manual Answers -> AI Build
Use these files:
- `profiles/templates/WhoAmI.Questionnaire.md`
- `profiles/templates/Voice.Questionnaire.md`
- `profiles/templates/Build-Final-Profile-Prompt.md`

Answer the questionnaires yourself, then paste those answers into the build prompt and ask an AI agent to generate the final contents for:
- `profiles/default/WhoAmI.md`
- `profiles/default/Voice.md`

This gives users a much better starting point than writing voice docs from scratch.
The build prompt explicitly preserves the fixed guardrails block in both final files.

If you have an X archive, use it only as reference material while answering the voice questionnaire. Public v1 does not ingest archives directly.

## Posting And Approval Modes
- No X API credentials: drafts can still be reviewed and approved locally, but posting stays manual.
- X API credentials configured: the app can post through the official X API.
- Telegram credentials configured: Telegram can mirror approvals, but the dashboard remains the default workflow.

## Voice Evolution
This repo includes a local feedback loop for improving `profiles/default/Voice.md` over time.

What it uses:
- approved drafts
- rejected drafts
- drafts you manually edited in the dashboard before approval
- manually posted drafts saved through the dashboard workflow

How it works:
- the app stores those signals locally in SQLite
- once per day, or whenever you trigger it manually, it can run a `Voice Review`
- the review compares your accepted vs rejected patterns and proposes a new `Voice.md`
- the dashboard shows a diff and lets you approve or reject the update

Important rules:
- `Humanizer.md` is treated as fixed and is never auto-rewritten
- `Voice.md` is never auto-updated silently
- edits made inside the dashboard are the highest-signal feedback, so prefer editing there before you approve a post

## Troubleshooting
- `Missing required profile file(s)`: run `.\scripts\setup.ps1` and fill the files in `profiles/default/`.
- `Missing Playwright session state`: run `.\scripts\capture-x-session.ps1` after logging into X.
- Want to avoid X login issues in Playwright: use the default `.\scripts\capture-x-session.ps1` flow, which captures from a real Chrome or Edge session over CDP.
- Want to try the old managed-browser path anyway: run `.\scripts\capture-x-session.ps1 -UseManagedBrowser`.
- Dashboard opens but nothing appears: make sure at least one list URL is set, or enable home timeline discovery.
- Approve button does not post: this is expected if X API credentials are not configured.
- Telegram actions do nothing: Telegram is optional and remains disabled until `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are configured.
- Vertex auth failures: verify `GOOGLE_CLOUD_PROJECT` and `GOOGLE_APPLICATION_CREDENTIALS`, then confirm the account has access to the configured models.
- Performance is worse from a OneDrive-backed path. Prefer cloning to a local folder like `C:\dev\clearfeed-twitter-x-dashboard` instead of `C:\Users\...\OneDrive\...`.

## Repo Layout
- `x_signal_dashboard/` application code
- `scripts/` bootstrap and runtime commands
- `profiles/default/` voice templates
- `profiles/templates/` questionnaire and AI prompt templates
- `data/sources/x_sources.yaml` feed config
- `docs/launch-checklist.md` release checklist

## Contributing
If you want to improve the source ranking, dashboard UX, or onboarding flow, open an issue first with:
- the problem you hit
- the behavior you expected
- the smallest change that would improve it

## Limitations And Compliance
- This project uses Playwright for local discovery. You are responsible for complying with X rules, your account setup, and any applicable platform restrictions.
- Home timeline scraping is optional and disabled by default.
- Posting uses the official X API only.
- This project is designed for human-assisted workflows, not unattended automation.

## Before You Publish
- Add sanitized dashboard screenshots or a short GIF.
- Test the quickstart on a clean Windows machine or VM.
- Test local-only approval.
- Test direct posting with X API credentials.
