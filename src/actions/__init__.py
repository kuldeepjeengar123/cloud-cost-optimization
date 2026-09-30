"""Actionable recommendations layer.

Turns the pipeline's free-text recommendations into structured, executable
``Action`` descriptors, persists them, and applies them through a swappable
executor:

    Phase 1 (now):  CSVExecutor  -> annotate the matching row in docs/*.csv
    Phase 2 (later): AWSExecutor -> stop/start instances, change config via boto3

The descriptor and the Teams "Apply" button stay identical across phases; only
the executor behind ``cfg.action_backend`` changes.
"""

from .approval import (
    ApprovalError,
    commit_batch,
    commit_status,
    raise_for_review,
    reopen,
    stage_decision,
    unstage,
    withdraw,
)
from .decision_log import DecisionLogStore
from .executor import ApplyResult, get_executor
from .models import Action
from .planner import plan_actions
from .review import ALL, APPLY, QUIT, SKIP, review_and_apply
from .rollback import load_batch_snapshot, rollback_batch
from .store import ActionStore

__all__ = [
    "Action",
    "ActionStore",
    "ApplyResult",
    "get_executor",
    "plan_actions",
    "review_and_apply",
    "APPLY",
    "SKIP",
    "ALL",
    "QUIT",
    "DecisionLogStore",
    "ApprovalError",
    "raise_for_review",
    "withdraw",
    "reopen",
    "stage_decision",
    "unstage",
    "commit_batch",
    "commit_status",
    "rollback_batch",
    "load_batch_snapshot",
]
