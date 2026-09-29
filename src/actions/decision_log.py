"""Decision log — an append-only record of every raise/approve/decline.

The two-stage approval dashboard (employee raises, RE team decides) shows this
back to both sides so a decline's reason is never lost. Stored as a small JSON
file next to ``pending_actions.json`` so it survives server restarts, same as
the action store — that file stays the source of truth this class reads from
(capped at MAX_ENTRIES, newest-first). When a ``PostgresStore`` is supplied,
every entry is also mirrored into its ``decision_log`` table: a second,
unbounded, durable copy of the same events (see src/storage/postgres.py) —
best-effort, so a database hiccup never blocks a raise/approve/decline.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..utils.logger import get_logger

if TYPE_CHECKING:
    from ..storage.postgres import PostgresStore

log = get_logger("actions.decision_log")

MAX_ENTRIES = 500  # simple cap so the file never grows unbounded in a demo


class DecisionLogStore:
    def __init__(self, path: Path, pg_store: Optional["PostgresStore"] = None):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._pg_store = pg_store

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read decision log (%s); starting empty.", exc)
            return []

    def add(self, entry: dict) -> None:
        with self._lock:
            data = self._read()
            data.insert(0, entry)  # newest first
            data = data[:MAX_ENTRIES]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        if self._pg_store is not None:
            self._pg_store.record_decision(
                at=entry.get("at"),
                action_id=entry.get("action_id"),
                title=entry.get("title"),
                decision=entry.get("decision"),
                actor=entry.get("actor"),
                estimated_savings=entry.get("estimated_savings"),
                note=entry.get("note"),
            )

    def all(self) -> list[dict]:
        return self._read()

    def clear(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("[]", encoding="utf-8")
