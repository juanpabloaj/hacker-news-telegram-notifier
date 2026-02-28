#!/usr/bin/env python3
"""Hacker News to Telegram notifier service."""

import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Set

import requests
from dotenv import load_dotenv

HN_BASE_URL = "https://hacker-news.firebaseio.com/v0"
HN_USER_URL = HN_BASE_URL + "/user/{username}.json"
HN_ITEM_URL = HN_BASE_URL + "/item/{item_id}.json"
HN_ITEM_LINK = "https://news.ycombinator.com/item?id={item_id}"
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
DEFAULT_POLL_INTERVAL_MINUTES = 5
REQUEST_TIMEOUT_SECONDS = 15
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2
COMMENT_PREVIEW_LIMIT = 300


@dataclass
class Settings:
    hn_username: str
    telegram_bot_token: str
    telegram_chat_id: str
    poll_interval_minutes: int


def load_settings() -> Settings:
    load_dotenv()

    hn_username = os.getenv("HN_USERNAME", "").strip()
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    interval_raw = os.getenv("POLL_INTERVAL_MINUTES", str(DEFAULT_POLL_INTERVAL_MINUTES)).strip()

    missing = [
        name
        for name, value in [
            ("HN_USERNAME", hn_username),
            ("TELEGRAM_BOT_TOKEN", telegram_bot_token),
            ("TELEGRAM_CHAT_ID", telegram_chat_id),
        ]
        if not value
    ]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    try:
        interval = max(1, int(interval_raw))
    except ValueError as exc:
        raise ValueError("POLL_INTERVAL_MINUTES must be an integer") from exc

    return Settings(
        hn_username=hn_username,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        poll_interval_minutes=interval,
    )


