"""
buffer_manager.py

Local store-and-forward buffer using SQLite3 (WAL mode).

Design: EVERY payload is written here first, then a separate
publisher loop drains it to MQTT and deletes rows on successful
publish (QoS1 ack). This is the simplest correct design -- there's
only one code path (write -> publish -> delete) instead of a
"try live publish, fall back to buffer on failure" branch that has
to be exactly right under crash/race conditions. It also means a
crash mid-publish just leaves the row for the next drain pass;
nothing is lost.

Ring behavior: once max_rows is hit, oldest rows are dropped to make
room for new ones rather than blocking ingestion -- protects flash
lifetime and guarantees the collector never backs up indefinitely
if the network is down for a long stretch.
"""

import sqlite3
import json
import time
import logging
import threading

logger = logging.getLogger("edge_collector.buffer")


class BufferManager:
    def __init__(self, db_path: str, max_rows: int = 200_000):
        self.db_path = db_path
        self.max_rows = max_rows
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    topic TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def enqueue(self, topic: str, payload: dict):
        with self._lock:
            self._conn.execute(
                "INSERT INTO outbox (ts, topic, payload) VALUES (?, ?, ?)",
                (time.time(), topic, json.dumps(payload)),
            )
            self._conn.commit()
            self._trim_if_needed()

    def _trim_if_needed(self):
        row = self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()
        count = row[0]
        if count > self.max_rows:
            overflow = count - self.max_rows
            self._conn.execute(
                """
                DELETE FROM outbox WHERE id IN (
                    SELECT id FROM outbox ORDER BY id ASC LIMIT ?
                )
                """,
                (overflow,),
            )
            self._conn.commit()
            logger.warning(
                "Buffer over max_rows (%d) -- dropped %d oldest rows",
                self.max_rows,
                overflow,
            )

    def peek_batch(self, batch_size: int):
        """Return up to batch_size oldest un-drained rows, FIFO order."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, ts, topic, payload FROM outbox ORDER BY id ASC LIMIT ?",
                (batch_size,),
            )
            return cur.fetchall()

    def delete_ids(self, ids):
        if not ids:
            return
        with self._lock:
            qmarks = ",".join("?" * len(ids))
            self._conn.execute(f"DELETE FROM outbox WHERE id IN ({qmarks})", ids)
            self._conn.commit()

    def pending_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def close(self):
        with self._lock:
            self._conn.close()
