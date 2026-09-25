"""OpenRouter-backed LLM client used by every LLM step in the pipeline.

Wraps the existing OpenRouter chat-completions endpoint with both streaming and
non-streaming modes, JSON-extraction helpers, and a simple retry on transient
errors. Every pipeline step calls through here so the provider can be swapped
in one place later (e.g. Bedrock).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

from ..config import LLMConfig
from ..utils.logger import get_logger

log = get_logger("llm.client")


class LLMError(RuntimeError):
    pass


class LLMClient:
    # Non-200 statuses worth retrying on a different key — auth/quota
    # problems specific to *that* key. Anything else (bad request, 5xx from
    # the provider, etc.) won't be fixed by switching keys, so it's raised
    # immediately instead of burning the fallback key on the same failure.
    _KEY_FALLBACK_STATUSES = (401, 402, 403, 429)

    def __init__(self, cfg: LLMConfig):
        # Primary key first, then the optional OPENROUTER_API_KEY1 fallback —
        # deduped so a fallback that's identical to the primary (or unset)
        # doesn't get tried twice.
        seen: set[str] = set()
        self._keys: list[str] = []
        for key in (cfg.api_key, cfg.api_key_fallback):
            if key and key not in seen:
                self._keys.append(key)
                seen.add(key)
        if not self._keys:
            raise LLMError(
                "No OpenRouter API key set: set OPENROUTER_API_KEY "
                "(and optionally OPENROUTER_API_KEY1 as a fallback) in environment / .env"
            )
        self.cfg = cfg

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        stream: bool | None = None,
        retries: int = 2,
    ) -> str:
        model = model or self.cfg.model_analysis
        max_tokens = max_tokens or self.cfg.max_tokens
        stream = self.cfg.stream if stream is None else stream

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "max_tokens": max_tokens,
        }

        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                if stream:
                    return self._stream(payload)
                return self._non_stream(payload)
            except Exception as exc:
                last_err = exc
                log.warning("LLM attempt %s failed: %s", attempt + 1, exc)
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"LLM call failed after {retries + 1} attempts: {last_err}")

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        attempts: int = 3,
    ) -> Any | None:
        """Like ``complete`` but insists on parseable JSON.

        ``complete``'s built-in retry only fires on transport exceptions — a
        2xx response whose body is prose or truncated JSON passes straight
        through. Reasoning models (e.g. Nemotron) intermittently spend their
        token budget on chain-of-thought and emit unparseable output, which
        silently empties whatever step relied on it. This retries the *whole*
        call when extraction fails, nudging the model to emit JSON only.

        Returns the parsed value, or ``None`` if every attempt is unparseable.
        """
        nudge = ""
        for attempt in range(1, attempts + 1):
            raw = self.complete(
                system=system,
                user=user + nudge,
                model=model,
                max_tokens=max_tokens,
            )
            parsed = self.extract_json(raw)
            if parsed is not None:
                return parsed
            log.warning(
                "JSON extraction failed on attempt %s/%s (raw len=%s); retrying.",
                attempt, attempts, len(raw or ""),
            )
            nudge = (
                "\n\nIMPORTANT: Your previous reply could not be parsed. "
                "Reply with ONLY the JSON object — no reasoning, no prose, "
                "no markdown fences."
            )
        return None

    def _request(self, payload: dict, *, stream: bool) -> requests.Response:
        """POST to OpenRouter, trying each configured API key in turn.

        A key that's invalid, out of credit, or rate-limited (401/402/403/429)
        falls through to the next configured key automatically instead of
        failing the call outright. Any other failure (bad request, a 5xx from
        the provider, a network error) isn't a key problem, so it's raised
        right away rather than burning through the fallback key too.
        """
        last_err: Exception | None = None
        for i, key in enumerate(self._keys):
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            try:
                resp = requests.post(self.cfg.base_url, headers=headers, json=payload, stream=stream, timeout=120)
            except requests.RequestException as exc:
                last_err = exc
                log.warning("OpenRouter request failed (%s); trying the next configured key if any.", exc)
                continue
            if resp.status_code == 200:
                return resp
            last_err = LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            if resp.status_code in self._KEY_FALLBACK_STATUSES and i + 1 < len(self._keys):
                log.warning("OpenRouter key rejected (HTTP %s); trying the next configured key.", resp.status_code)
                continue
            break
        raise last_err

    def _non_stream(self, payload: dict) -> str:
        resp = self._request(payload, stream=False)
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def _stream(self, payload: dict) -> str:
        return "".join(self._iter_sse_deltas(payload))

    def _iter_sse_deltas(self, payload: dict):
        """Yield each text delta from an OpenRouter SSE stream as it arrives."""
        resp = self._request(payload, stream=True)
        for line in resp.iter_lines():
            if not line:
                continue
            text = line.decode("utf-8")
            if not text.startswith("data: "):
                continue
            chunk = text[6:]
            if chunk == "[DONE]":
                break
            try:
                parsed = json.loads(chunk)
                delta = parsed["choices"][0].get("delta", {}).get("content")
                if delta:
                    yield delta
            except (json.JSONDecodeError, KeyError, IndexError):
                continue

    def stream(self, system: str, user: str, *, model: str | None = None, max_tokens: int | None = None):
        """Yield response text deltas as they arrive.

        For callers that want to forward tokens to a client in real time
        (the chat widget's SSE endpoint) instead of waiting for the full
        completion the way ``complete()``/``complete_json()`` do. Raises
        ``LLMError`` once every configured key has been tried and rejected
        (see ``_request``); a mid-stream transport error surfaces as
        whatever ``requests`` raises from the iterator, since there is no
        completed response left to retry.
        """
        model = model or self.cfg.model_analysis
        max_tokens = max_tokens or self.cfg.max_tokens
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": True,
            "max_tokens": max_tokens,
        }
        yield from self._iter_sse_deltas(payload)

    @staticmethod
    def extract_json(text: str) -> Any:
        """Best-effort JSON extraction from an LLM response.

        Tries a fenced ```json block first, then the first balanced {...} or
        [...] span. Returns None if nothing parseable is found so callers can
        fall back to the raw text.
        """
        if not text:
            return None
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fenced:
            try:
                return json.loads(fenced.group(1).strip())
            except json.JSONDecodeError:
                pass
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start != -1 and end > start:
                candidate = text[start : end + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue
        return None