class StateStore:
    """SQLite-backed storage for monitored items and notification deduplication."""

    def __init__(self, db_path: str = "state.db") -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self._create_tables()

    def _create_tables(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monitored_items (
                item_id INTEGER PRIMARY KEY
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS item_kids (
                item_id INTEGER NOT NULL,
                kid_id INTEGER NOT NULL,
                PRIMARY KEY (item_id, kid_id),
                FOREIGN KEY (item_id) REFERENCES monitored_items(item_id) ON DELETE CASCADE
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notified_kids (
                kid_id INTEGER PRIMARY KEY,
                notified_at INTEGER NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def reset_state(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM item_kids")
            self.conn.execute("DELETE FROM monitored_items")
            self.conn.execute("DELETE FROM notified_kids")
            self.conn.execute("DELETE FROM metadata")

    def add_monitored_items(self, item_ids: Iterable[int]) -> None:
        rows = [(item_id,) for item_id in item_ids]
        if not rows:
            return
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO monitored_items(item_id) VALUES (?)",
                rows,
            )

    def replace_monitored_items(self, item_ids: Iterable[int]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM monitored_items")
            self.conn.executemany(
                "INSERT OR IGNORE INTO monitored_items(item_id) VALUES (?)",
                [(item_id,) for item_id in item_ids],
            )

    def remove_monitored_items(self, item_ids: Iterable[int]) -> None:
        rows = [(item_id,) for item_id in item_ids]
        if not rows:
            return
        with self.conn:
            self.conn.executemany("DELETE FROM item_kids WHERE item_id = ?", rows)
            self.conn.executemany("DELETE FROM monitored_items WHERE item_id = ?", rows)

    def get_monitored_items(self) -> list[int]:
        rows = self.conn.execute("SELECT item_id FROM monitored_items ORDER BY item_id").fetchall()
        return [row[0] for row in rows]

    def get_known_kids(self, item_id: int) -> Set[int]:
        rows = self.conn.execute("SELECT kid_id FROM item_kids WHERE item_id = ?", (item_id,)).fetchall()
        return {row[0] for row in rows}

    def add_kids(self, item_id: int, kid_ids: Iterable[int]) -> None:
        rows = [(item_id, kid_id) for kid_id in kid_ids]
        if not rows:
            return
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO item_kids(item_id, kid_id) VALUES (?, ?)",
                rows,
            )

    def is_kid_notified(self, kid_id: int) -> bool:
        row = self.conn.execute("SELECT 1 FROM notified_kids WHERE kid_id = ?", (kid_id,)).fetchone()
        return row is not None

    def mark_kid_notified(self, kid_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO notified_kids(kid_id, notified_at) VALUES (?, ?)",
                (kid_id, int(time.time())),
            )

    def get_metadata(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_metadata(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


class HNClient:
    def __init__(self, session: requests.Session) -> None:
        self.session = session

    def fetch_user_submitted_ids(self, username: str) -> list[int]:
        data = self._get_json(HN_USER_URL.format(username=username))
        submitted = data.get("submitted", []) if isinstance(data, dict) else []
        return [item_id for item_id in submitted if isinstance(item_id, int)]

    def fetch_item(self, item_id: int) -> Optional[dict[str, Any]]:
        data = self._get_json(HN_ITEM_URL.format(item_id=item_id))
        return data if isinstance(data, dict) else None

    def _get_json(self, url: str) -> Any:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES:
                    raise RuntimeError(f"GET {url} failed after {MAX_RETRIES} attempts") from exc
                delay = RETRY_BACKOFF_SECONDS * attempt
                logging.warning("Request failed (%s). Retrying in %s seconds", exc, delay)
                time.sleep(delay)
        return None


class TelegramClient:
    def __init__(self, session: requests.Session, bot_token: str, chat_id: str) -> None:
        self.session = session
        self.url = TELEGRAM_SEND_URL.format(token=bot_token)
        self.chat_id = chat_id

    def send_notification(self, message: str) -> None:
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.post(self.url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
                response.raise_for_status()
                body = response.json()
                if not body.get("ok", False):
                    raise RuntimeError(f"Telegram API error: {body}")
                return
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                if attempt == MAX_RETRIES:
                    raise RuntimeError(
                        f"Failed to send Telegram message after {MAX_RETRIES} attempts"
                    ) from exc
                delay = RETRY_BACKOFF_SECONDS * attempt
                logging.warning("Telegram send failed (%s). Retrying in %s seconds", exc, delay)
                time.sleep(delay)


def extract_kids(item: Optional[dict[str, Any]]) -> Set[int]:
    if not item:
        return set()
    kids = item.get("kids", [])
    if not isinstance(kids, list):
        return set()
    return {kid_id for kid_id in kids if isinstance(kid_id, int)}


def strip_html_tags(text: str) -> str:
    # HN comment text is simple HTML. Remove tags for Telegram readability.
    import re

    without_tags = re.sub(r"<[^>]+>", "", text)
    return (
        without_tags.replace("&quot;", '"')
        .replace("&#x27;", "'")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )


def format_notification(comment_id: int, comment_data: dict[str, Any]) -> str:
    author = comment_data.get("by") or "unknown"
    text = comment_data.get("text") or "(no text)"
    if not isinstance(text, str):
        text = str(text)

    clean_text = strip_html_tags(text).strip()
    if len(clean_text) > COMMENT_PREVIEW_LIMIT:
        clean_text = clean_text[: COMMENT_PREVIEW_LIMIT - 3].rstrip() + "..."

    link = HN_ITEM_LINK.format(item_id=comment_id)
    return f"New HN reply/comment by {author}:\n\n{clean_text}\n\n{link}"


def send_comment_notification(
    kid_id: int,
    hn_client: HNClient,
    tg_client: TelegramClient,
    store: StateStore,
) -> bool:
    if store.is_kid_notified(kid_id):
        return False

    comment_data = hn_client.fetch_item(kid_id)
    if not comment_data:
        logging.warning("Could not fetch comment %s", kid_id)
        return False

    message = format_notification(kid_id, comment_data)
    tg_client.send_notification(message)
    store.mark_kid_notified(kid_id)
    logging.info("Sent notification for comment %s", kid_id)
    return True


def bootstrap_initial_state(settings: Settings, hn_client: HNClient, store: StateStore) -> None:
    """Initialize baseline only once. Existing comments are not notified."""
    logging.info("Initializing baseline state for user '%s'", settings.hn_username)

    submitted_ids = hn_client.fetch_user_submitted_ids(settings.hn_username)
    store.replace_monitored_items(submitted_ids)

    for idx, item_id in enumerate(submitted_ids, start=1):
        item = hn_client.fetch_item(item_id)
        store.add_kids(item_id, extract_kids(item))
        if idx % 100 == 0:
            logging.info("Initialized %s/%s items", idx, len(submitted_ids))

    store.set_metadata("hn_username", settings.hn_username)
    store.set_metadata("initialized_at", str(int(time.time())))
    logging.info("Baseline bootstrap completed. Monitoring %s submitted items", len(submitted_ids))


def ensure_state_initialized(settings: Settings, hn_client: HNClient, store: StateStore) -> None:
    stored_username = store.get_metadata("hn_username")
    if stored_username is None:
        bootstrap_initial_state(settings, hn_client, store)
        return

    if stored_username != settings.hn_username:
        logging.warning(
            "Stored user '%s' differs from configured user '%s'. Resetting local state.",
            stored_username,
            settings.hn_username,
        )
        store.reset_state()
        bootstrap_initial_state(settings, hn_client, store)
        return

    logging.info("Loaded existing state. Monitoring %s submitted items", len(store.get_monitored_items()))


def refresh_monitored_items(
    settings: Settings,
    hn_client: HNClient,
    tg_client: TelegramClient,
    store: StateStore,
) -> None:
    """Sync monitored submitted items and notify comments already present on newly seen items."""
    latest_submitted = hn_client.fetch_user_submitted_ids(settings.hn_username)
    latest_set = set(latest_submitted)
    current_set = set(store.get_monitored_items())

    new_items = sorted(latest_set - current_set)
    removed_items = sorted(current_set - latest_set)

    if removed_items:
        store.remove_monitored_items(removed_items)
        logging.info("Removed %s stale submitted items", len(removed_items))

    if not new_items:
        return

    store.add_monitored_items(new_items)
    logging.info("Discovered %s new submitted items", len(new_items))

    for item_id in new_items:
        item = hn_client.fetch_item(item_id)
        kids = extract_kids(item)
        if not kids:
            continue

        store.add_kids(item_id, kids)
        for kid_id in sorted(kids):
            send_comment_notification(kid_id, hn_client, tg_client, store)


def poll_once(hn_client: HNClient, tg_client: TelegramClient, store: StateStore) -> None:
    monitored_items = store.get_monitored_items()
    logging.info("Polling %s monitored items", len(monitored_items))

    for item_id in monitored_items:
        item_data = hn_client.fetch_item(item_id)
        current_kids = extract_kids(item_data)
        known_kids = store.get_known_kids(item_id)
        new_kids = sorted(current_kids - known_kids)

        if not new_kids:
            continue

        logging.info("Found %s new comments for item %s", len(new_kids), item_id)

        for kid_id in new_kids:
            send_comment_notification(kid_id, hn_client, tg_client, store)

        store.add_kids(item_id, new_kids)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> int:
    configure_logging()

    try:
        settings = load_settings()
    except ValueError as exc:
        logging.error("Configuration error: %s", exc)
        return 1

    session = requests.Session()
    session.headers.update({"User-Agent": "hn-telegram-notifier/1.0"})

    hn_client = HNClient(session)
    tg_client = TelegramClient(session, settings.telegram_bot_token, settings.telegram_chat_id)
    store = StateStore(db_path="state.db")

    try:
        ensure_state_initialized(settings, hn_client, store)
    except Exception as exc:  # noqa: BLE001
        logging.exception("State initialization failed: %s", exc)
        return 1

    interval_seconds = settings.poll_interval_minutes * 60
    logging.info("Starting polling loop every %s minutes", settings.poll_interval_minutes)

    while True:
        try:
            refresh_monitored_items(settings, hn_client, tg_client, store)
            poll_once(hn_client, tg_client, store)
        except Exception as exc:  # noqa: BLE001
            logging.exception("Polling cycle failed: %s", exc)

        time.sleep(interval_seconds)


if __name__ == "__main__":
    sys.exit(main())
