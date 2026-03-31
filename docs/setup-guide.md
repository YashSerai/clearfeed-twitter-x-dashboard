# Clearfeed Setup Guide

This guide covers the full local setup, voice configuration, runtime commands, and common troubleshooting steps for Clearfeed.

## AI Provider Options
Clearfeed supports two AI backends:
- `vertex`: Google Vertex / Gemini through your Google Cloud project
- `openai_compatible`: any OpenAI-style endpoint, including OpenAI itself and local servers like Ollama, LM Studio, and vLLM

The app uses one provider for the whole workflow at a time:
- candidate ranking
- reply drafting
- original post drafting
- voice review
- archive-based voice bootstrapping

Local models are supported, but stronger hosted models usually do better on archive-to-voice synthesis and `Voice.md` proposal quality.

## Requirements
- Windows with PowerShell.
- Git.
- Python 3 with `venv` support available as `py` or `python`.
- Either:
  - a Google Cloud project and application credentials for Vertex/Gemini
  - or an OpenAI-compatible base URL and model names
- An X/Twitter account you can log into locally for Playwright list access and optional home timeline access.

The bootstrap script installs Python dependencies and Playwright Chromium for you. You do not need to preinstall Playwright separately.
You do not need to fill in `.env` before running `bootstrap.ps1` or `setup.ps1`.

## Full Quickstart
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

It also asks you which AI provider you want to use:
- `1` = Vertex
- `2` = OpenAI-compatible

If `.env` already exists, `setup.ps1` updates provider-related keys in place instead of replacing the whole file.
Even so, back up one-time secrets before rerunning setup. Some providers only show API keys once.

Then:
1. Fill in `.env`.
2. Build `profiles/default/WhoAmI.md`.
3. Build `profiles/default/Voice.md`.
4. Add your feed URLs and weights in `.env` or `data/sources/x_sources.yaml`.
5. Optionally set `HOME_TIMELINE_ENABLED=true`.
6. Save a logged-in X session:

```powershell
.\scripts\capture-x-session.ps1
```

7. Start the dashboard:

```powershell
.\scripts\run-dashboard.ps1
```

8. In a second terminal, start the worker:

```powershell
.\scripts\run-worker.ps1
```

## Common Commands
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

Stop any background worker process for this repo:

```powershell
.\scripts\stop_services.ps1
```

Stop both dashboard and worker background processes for this repo:

```powershell
.\scripts\stop_all_services.ps1
```

Import an unzipped X archive:

```powershell
.\scripts\import-x-archive.ps1 -ArchiveDir "C:\path\to\unzipped\twitter-archive"
```

Import and immediately build an archive-derived `Voice.md` proposal:

```powershell
.\scripts\import-x-archive.ps1 -ArchiveDir "C:\path\to\unzipped\twitter-archive" -RunVoiceBuild
```

Register Windows startup/logon launch for this repo:

```powershell
.\scripts\register_windows_task.ps1
```

Remove the Windows startup/logon task:

```powershell
.\scripts\unregister_windows_task.ps1
```

## Source Configuration
The starter config supports three weighted list sources plus an optional home timeline source.

Env-based setup:
- `LIST_1_URL`, `LIST_1_WEIGHT`
- `LIST_2_URL`, `LIST_2_WEIGHT`
- `LIST_3_URL`, `LIST_3_WEIGHT`
- `HOME_TIMELINE_ENABLED`, `HOME_TIMELINE_WEIGHT`
- `WORKER_MIN_DELAY_MINUTES`, `WORKER_MAX_DELAY_MINUTES`

You can also edit `data/sources/x_sources.yaml` directly if you want more feeds, different labels, or different source behavior.

The default worker cadence is randomized between `25` and `35` minutes. If you want a tighter or slower loop, change those two env vars.

## Provider Setup
### Vertex
Fill:
- `AI_PROVIDER=vertex`
- `AI_TEXT_MODEL`
- `AI_POLISH_MODEL`
- optional `AI_VISION_MODEL`
- optional `AI_IMAGE_MODEL`
- `GOOGLE_CLOUD_PROJECT`
- `GOOGLE_APPLICATION_CREDENTIALS`
- optional `GOOGLE_CLOUD_LOCATION`

