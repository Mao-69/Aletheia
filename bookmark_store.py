"""Local bookmark store: SHA-256 hash <-> room string.

Babel identifiers can stretch to over a million characters. A QR code can hold
about 3,000. To make QRs useful, we follow babel-master's approach:

  When a long room is "saved", store it on disk keyed by its SHA-256 hash.
  Refer to it via the short hash (`@HASH.W.S.B.P`) in QR codes and elsewhere.
  Resolve the hash back to the full room when the user comes to browse it.

This is a local-first store: rooms are kept in a single SQLite file in the
project directory. The choice of SQLite over a flat file:

* atomic writes (no risk of half-written room on power loss),
* O(1) lookup by hash regardless of how many rooms have been saved,
* concurrent reads are safe (we open a connection per call).

Hash collisions are theoretically possible but the working set will never
get close to enough rooms to make that a concern. With SHA-256, expected
collisions appear at ~2^128 stored rooms.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Final

# Use a short hash prefix for shorter URLs/QRs while keeping collision risk
# astronomically low. 16 hex chars = 64 bits = ~2^32 rooms before any
# expected collision. A typical user will never approach that.
HASH_LEN: Final[int] = 16


class BookmarkStore:
    """Thread-safe key-value store for hash -> room mappings."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # `check_same_thread=False` is fine because we serialise writes through _lock,
        # and SQLite handles concurrent reads natively.
        conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bookmarks (
                    hash TEXT PRIMARY KEY,
                    room TEXT NOT NULL,
                    created_at REAL DEFAULT (julianday('now'))
                )
            """)

    @staticmethod
    def hash_for_room(room: str) -> str:
        """Compute the canonical hash for a room string. Stable across processes."""
        return hashlib.sha256(room.encode("utf-8")).hexdigest()[:HASH_LEN]

    def remember(self, room: str) -> str:
        """Store the room and return its hash. Idempotent."""
        h = self.hash_for_room(room)
        with self._lock, self._connect() as conn:
            # ON CONFLICT DO NOTHING: if the room is already saved, we don't
            # overwrite anything. Two different rooms colliding on the same
            # truncated hash would surface here, but we accept the risk for
            # the URL/QR brevity win.
            conn.execute(
                "INSERT OR IGNORE INTO bookmarks (hash, room) VALUES (?, ?)",
                (h, room),
            )
        return h

    def lookup(self, hash_: str) -> str | None:
        """Resolve a hash back to its room string. Returns None if unknown."""
        if not hash_:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT room FROM bookmarks WHERE hash = ?", (hash_,)
            ).fetchone()
        return row[0] if row else None

    def count(self) -> int:
        with self._connect() as conn:
            (n,) = conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()
        return n


def is_hashed_identifier(identifier: str) -> bool:
    """`@HASH.WALL.SHELF.BOOK.PAGE` form — uses a stored hash instead of a full room."""
    return identifier.startswith("@")


def split_hashed_identifier(identifier: str) -> tuple[str, str]:
    """Split `@HASH.WALL.SHELF.BOOK.PAGE` -> (hash, suffix-after-hash).

    The suffix retains the leading dot so the caller can re-assemble:
        full_id = room + suffix
    """
    if not identifier.startswith("@"):
        raise ValueError("Not a hashed identifier")
    rest = identifier[1:]
    dot_at = rest.find(".")
    if dot_at < 0:
        raise ValueError("Hashed identifier missing tail")
    hash_part = rest[:dot_at]
    suffix = rest[dot_at:]  # includes the leading "."
    return hash_part, suffix
