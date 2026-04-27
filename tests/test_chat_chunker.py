"""Tests for the Telegram message chunker."""

from __future__ import annotations

import pytest

from kai_trader.chat.chunker import chunk_for_telegram


def test_short_text_returns_single_chunk() -> None:
    assert chunk_for_telegram("hello world") == ["hello world"]


def test_empty_text_returns_empty_list() -> None:
    assert chunk_for_telegram("") == []


def test_splits_on_paragraph_boundaries() -> None:
    paragraph = "x" * 800
    text = "\n\n".join([paragraph] * 6)
    chunks = chunk_for_telegram(text, cap=2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert len(chunks) >= 2
    # Each chunk should still be made of complete paragraphs.
    for chunk in chunks:
        for piece in chunk.split("\n\n"):
            assert piece == paragraph


def test_oversized_paragraph_splits_on_sentence() -> None:
    long_para = ("Sentence one. " * 200).strip()
    chunks = chunk_for_telegram(long_para, cap=400)
    assert all(len(c) <= 400 for c in chunks)


def test_huge_word_falls_back_to_hard_cut() -> None:
    blob = "a" * 9000
    chunks = chunk_for_telegram(blob, cap=4000)
    assert chunks
    assert all(len(c) <= 4000 for c in chunks)
    assert "".join(chunks) == blob


def test_zero_cap_rejected() -> None:
    with pytest.raises(ValueError):
        chunk_for_telegram("anything", cap=0)
