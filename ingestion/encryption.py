"""Fernet symmetric encryption for clean JSON output written to staging/.

Key resolution order:
    1. ENCRYPTION_KEY env var - a base64 Fernet key, for container/prod
       deployments where the key comes from the orchestrator / secrets
       manager rather than a file baked into the image.
    2. secret.key next to this file, generated on first use - fine for
       local dev, since it's a local demo.
"""
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

FOLDER = Path(__file__).parent
KEY_PATH = FOLDER / "secret.key"


def _load_or_create_key() -> bytes:
    env_key = os.environ.get("ENCRYPTION_KEY")
    if env_key:
        return env_key.encode("utf-8")
    if KEY_PATH.exists():
        return KEY_PATH.read_bytes()
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    return key


def get_fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def encrypt_json(records: list[dict], dest_path: Path) -> None:
    """Serialize records to JSON and write the Fernet-encrypted bytes to dest_path."""
    payload = json.dumps(records, default=str).encode("utf-8")
    token = get_fernet().encrypt(payload)
    dest_path.write_bytes(token)


def decrypt_json(src_path: Path) -> list[dict]:
    """Inverse of encrypt_json - reads an encrypted staging file back to records."""
    token = src_path.read_bytes()
    payload = get_fernet().decrypt(token)
    return json.loads(payload.decode("utf-8"))
