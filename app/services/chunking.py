"""Splitting document text into retrievable chunks.

Deterministic and dependency-free: the same text always produces the same
chunks, which is what makes `source_hash` a reliable staleness check.

Splits at the largest natural boundary that fits — paragraph, then line, then
sentence, then a hard cut — so a chunk rarely ends mid-thought.
"""

import hashlib
import re

DEFAULT_MAX_CHARS = 1000
DEFAULT_OVERLAP = 100

# Ordered widest-first: prefer to break at a blank line, fall back to a newline,
# then a sentence end, and only then cut mid-text.
_BOUNDARIES = (re.compile(r"\n\s*\n"), re.compile(r"\n"), re.compile(r"(?<=[.!?])\s+"))


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_point(text: str, limit: int) -> int:
    """Index to cut at, preferring the latest natural boundary before `limit`."""
    window = text[:limit]
    for boundary in _BOUNDARIES:
        matches = list(boundary.finditer(window))
        if matches:
            # Only worth honouring if it leaves a chunk of reasonable size;
            # otherwise a stray newline near the start shreds the document.
            candidate = matches[-1].end()
            if candidate >= limit // 2:
                return candidate
    return limit


def chunk_text(
    text: str, *, max_chars: int = DEFAULT_MAX_CHARS, overlap: int = DEFAULT_OVERLAP
) -> list[str]:
    """Split `text` into chunks of at most `max_chars`, overlapping by `overlap`.

    Overlap keeps a sentence that straddles a boundary retrievable from either
    side. Returns [] for text that is empty or only whitespace.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not 0 <= overlap < max_chars:
        raise ValueError("overlap must be non-negative and smaller than max_chars")

    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        remaining = text[start:]
        if len(remaining) <= max_chars:
            chunk = remaining.strip()
            if chunk:
                chunks.append(chunk)
            break

        cut = _split_point(remaining, max_chars)
        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)

        # Step forward by at least one character so a pathological boundary
        # cannot loop forever.
        advance = max(cut - overlap, 1)
        start += advance
    return chunks
