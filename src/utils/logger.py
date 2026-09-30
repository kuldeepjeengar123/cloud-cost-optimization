"""Minimal step-aware logger used by the pipeline."""

from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        # stderr, not stdout: mcp_server.py runs over the stdio transport,
        # where stdout must carry nothing but the JSON-RPC message stream —
        # a single log line on stdout corrupts that framing and breaks the
        # connection (observed as "Connection closed" during the client's
        # initialize handshake). stderr is unaffected and still shows up in
        # a terminal or redirected log file for every entry point.
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
