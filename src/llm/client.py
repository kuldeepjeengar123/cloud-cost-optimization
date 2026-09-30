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


class LLMQuotaExceededError(LLMError):
    """Every configured key was rejected with a quota/auth status (401/402/403/429)
    — as opposed to a transport error or a malformed response — so callers can
    show a short, specific message instead of dumping each key's raw failure text."""


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
            except LLMQuotaExceededError:
                # Every configured key is already confirmed out of quota —
                # retrying (and sleeping between attempts) can't change that,
                # so re-raise immediately instead of burning through the same
                # rate limit 3 times. Re-raised as-is (not wrapped in a plain
                # LLMError like the generic path below) so callers can still
                # tell this apart and show a short, specific message.
                raise
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
        # Every key's own failure is kept, not just the last one. Reporting
        # only the last is actively misleading when the keys fail differently
        # — a primary that is merely rate-limited (429) followed by a bad
        # fallback key (401) reads as an authentication problem, sending you
        # after the wrong cause entirely.
        failures: list[str] = []
        # True only if every failure below was a key-rejection status
        # (401/402/403/429) — a transport error or an upstream 5xx/200-with-
        # error-body means something other than "every key is out of quota",
        # so callers (the chat widget) can tell those apart and show a short
        # "quota exhausted" message only when it's actually that.
        all_quota_like = True
        for i, key in enumerate(self._keys):
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            try:
                resp = requests.post(self.cfg.base_url, headers=headers, json=payload, stream=stream, timeout=120)
            except requests.RequestException as exc:
                failures.append(f"key #{i + 1}: {exc}")
                all_quota_like = False
                log.warning("OpenRouter request failed (%s); trying the next configured key if any.", exc)
                continue
            if resp.status_code == 200:
                # OpenRouter (and the upstream providers it fronts) can return
                # HTTP 200 with an {"error": ...} body instead of a real
                # completion — seen in practice as "Upstream error from
                # Nvidia: Service temporarily overloaded" on the free
                # Nemotron model. Treat that the same as a non-200 failure
                # (try the next key) instead of returning it and letting a
                # bare `KeyError: 'choices'` surface deeper in the call stack.
                if not stream:
                    try:
                        body = resp.json()
                    except ValueError:
                        body = None
                    if isinstance(body, dict) and "error" in body:
                        err = body["error"]
                        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                        failures.append(f"key #{i + 1}: upstream error (HTTP 200 body): {msg}")
                        all_quota_like = False
                        log.warning(
                            "OpenRouter returned HTTP 200 with an error body (%s); "
                            "trying the next configured key if any.", msg,
                        )
                        if i + 1 < len(self._keys):
                            continue
                        break
                return resp
            failures.append(f"key #{i + 1}: HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code in self._KEY_FALLBACK_STATUSES:
                if i + 1 < len(self._keys):
                    log.warning("OpenRouter key rejected (HTTP %s); trying the next configured key.", resp.status_code)
                    continue
            else:
                all_quota_like = False
            break
        detail = "; ".join(failures) or "no configured API key produced a response"
        if failures and all_quota_like:
            raise LLMQuotaExceededError(detail)
        raise LLMError(detail)

    def _non_stream(self, payload: dict) -> str:
        resp = self._request(payload, stream=False)
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def _stream(self, payload: dict) -> str:
        return "".join(self._iter_sse_deltas(payload))

    def _iter_sse_deltas(self, payload: dict):
        """Yield each text delta from an OpenRouter SSE stream as it arrives.

        Raises ``LLMError`` on an error chunk (e.g. the upstream model was
        temporarily overloaded) instead of silently swallowing it — a 200
        status at the HTTP level doesn't guarantee every SSE chunk actually
        carries content; see ``_request``'s handling of the same failure mode
        for the non-streaming call. Without this, the stream would just end
        with zero deltas and no indication anything went wrong.
        """
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
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "error" in parsed:
                err = parsed["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                log.warning("OpenRouter stream returned an error chunk: %s", msg)
                raise LLMError(f"upstream error: {msg}")
            try:
                delta = parsed["choices"][0].get("delta", {}).get("content")
            except (KeyError, IndexError):
                continue
            if delta:
                yield delta

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
