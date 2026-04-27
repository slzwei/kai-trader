"""Split long replies for Telegram's 4096-character limit.

We aim for chunks under ~4000 chars to leave headroom for HTML escapes.
Splits prefer paragraph boundaries (``\\n\\n``), then sentence boundaries
inside an oversized paragraph, then fall back to a hard cut.
"""

from __future__ import annotations

import re

DEFAULT_CAP = 4000

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def chunk_for_telegram(text: str, *, cap: int = DEFAULT_CAP) -> list[str]:
    """Return the message split into chunks, each at or below ``cap`` chars."""
    if cap < 1:
        raise ValueError("cap must be >= 1")
    if len(text) <= cap:
        return [text] if text else []

    chunks: list[str] = []
    buffer: list[str] = []
    buffer_len = 0

    for paragraph in text.split("\n\n"):
        # A single oversized paragraph: split it and flush as we go.
        if len(paragraph) > cap:
            if buffer:
                chunks.append("\n\n".join(buffer))
                buffer, buffer_len = [], 0
            chunks.extend(_split_paragraph(paragraph, cap))
            continue

        block_len = len(paragraph) + (2 if buffer else 0)
        if buffer_len + block_len > cap:
            chunks.append("\n\n".join(buffer))
            buffer = [paragraph]
            buffer_len = len(paragraph)
        else:
            buffer.append(paragraph)
            buffer_len += block_len

    if buffer:
        chunks.append("\n\n".join(buffer))

    return chunks


def _split_paragraph(paragraph: str, cap: int) -> list[str]:
    """Split a single oversized paragraph on sentence boundaries, then hard-cut."""
    out: list[str] = []
    pieces = _SENTENCE_BOUNDARY.split(paragraph) if paragraph else [""]
    buffer: list[str] = []
    buffer_len = 0
    for piece in pieces:
        if not piece:
            continue
        if len(piece) > cap:
            if buffer:
                out.append(" ".join(buffer))
                buffer, buffer_len = [], 0
            out.extend(_hard_cut(piece, cap))
            continue
        block_len = len(piece) + (1 if buffer else 0)
        if buffer_len + block_len > cap:
            out.append(" ".join(buffer))
            buffer = [piece]
            buffer_len = len(piece)
        else:
            buffer.append(piece)
            buffer_len += block_len
    if buffer:
        out.append(" ".join(buffer))
    return out


def _hard_cut(piece: str, cap: int) -> list[str]:
    return [piece[i : i + cap] for i in range(0, len(piece), cap)]
