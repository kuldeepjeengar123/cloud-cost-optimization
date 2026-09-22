"""Rollback support for a committed AWS batch.

``commit_batch()`` (see ``approval.py``) snapshots each targeted instance's
pre-change state into ``outputs/applied/<batch_id>/rollback.json`` *before*
calling the executor, so a bad batch can be undone without guessing at what
it used to look like. Only the ``aws`` backend produces anything worth
rolling back — the ``csv`` backend never mutates its source file, so there is
nothing to undo there.

Reversibility by op:
    stop      -> start the instance back up
    resize    -> resize back to the pre-change instance type
    tag       -> restore the pre-change values of the tag keys that changed
    terminate -> irreversible (the instance no longer exists in the mock/real
                 fleet); recorded with reversible=False and surfaced as such
    budget    -> no instance target; nothing to roll back
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..utils.logger import get_logger
from .decision_log import DecisionLogStore
from .models import Action
from .store import ActionStore

if TYPE_CHECKING:
    from ..config import PipelineConfig

log = get_logger("actions.rollback")

_REVERSIBLE_OPS = {"stop", "resize", "tag"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tags_dict(inst: dict) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in inst.get("Tags", [])}


def snapshot_before(action: Action, cfg: "PipelineConfig") -> dict | None:
    """Capture the pre-change state of whatever ``action`` (an aws-backend,
    staged_approve action) is about to touch. Returns None for non-aws
    actions or actions with no resolvable targets (nothing worth recording).
    """
    if action.backend != "aws" or not action.executable:
        return None
    try:
        from .executor import AWSExecutor

        instances = AWSExecutor(cfg).matched_instances(action)
    except Exception as exc:  # pragma: no cover - defensive only
        log.warning("Rollback snapshot failed for %s (%s)", action.id, exc)
        return None
    if not instances:
        return None
    return {
        "action_id": action.id,
        "title": action.title,
        "op": action.op or "",
        "reversible": (action.op or "") in _REVERSIBLE_OPS,
        "instances_before": [
            {
                "InstanceId": i["InstanceId"],
                "InstanceType": i["InstanceType"],
                "State": i.get("State", {}).get("Name", "unknown"),
                "Tags": _tags_dict(i),
            }
            for i in instances
        ],
    }


def write_batch_snapshot(cfg: "PipelineConfig", batch_id: str, snapshots: list[dict | None]) -> Path | None:
    entries = [s for s in snapshots if s]
    if not entries:
        return None
    folder = cfg.applied_folder / batch_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "rollback.json"
    payload = {"batch_id": batch_id, "created_at": _now(), "backend": "aws", "actions": entries}
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def load_batch_snapshot(cfg: "PipelineConfig", batch_id: str) -> dict | None:
    path = cfg.applied_folder / batch_id / "rollback.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def rollback_batch(
    cfg: "PipelineConfig",
    store: ActionStore,
    decision_log: DecisionLogStore,
    batch_id: str,
    actor: str,
) -> dict:
    """Undo every reversible action committed under ``batch_id``.

    Actions with no rollback entry (csv backend, or aws actions that had no
    resolvable targets) are absent from the snapshot and simply not touched.
    Irreversible ops (terminate) and ops with no live client are reported as
    skipped rather than silently ignored, so the RE team can see exactly what
    could and could not be undone.
    """
    snapshot = load_batch_snapshot(cfg, batch_id)
    if snapshot is None:
        raise ValueError(f"No rollback snapshot found for batch '{batch_id}'.")

    from .executor import get_executor

    executor = get_executor(cfg, backend="aws")
    if cfg.aws_use_mock:
        from ..mock_aws.client import ensure_mock_server

        ensure_mock_server(cfg.aws_endpoint_url)
    client = getattr(executor, "_client", None)

    results = []
    for entry in snapshot.get("actions", []):
        action_id = entry["action_id"]
        action = store.get(action_id)
        ok, note = False, None

        if not entry.get("reversible"):
            note = f"Not reversible (op='{entry.get('op')}'); no action taken."
        elif client is None:
            note = "AWS client unavailable; could not roll back."
        else:
            op = entry["op"]
            try:
                if op == "stop":
                    for inst in entry["instances_before"]:
                        client.start_instance(inst["InstanceId"])
                    ok = True
                    note = f"Restarted {len(entry['instances_before'])} instance(s)."
                elif op == "resize":
                    for inst in entry["instances_before"]:
                        client.resize_instance(inst["InstanceId"], inst["InstanceType"])
                    ok = True
                    note = f"Resized {len(entry['instances_before'])} instance(s) back to their original type."
                elif op == "tag":
                    for inst in entry["instances_before"]:
                        client.tag_instance(inst["InstanceId"], inst["Tags"])
                    ok = True
                    note = f"Restored tags on {len(entry['instances_before'])} instance(s)."
                else:
                    note = f"Unknown op '{op}'; no action taken."
            except Exception as exc:  # pragma: no cover - defensive only
                note = f"Rollback failed: {exc}"

        if action is not None:
            action.result_note = f"{action.result_note or ''} | Rolled back: {note}".strip(" |")
            store.save(action)
            decision_log.add({
                "at": _now(),
                "action_id": action.id,
                "title": action.title,
                "decision": "rolled_back" if ok else "rollback_skipped",
                "actor": actor,
                "estimated_savings": action.estimated_savings,
                "note": note,
            })
        results.append({"action_id": action_id, "ok": ok, "note": note})

    log.info("Rollback of batch %s: %s", batch_id, results)
    return {"batch_id": batch_id, "results": results}
