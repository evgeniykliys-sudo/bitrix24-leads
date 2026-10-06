"""Очередь заявок на случай, когда CRM недоступна: заявка сохраняется локально и досылается позже.
Клиент не должен страдать из-за того, что у Битрикс24 технические работы."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.getenv("OUTBOX_PATH") or "outbox.db"


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with _connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            result TEXT
        )""")


def put(payload: dict, error: str) -> int:
    with _connect() as conn:
        return conn.execute(
            "INSERT INTO outbox (payload, attempts, last_error, created_at) VALUES (?, 1, ?, ?)",
            (json.dumps(payload, ensure_ascii=False), error[:500], datetime.now().isoformat(timespec="seconds")),
        ).lastrowid


MAX_ATTEMPTS = 60  # ~час попыток раз в минуту; дальше ошибка явно не временная — разбирается вручную (админ уведомлён)


def pending() -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM outbox WHERE delivered_at IS NULL AND attempts < ? ORDER BY id",
                            (MAX_ATTEMPTS,)).fetchall()


def failed_again(item_id: int, error: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?", (error[:500], item_id))


def delivered(item_id: int, result: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE outbox SET delivered_at = ?, result = ? WHERE id = ? AND delivered_at IS NULL",
                     (datetime.now().isoformat(timespec="seconds"), result, item_id))
