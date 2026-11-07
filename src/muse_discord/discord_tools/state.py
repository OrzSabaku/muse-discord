"""Read positions, independent of the monitor's delivery queues."""

import os
import sqlite3
from contextlib import contextmanager


@contextmanager
def read_state(path):
    # SQLite stores ids as text: Discord snowflakes need unsigned 64 bits.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path, timeout=5)
    try:
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS read_state "
                "(channel_id TEXT PRIMARY KEY, message_id TEXT NOT NULL)"
            )
        yield ReadState(db)
    finally:
        db.close()


class ReadState:
    def __init__(self, db):
        self.db = db

    def get(self, channel):
        row = self.db.execute(
            "SELECT message_id FROM read_state WHERE channel_id=?", (channel,)
        ).fetchone()
        return row[0] if row else None

    def advance(self, channel, message):
        # Serialize read/compare/write so concurrent readers cannot move back.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.get(channel)
            result = str(max(int(previous or 0), int(message)))
            self.db.execute(
                "INSERT INTO read_state VALUES (?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET "
                "message_id=excluded.message_id",
                (channel, result),
            )
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise
