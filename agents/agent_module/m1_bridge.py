"""Bridge from M1's real encrypted output to M2's raw_record input shape.

Pure local file I/O + Fernet decryption - no network, no LLM call, no AWS
call. M1 (ingestion/) and M2 (agents/) are separate, independently
runnable modules in this repo; this is the one place that knows about both
paths, so neither module has to import the other or add a cross-module
dependency. (Folder reorg, integration Step 13: m1_ingestion/ -> ingestion/,
agent_module/ -> agents/ - this file's own inner package name is unaffected.)

Replaces the Step 2 workflow of hand-copying a decrypted record into
sample_data/*.json - point run_pipeline_live_m1_bridge.py at whatever M1 has
actually produced in ingestion/staging/, live, no manual step in between.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

from cryptography.fernet import Fernet

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_M1_DIR = _REPO_ROOT / "ingestion"
_M1_STAGING = _M1_DIR / "staging"
_M1_KEY_PATH = _M1_DIR / "secret.key"


def _load_m1_key() -> bytes:
    if not _M1_KEY_PATH.exists():
        raise FileNotFoundError(
            f"No M1 encryption key at {_M1_KEY_PATH} - run ingestion/pipeline.py "
            "at least once first (it generates secret.key on first use)."
        )
    return _M1_KEY_PATH.read_bytes()


def decrypt_m1_file(enc_path: Path) -> List[dict]:
    """Decrypt one M1 staging/<name>.json.enc file back to its list of
    CostLineItem-shaped records. Mirrors ingestion/encryption.py's
    decrypt_json() exactly (duplicated rather than imported, so this module
    and ingestion/ stay independently runnable with no sys.path surgery)."""
    fernet = Fernet(_load_m1_key())
    token = enc_path.read_bytes()
    payload = fernet.decrypt(token)
    return json.loads(payload.decode("utf-8"))


def list_m1_staging_files() -> List[Path]:
    if not _M1_STAGING.exists():
        raise FileNotFoundError(f"No staging folder at {_M1_STAGING} - M1 hasn't produced any output yet.")
    return sorted(_M1_STAGING.glob("*.json.enc"), key=lambda p: p.stat().st_mtime)


def load_all_m1_records() -> List[dict]:
    """Decrypt every *.json.enc file currently in ingestion/staging/ and
    return their records concatenated, oldest file first."""
    records: List[dict] = []
    for enc_path in list_m1_staging_files():
        records.extend(decrypt_m1_file(enc_path))
    return records


def load_latest_m1_file_records() -> List[dict]:
    """Just the most recently modified staging file's records - useful for a
    single-file smoke test without pulling in every historical run."""
    files = list_m1_staging_files()
    if not files:
        raise FileNotFoundError(f"No *.json.enc files in {_M1_STAGING} yet.")
    return decrypt_m1_file(files[-1])
