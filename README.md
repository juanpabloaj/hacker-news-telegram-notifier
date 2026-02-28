# Hacker News Telegram Notifier

A simple Python service that polls Hacker News and sends Telegram notifications when someone comments on one of your submitted posts/comments or replies to one of your comments.

## Features

- Monitors all IDs from `user/{username}.json -> submitted`.
- Stores tracked child comments (`kids`) in SQLite (`state.db`).
- Sends Telegram notifications for new child comments only.
- First startup is bootstrap-only (no notifications for existing comments).
- Configurable polling interval (default 5 minutes).
- Simple retry logic for temporary network/API errors.
- Docker and Docker Compose support.

## Requirements

- Python 3.10+
- A Telegram bot token
- Telegram chat ID where notifications will be sent

## 1) Create a Telegram bot

1. Open Telegram and find `@BotFather`.
2. Send `/newbot` and follow the steps.
3. Save the bot token (`TELEGRAM_BOT_TOKEN`).

## 2) Get your Telegram chat ID

### Option A: personal chat

1. Start a chat with your bot and send one message.
2. Open:
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
3. Find `chat.id` in the response.

### Option B: group/channel

1. Add the bot to the target group/channel.
2. Send a message in the group/channel.
3. Call `getUpdates` as above and copy the `chat.id`.

## 3) Configure environment variables

Copy the example file and edit values:

```bash
cp .env.example .env
```

Required variables:

- `HN_USERNAME`: your Hacker News username.
- `TELEGRAM_BOT_TOKEN`: bot token from BotFather.
- `TELEGRAM_CHAT_ID`: destination chat ID.
- `POLL_INTERVAL_MINUTES`: polling interval in minutes (default `5`).

## 4) Run locally with uv (recommended)

```bash
cp .env.example .env
uv run python hn_notifier.py
```

Notes:

- No manual virtualenv activation is required.
- On first run, `uv` creates `.venv` and installs dependencies from `pyproject.toml`.

## 5) Run locally with pip (alternative)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python hn_notifier.py
```

The service will bootstrap state first and then run forever.

## 6) Run with Docker

```bash
docker compose up -d --build
```

Logs:

```bash
docker compose logs -f
```

Stop:

```bash
docker compose down
```

## systemd unit template

A generic `systemd` unit file is available at `systemd/hn-notifier.service`.
Replace `<APP_USER>` and `<APP_DIR>` with your server values before installing it in `/etc/systemd/system/`.

## How it works

1. On startup, the app reads all `submitted` item IDs for your user.
2. It stores those IDs and the current `kids` values in SQLite.
3. Every polling cycle:
   - It refreshes submitted IDs and adds newly submitted items.
   - It checks each monitored item for new `kids`.
   - For each new child comment ID, it fetches details and sends Telegram notification:
     - author
     - text preview (max 300 chars)
     - direct Hacker News link
4. It stores new child IDs so each comment is notified once.

## Notes

- HN comment text can include minimal HTML; the service strips tags for Telegram readability.
- If Hacker News or Telegram fails temporarily, requests are retried.
- The persistent `state.db` file allows safe restarts without duplicate notifications.
