"""
Week 5 containerized entrypoint: watches WATCH_FOLDER for new CSVs and runs
each one through the full Week 3/4 guardrail pipeline (validation, PII
masking, account gate, encryption, audit log), alongside a FastAPI /health
endpoint for the container runtime.

Config via env vars (see .env.example):
    WATCH_FOLDER            - folder to watch for incoming CSVs (default: ./incoming)
    ENCRYPTION_KEY          - Fernet key for staging output (see encryption.py)
    ACCOUNT_ALLOWLIST / ACCOUNT_DENYLIST - see accounts.py
    RATE_LIMIT_PER_MINUTE   - files/minute cap (default 5)
    PORT                    - health check port (default 8000)

Run locally:   python app.py
Run in Docker: see Dockerfile / docker-compose.yml
Health check:  GET /health -> {"status": "ok"}
"""
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

from pipeline import process_file
from rate_limiter import RateLimiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("app")

FOLDER = Path(__file__).parent
WATCH_FOLDER = Path(os.environ.get("WATCH_FOLDER", str(FOLDER / "incoming")))

# How long a file's size must stay unchanged before we treat it as fully written.
STABLE_SECONDS = 1.0
STABLE_POLL_INTERVAL = 0.25
STABLE_TIMEOUT = 30.0

rate_limiter = RateLimiter(max_per_minute=int(os.environ.get("RATE_LIMIT_PER_MINUTE", "5")))


def wait_until_stable(path: Path) -> bool:
    deadline = time.monotonic() + STABLE_TIMEOUT
    last_size = -1
    stable_since = None
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last_size:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= STABLE_SECONDS:
                return True
        else:
            stable_since = None
            last_size = size
        time.sleep(STABLE_POLL_INTERVAL)
    return False


class GuardrailHandler(FileSystemEventHandler):
    def __init__(self):
        self._seen = set()

    def _handle(self, path_str: str) -> None:
        path = Path(path_str)
        if path.suffix.lower() != ".csv" or path in self._seen:
            return
        self._seen.add(path)
        try:
            if wait_until_stable(path):
                logger.info(f"New file detected: {path.name}")
                process_file(path, rate_limiter)
            else:
                logger.warning(f"Gave up waiting for {path.name} to finish writing.")
        except Exception:
            logger.exception(f"Failed to process {path.name}")
        finally:
            self._seen.discard(path)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(event.dest_path)


def start_watcher() -> PollingObserver:
    # PollingObserver, not the default inotify-based Observer: Docker Desktop
    # bind mounts (Windows/Mac) don't propagate real filesystem events into
    # the container, so inotify never fires for host-side file drops.
    WATCH_FOLDER.mkdir(parents=True, exist_ok=True)
    logger.info(f"Watching {WATCH_FOLDER} for new CSVs...")
    observer = PollingObserver(timeout=1)
    observer.schedule(GuardrailHandler(), str(WATCH_FOLDER), recursive=False)
    observer.start()
    return observer


@asynccontextmanager
async def lifespan(app: FastAPI):
    observer = start_watcher()
    yield
    observer.stop()
    observer.join()


app = FastAPI(title="M1 Data Ingestion Pipeline", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
