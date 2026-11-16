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


# Any hash-prefixed line is structural, including nonstandard Markdown headings.
HASH_HEADING = re.compile(r"^[ \t]*(?P<marks>#+)[ \t]*(?P<title>.*?)[ \t]*#*[ \t]*$")


def heading_title(line: str) -> str | None:
    match = HASH_HEADING.match(line.lstrip('\ufeff').rstrip('\r\n'))
    return match['title'].strip() if match else None


def trim_before_first_section(text: str) -> str:
    """Drop editorial front matter before the first heading numbered 01.

    Leave manuscripts without that heading unchanged, and preserve the exact
    heading, prose, and line endings in manuscripts that have it.
    """
    offset = 0
    for line in text.splitlines(keepends=True):
        title = heading_title(line)
        if title is not None and re.match(r'01(?:$|\s|[.):\-–—])', title):
            return text[offset:]
        offset += len(line)
    return text


def without_headings(text: str) -> str:
    return ''.join(line for line in text.splitlines(keepends=True)
                   if heading_title(line) is None)


def prose_content_ranges(text, start, end, headings):
    """Subtract structural headings, preserving exact source offsets."""
    position = start
    for a, b in sorted(headings):
        if b <= position or a >= end:
            continue
        if text[position:a].strip():
            yield position, a
        position = min(end, b)
    if text[position:end].strip():
        yield position, end
