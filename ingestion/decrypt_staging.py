"""Week 4 demo helper: prove the staging file is actually encrypted, then decrypt it.

Run: python decrypt_staging.py staging/<name>.json.enc
"""
import sys
from pathlib import Path

from encryption import decrypt_json


def main():
    if len(sys.argv) != 2:
        print("Usage: python decrypt_staging.py <staging_file.json.enc>")
        sys.exit(1)

    path = Path(sys.argv[1])
    raw = path.read_bytes()
    print(f"On disk (first 80 bytes, encrypted): {raw[:80]!r}")

    records = decrypt_json(path)
    print(f"\nDecrypted {len(records)} record(s):")
    for r in records:
        print(f"  {r}")


if __name__ == "__main__":
    main()
