"""Embedding generation.

Placeholder implementation — swap in a real model (sentence-transformers,
an API-backed encoder, etc.) once the pipeline is settled.
"""

from app.core.config import get_settings


def embed(text: str) -> list[float]:
    dim = get_settings().embedding_dim
    raise NotImplementedError(f"Wire up an encoder producing {dim}-dim vectors for: {text[:40]!r}")
