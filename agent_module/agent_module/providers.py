from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

_CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> Dict[str, Any]:
    with _CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def list_client_names(config: Optional[Dict[str, Any]] = None) -> List[str]:
    config = config or load_config()
    return list(config.get("clients", {}).keys())


def build_llm_client(client_name: str, config: Optional[Dict[str, Any]] = None) -> ChatOpenAI:
    """
    Build an OpenRouter-backed ChatOpenAI client for one entry in config.json's
    "clients" map. Each client's key, model, base_url, and timeout can be
    overridden independently via environment variables named
    "<CLIENT_NAME_UPPER>_API_KEY" / "_MODEL" / "_BASE_URL" / "_TIMEOUT",
    falling back to config.json, then to a shared OPENROUTER_API_KEY.

    Adding a new client (a third model, a different provider routed through
    OpenRouter, etc.) only requires a new entry in config.json — no code change.
    """
    config = config or load_config()
    client_cfg = config.get("clients", {}).get(client_name)
    if client_cfg is None:
        raise RuntimeError(f"No client config found for '{client_name}' in config.json.")

    env_prefix = client_name.upper()
    api_key = os.getenv(f"{env_prefix}_API_KEY") or os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            f"No API key set for client '{client_name}'. "
            f"Set {env_prefix}_API_KEY or a shared OPENROUTER_API_KEY."
        )

    base_url = (
        os.getenv(f"{env_prefix}_BASE_URL")
        or client_cfg.get("base_url", "https://openrouter.ai/api/v1")
    ).rstrip("/")
    model = os.getenv(f"{env_prefix}_MODEL") or client_cfg.get("model")
    if not model:
        raise RuntimeError(f"No model configured for client '{client_name}'.")
    timeout = int(os.getenv(f"{env_prefix}_TIMEOUT") or client_cfg.get("timeout", 120))

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        temperature=config.get("temperature", 0.2),
    )


def build_embeddings_client(config: Optional[Dict[str, Any]] = None) -> OpenAIEmbeddings:
    """
    Build an OpenRouter-backed OpenAIEmbeddings client from config.json's
    "embeddings" entry. Follows the same override pattern as build_llm_client:
    EMBEDDINGS_API_KEY / EMBEDDINGS_MODEL / EMBEDDINGS_BASE_URL env vars,
    falling back to config.json, then to a shared OPENROUTER_API_KEY.
    """
    config = config or load_config()
    emb_cfg = config.get("embeddings", {})

    api_key = os.getenv("EMBEDDINGS_API_KEY") or os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No API key set for embeddings. Set EMBEDDINGS_API_KEY or a shared OPENROUTER_API_KEY."
        )

    base_url = (
        os.getenv("EMBEDDINGS_BASE_URL")
        or emb_cfg.get("base_url", "https://openrouter.ai/api/v1")
    ).rstrip("/")
    model = os.getenv("EMBEDDINGS_MODEL") or emb_cfg.get("model", "nvidia/llama-nemotron-embed-vl-1b-v2:free")

    return OpenAIEmbeddings(model=model, api_key=api_key, base_url=base_url, check_embedding_ctx_length=False, encoding_format="float")
