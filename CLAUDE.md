# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Telegram and Max messenger bot for Fabry disease screening questionnaires. Russian-language medical intake bot built with Python 3.11. Single entry point [bot/main.py](bot/main.py) that handles both platforms via the `obabot` abstraction layer (built on top of aiogram for Telegram and maxapi for Max). All business logic — STEPS, validators, scoring, reports, PDF generation — lives in [bot/core.py](bot/core.py).

There is also a separate webhook service in [webhook/webhook.py](webhook/webhook.py).

## Repository Layout

```
fabri_bot/
├── .env                      # tokens & config (in repo root)
├── docker-compose.yml
├── Dockerfile
├── bot/
│   ├── main.py               # entry point (Telegram + Max)
│   ├── core.py               # STEPS, validators, scoring, reports, PDF
│   ├── fabry_score_weights.json
│   └── requirements.txt
└── webhook/
    ├── webhook.py
    └── Dockerfile.webhook
```

Note: `bot/main.py` uses a flat import `from core import ...`, so it must be run with `bot/` as the working directory (or on `PYTHONPATH`). `python -m bot.main` from the repo root will fail.

## Commands

```bash
# Install dependencies
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r bot/requirements.txt

# Run locally (from bot/ directory — required because of flat `from core import`)
cd bot
python main.py

# .env lives in the repo root; load_dotenv() walks up from cwd and finds it.

# Run with Docker Compose
docker compose up -d
docker compose logs -f
docker compose down

# Syntax check (no test suite exists)
python -m py_compile bot/main.py bot/core.py
```

### systemd unit (Docker-less deployment)

```ini
[Unit]
Description=Fabri Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/fabri_bot/bot
EnvironmentFile=/opt/fabri_bot/.env
ExecStart=/opt/fabri_bot/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
User=fabri

[Install]
WantedBy=multi-user.target
```

## Architecture

**Single entry point, dual platform:** [bot/main.py](bot/main.py) starts a Telegram bot, a Max bot, or both, depending on which tokens are present in `.env`. Platform abstraction is provided by the `obabot` package (`from obabot import create_bot`, `BPlatform`, etc.); aiogram is still used directly for some Telegram-specific types (`TelegramBadRequest`, `AiohttpSession`, `InlineKeyboardBuilder`).

**Business logic in [bot/core.py](bot/core.py):**

- **Step system:** 31 `Step` dataclass instances in the `STEPS` list define the questionnaire. Each step has a `key`, `kind` (`choice` / `text` / `collect`), display text function, optional `condition` callable, and optional `validator`.
- **Conditional branching:** Steps can be skipped based on prior answers (e.g., pain trigger questions only appear if user reported pain). Conditions are callables receiving the `answers` dict.
- **Gender-aware text:** Several question texts vary by `sex` answer.
- **Risk scoring:** `calculate_fabry_score_details()` computes a cumulative score using weights loaded from [bot/fabry_score_weights.json](bot/fabry_score_weights.json); `get_score_interpretation()` returns the textual interpretation level.
- **Reports:** `build_survey_result()`, `build_group_report()`, `generate_pdf_report()` produce per-user output and group-chat summaries.
- **Group forwarding:** Completed surveys are optionally sent to group chat (`GROUP_CHAT_ID` / `MAX_GROUP_CHAT_ID`).
- **No database:** All state is in-memory (`MemoryStorage` FSM). Data exists only during the conversation session.

**FSM:** State machine defined in `main.py`; states include `waiting_consent`, `waiting_choice`, `waiting_text`, `collecting_additional`.

**Validators** (in `core.py`): `validate_age()`, `validate_nonempty()`, `validate_contact()` — signature `(str, dict) -> tuple[bool, str]`.

## Environment Variables

Configured via [.env](.env) in the repo root:

- `BOT_TOKEN` — Telegram Bot API token (required if you want Telegram)
- `MAX_BOT_TOKEN` — Max Bot API token (required if you want Max)
- At least one of `BOT_TOKEN` or `MAX_BOT_TOKEN` must be set, otherwise startup raises `RuntimeError`.
- `GROUP_CHAT_ID` (optional) — Telegram group chat to forward completed surveys
- `MAX_GROUP_CHAT_ID` (optional) — Max group chat to forward completed surveys
- `LOG_CHAT_ID` (optional) — Telegram chat to receive log messages
- `HOTLINE_PHONE` (optional) — displayed hotline number
- `CONSENT_DECLINE_PHONE` (optional) — alternative phone on consent decline
- `TG_PROXY` (optional) — proxy URL for Telegram (see [PROXY_SETUP.md](PROXY_SETUP.md) / [VLESS_PROXY_SETUP.md](VLESS_PROXY_SETUP.md))
- `TEST_MODE`, `TEST_BOT_TOKEN`, `TEST_GROUP_CHAT_ID` — when `TEST_MODE` is truthy, the test token/group are used in place of `BOT_TOKEN` / `GROUP_CHAT_ID`.

## Conventions

- Code style: PEP 8, type hints throughout
- Commit messages: conventional commits format (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`)
- Branch naming: `feature/`, `fix/`, `docs/`, `refactor/`, `test/`
- All user-facing text is in Russian
