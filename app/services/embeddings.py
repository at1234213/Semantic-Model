"""Turning text into vectors.

Two providers behind one protocol:

    hash                   deterministic, dependency-free, no model download
    sentence_transformers  the real encoder, imported lazily

The provider is recorded on every row it embeds, so switching encoders makes
the old vectors visibly stale rather than silently incomparable — cosine
distance between two different models' outputs is meaningless, not merely
inaccurate.
"""

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol, runtime_checkable

from app.core.config import get_settings

# Fixed in the column type, so changing it is a migration rather than a setting.
EMBEDDING_DIMENSIONS = 384

_TOKEN = re.compile(r"[a-z0-9]+")


@runtime_checkable
class Embedder(Protocol):
    """Anything that can turn text into fixed-width vectors."""

    name: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """A hashing vectoriser: tokens are hashed into buckets, then L2-normalised.

    Not semantic — it cannot tell that "revenue" and "turnover" are related. But
    it is deterministic, needs no model download, and preserves lexical overlap,
    so similar text really does score closer. That makes it usable for tests and
    for exercising the retrieval path without a 2GB image.
    """

    name = "hash-v1"

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self.dimensions = dimensions

    def _bucket(self, token: str) -> int:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self.dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in _TOKEN.findall(text.lower()):
                vector[self._bucket(token)] += 1.0
            norm = math.sqrt(sum(value * value for value in vector))
            # An empty or punctuation-only string has no direction; a zero
            # vector is the honest answer and cosine distance treats it as
            # maximally distant from everything.
            vectors.append([value / norm for value in vector] if norm else vector)
        return vectors


class SentenceTransformerEmbedder:
    """The real encoder. Imported lazily so the package is optional."""

    def __init__(self, model_name: str, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise RuntimeError(
                "EMBEDDING_PROVIDER is 'sentence_transformers' but the package is not "
                "installed. Add sentence-transformers to requirements.txt and rebuild, "
                "or set EMBEDDING_PROVIDER=hash."
            ) from exc

        self.name = model_name
        self.dimensions = dimensions
        self._model = SentenceTransformer(model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        encoded = self._model.encode(texts, normalize_embeddings=True)
        return [list(map(float, row)) for row in encoded]


@lru_cache
def get_embedder() -> Embedder:
    settings = get_settings()
    provider = settings.embedding_provider

    if provider == "hash":
        return HashEmbedder(settings.embedding_dim)
    if provider == "sentence_transformers":
        return SentenceTransformerEmbedder(settings.embedding_model, settings.embedding_dim)
    raise ValueError(
        f"Unknown EMBEDDING_PROVIDER {provider!r}. Expected 'hash' or 'sentence_transformers'."
    )
