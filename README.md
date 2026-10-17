# eclass deadline bot

Telegram bot that tracks deadlines from eclass.inha.ac.kr (Moodle). Single file: `bot.py` (aiogram 3, aiohttp, requests, BeautifulSoup, SQLite).

## What it does

- Logs in to eclass like a browser and reads every course page you're enrolled in every `POLL_MINUTES` (default 20): assignments and quizzes with their **deadline** and whether each is **submitted**. Alerts on **new** and **moved** deadlines.
  ✅ submitted · ❌ not yet · ❔ couldn't tell (e.g. "submit on Gradescope"). Submitted items get no reminders and are hidden from `/today` and `/week`.
- **Reminders** 3 days, 1 day and 3 hours before each deadline, once each. Items due together arrive as one message.
- **Course materials** (files, lecture videos, folders, links, pages — anything without a submission): new ones are announced as 📚 *New material*, grouped by course. `/resources` lists the latest 15; `/resources database` filters by course or title. The first run only records what's already there.
- With `MOODLE_TOKEN`: new course announcements from the news forums.

Message format: `❌ Homework 04 · Electronic Circuit · 23:59 · 1d 10h` (the title links to the activity).

## Setup

1. Create a bot with @BotFather.
2. `./setup.sh` (venv, deps, `.env`, systemd user service). Logs: `journalctl --user -u eclass-bot -f`

Manual: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, copy `.env.example` to `.env`, `.venv/bin/python bot.py`.
Quote the password in `.env` if it contains spaces or `#`. `.env` is mode 600 and credentials are never logged. SSO-only accounts are not supported (the plain username/password form is used).

## Commands

`/upcoming` next 15 · `/today` next 24 h · `/week` next 7 days · `/resources [course]` latest materials · `/sync` refresh now · `/help` explains everything (also shown by `/start`, with tap-able buttons; the same list is in Telegram's command menu)

Only the chat id in `.env` is answered.

## Tests

```
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
