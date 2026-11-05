"""Shared Anthropic client helper. Import-safe without ANTHROPIC_API_KEY."""
from __future__ import annotations

import os
from typing import Any

DEFAULT_MODEL = "claude-sonnet-5-5"


def model_id() -> str:
    return os.environ.get("AUTOAPPLY_MODEL", DEFAULT_MODEL)


def get_client() -> Any:
    """Create the Anthropic client lazily (only when a stage actually calls Claude)."""
    from dotenv import load_dotenv
    import anthropic

    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Put it in .env (see .env.example).")
    return anthropic.Anthropic()


def complete_text(client: Any, system: str, prompt: str, max_tokens: int = 4096) -> str:
    """Single-turn call; returns concatenated text blocks. Tests pass a fake client."""
    resp = client.messages.create(
        model=model_id(),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