### OpenAI-Compatible
Fill:
- `AI_PROVIDER=openai_compatible`
- `AI_TEXT_MODEL`
- `AI_POLISH_MODEL`
- optional `AI_VISION_MODEL`
- optional `AI_IMAGE_MODEL`
- `OPENAI_COMPAT_BASE_URL`
- optional `OPENAI_COMPAT_API_KEY`
- optional `OPENAI_COMPAT_TIMEOUT_SECONDS`

Examples of OpenAI-compatible targets:
- OpenAI
- Ollama
- LM Studio
- vLLM
- local or self-hosted gateways that expose OpenAI-style chat endpoints

## Voice Setup
This repo uses three local files as the voice packet:
- `profiles/default/WhoAmI.md`
- `profiles/default/Voice.md`
- `profiles/default/Humanizer.md`

`WhoAmI.md` and `Voice.md` include a fixed `## Active Guardrails` block at the bottom. Fill the editable sections above that block, but leave the guardrails unchanged unless you are intentionally changing how the system composes identity, voice, and humanizer context.

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

If you have an X archive, you can still use it as reference material while answering the voice questionnaire. You can also import it directly into Clearfeed and let the app generate an archive-derived summary plus a proposed `Voice.md`.

## Archive Import
Archive import is folder-based in v1. Download your X archive, unzip it locally, and point Clearfeed at the root folder.

What Clearfeed extracts:
- authored tweets
- replies
- note tweets
- community tweets

What Clearfeed stores locally from the import:
- archive import metadata
- deduped authored archive items
- an archive-derived summary
- a reviewable `Voice.md` proposal

What Clearfeed writes to disk:
- `profiles/generated/ARCHIVE_VOICE.md`

Recommended flow:
1. Request and unzip your X archive.
2. Import it from the dashboard or helper script.
3. Review the generated archive summary.
4. Run `Archive Voice Build`.
5. Approve or reject the proposed `Voice.md` update.

Clearfeed keeps `WhoAmI.md` separate and does not rewrite `Humanizer.md`.

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

How archive import fits in:
- archive import gives you a stronger starting voice from your real post history
- voice review then keeps refining that voice from live dashboard decisions over time

Important rules:
- `Humanizer.md` is treated as fixed and is never auto-rewritten
- `Voice.md` is never auto-updated silently
- edits made inside the dashboard are the highest-signal feedback, so prefer editing there before you approve a post

## Troubleshooting
- `Missing required profile file(s)`: run `.\scripts\setup.ps1` and fill the files in `profiles/default/`.
- `Missing Playwright session state`: run `.\scripts\capture-x-session.ps1` after logging into X.
- Want to avoid X login issues in Playwright: use the default `.\scripts\capture-x-session.ps1` flow, which captures from a real Chrome or Edge session over CDP.
- Want to try the old managed-browser path anyway: run `.\scripts\capture-x-session.ps1 -UseManagedBrowser`.
- OpenAI-compatible text works but vision/image features are unavailable: set `AI_VISION_MODEL` and/or `AI_IMAGE_MODEL`, and make sure your provider actually supports those capabilities.
- Archive import fails: point the import at the unzipped archive root folder, not a random parent folder.
- Dashboard opens but nothing appears: make sure at least one list URL is set, or enable home timeline discovery.
- Approve button does not post: this is expected if X API credentials are not configured.
- Telegram actions do nothing: Telegram is optional and remains disabled until `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are configured.
- Vertex auth failures: verify `GOOGLE_CLOUD_PROJECT` and `GOOGLE_APPLICATION_CREDENTIALS`, then confirm the account has access to the configured models.
- Archive proposals or voice reviews feel weak on a local model: try a stronger hosted model for `AI_POLISH_MODEL`.
- Performance is worse from a OneDrive-backed path. Prefer cloning to a local folder like `C:\dev\clearfeed-twitter-x-dashboard` instead of `C:\Users\...\OneDrive\...`.
