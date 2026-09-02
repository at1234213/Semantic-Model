"""Step 21: the chunking algorithm."""

import pytest

from app.services.chunking import chunk_text, content_hash


def test_short_text_is_one_chunk() -> None:
    assert chunk_text("hello world") == ["hello world"]


def test_blank_text_produces_nothing() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_chunks_respect_the_size_limit() -> None:
    text = "word " * 2000
    for chunk in chunk_text(text, max_chars=200, overlap=20):
        assert len(chunk) <= 200


def test_chunking_is_deterministic() -> None:
    """Required for source_hash to be a reliable staleness check."""
    text = "Sentence one. Sentence two.\n\n" * 50
    assert chunk_text(text, max_chars=300) == chunk_text(text, max_chars=300)


def test_paragraph_boundaries_are_preferred() -> None:
    text = "A" * 400 + "\n\n" + "B" * 400
    chunks = chunk_text(text, max_chars=500, overlap=0)
    assert chunks[0] == "A" * 400


def test_overlap_repeats_content_between_chunks() -> None:
    text = "x" * 1000
    chunks = chunk_text(text, max_chars=300, overlap=100)
    assert len(chunks) > 1
    assert sum(len(c) for c in chunks) > len(text)


def test_no_overlap_covers_the_text_exactly_once() -> None:
    text = "y" * 900
    chunks = chunk_text(text, max_chars=300, overlap=0)
    assert "".join(chunks) == text


def test_pathological_input_terminates() -> None:
    """A boundary at position zero must still advance."""
    assert len(chunk_text("\n" * 5000, max_chars=100, overlap=50)) >= 0


@pytest.mark.parametrize(
    "max_chars,overlap", [(0, 0), (-1, 0), (100, 100), (100, 150), (100, -1)]
)
def test_invalid_parameters_are_rejected(max_chars: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        chunk_text("some text", max_chars=max_chars, overlap=overlap)


def test_content_hash_is_stable_and_sensitive() -> None:
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")
    assert len(content_hash("abc")) == 64
