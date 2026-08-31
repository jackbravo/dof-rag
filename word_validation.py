"""Validation helpers for downloaded Microsoft Word payloads."""

from __future__ import annotations

from pathlib import Path

MIN_FILE_SIZE = 1024
WORD_SIGNATURES = (
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",  # OLE Compound File (.doc)
    b"PK\x03\x04",  # OOXML ZIP container (.docx)
)


def is_valid_word_payload(content: bytes) -> bool:
    """Return whether bytes have a supported Word signature and size."""
    return len(content) >= MIN_FILE_SIZE and content.startswith(WORD_SIGNATURES)


def is_valid_word_file(path: Path) -> bool:
    """Validate a Word file without reading the whole payload into memory."""
    try:
        if path.stat().st_size < MIN_FILE_SIZE:
            return False
        with path.open("rb") as stream:
            return stream.read(8).startswith(WORD_SIGNATURES)
    except OSError:
        return False
