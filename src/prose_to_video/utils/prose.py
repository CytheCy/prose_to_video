"""UTF-8 prose validation and simple text statistics."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


WORD_RE = re.compile(r"\b\w+(?:['’]\w+)*\b", re.UNICODE)
PARAGRAPH_RE = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class ProseStats:
    characters: int
    words: int
    paragraphs: int


def count_prose(text: str) -> ProseStats:
    """Count Unicode characters, regex words, and blocks split by blank lines.

    A word is a Unicode letter/digit/underscore sequence with optional internal
    apostrophes. Hyphens split words; Markdown punctuation is ignored. A
    paragraph is a nonempty block separated by one or more blank lines.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [part for part in PARAGRAPH_RE.split(normalized) if part.strip()]
    return ProseStats(len(text), len(WORD_RE.findall(text)), len(paragraphs))


def read_prose(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            return stream.read()
    except UnicodeDecodeError as exc:
        raise ValueError("Prose must be UTF-8 text; this file could not be decoded") from exc
