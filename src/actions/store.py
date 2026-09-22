"""Action store — persists planned actions so a Teams link can resolve them.

A Teams "Apply" button opens ``/apply?id=<action_id>`` possibly minutes after
the report was generated, so the action set must outlive the pipeline run. This
is a tiny JSON-file store at ``outputs/pending_actions.json`` keyed by action id.
A threading lock guards concurrent applies from the web server.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from ..utils.logger import get_logger
from .models import STATUS_PENDING, Action

# Fields cleared on a full reset — everything the approval workflow (raise /
# apply / decline) writes onto an action, so it reads exactly as it did the
# moment plan_actions() first created it.
_DECISION_FIELDS = (
    "applied_at", "result_note", "raised_by", "raised_at",
    "decided_by", "decided_at", "decline_reason",
    "staged_by", "staged_at", "commit_batch_id", "committed_at",
)

log = get_logger("actions.store")


class ActionStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read action store (%s); starting empty.", exc)
            return {}

    def _write(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def upsert_many(self, actions: list[Action]) -> None:
        """Replace this run's pending actions; never touch a decided one.

        A "pending" action is a suggestion nobody has acted on yet, so it's
        only ever as fresh as the run that produced it: if this run no
        longer raises it, it's stale and gets dropped rather than sitting
        alongside the new batch forever. Anything a human has moved past
        "pending" (raised, applied, declined, dismissed, acknowledged) is
        left alone regardless of whether this run mentions it again.
        """
        with self._lock:
            data = self._read()
            incoming_ids = {action.id for action in actions}
            for existing_id, existing in list(data.items()):
                if existing.get("status") == STATUS_PENDING and existing_id not in incoming_ids:
                    del data[existing_id]
            for action in actions:
                existing = data.get(action.id)
                if existing and existing.get("status") != STATUS_PENDING:
                    continue
                data[action.id] = action.to_dict()
            self._write(data)

    def clear(self) -> int:
        """Wipe every action, decided or not. Called when a new pipeline run
        starts so the recommendations list only ever reflects the run in
        progress, instead of accumulating applied/dismissed/declined entries
        from every run that came before it. Returns the number removed."""
        with self._lock:
            data = self._read()
            self._write({})
            return len(data)

    def reset_all_to_pending(self) -> int:
        """Test-only: undo every decision, so the store reads exactly as it did
        right after the run that planned it — no re-run needed. Returns the
        number of actions reset."""
        with self._lock:
            data = self._read()
            for raw in data.values():
                raw["status"] = STATUS_PENDING
                for field_name in _DECISION_FIELDS:
                    raw[field_name] = None
            self._write(data)
            return len(data)

    def get(self, action_id: str) -> Optional[Action]:
        data = self._read()
        raw = data.get(action_id)
        return Action.from_dict(raw) if raw else None

    def all(self) -> list[Action]:
        return [Action.from_dict(v) for v in self._read().values()]

    def by_status(self, *statuses: str) -> list[Action]:
        return [a for a in self.all() if a.status in statuses]

    def save(self, action: Action) -> None:
        with self._lock:
            data = self._read()
            data[action.id] = action.to_dict()
            self._write(data)
