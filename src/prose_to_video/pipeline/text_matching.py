"""Order-preserving manuscript/transcript matching with exact source offsets."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher

from prose_to_video.models.alignment import (AlignmentIssue, SceneTiming, SpanAlignment,
                                             TextPassage, TranscriptWord, WordAlignment)
from prose_to_video.models.scene import Scene
from prose_to_video.utils.prose import WORD_RE


@dataclass(frozen=True)
class Token:
    text: str
    start: int
    end: int


_NUMBERS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
            "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}


def normalize(word: str) -> str:
    value = word.casefold().replace("’", "'").replace("_", "")
    value = re.sub(r"[^\w']", "", value, flags=re.UNICODE).replace("'", "")
    return _NUMBERS.get(value, value)


def manuscript_tokens(text: str) -> list[Token]:
    return [Token(match.group(), match.start(), match.end()) for match in WORD_RE.finditer(text)]


def _anchors(a: list[str], b: list[str], size: int = 4) -> list[tuple[int, int]]:
    if min(len(a), len(b)) < size:
        return []
    aa = {tuple(a[i:i + size]): i for i in range(len(a) - size + 1)}
    bb = {tuple(b[i:i + size]): i for i in range(len(b) - size + 1)}
    ca = Counter(tuple(a[i:i + size]) for i in range(len(a) - size + 1))
    cb = Counter(tuple(b[i:i + size]) for i in range(len(b) - size + 1))
    words_a, words_b = Counter(a), Counter(b)
    candidates = sorted((aa[key], bb[key]) for key in ca.keys() & cb.keys()
                        if ca[key] == cb[key] == 1 and
                        words_a[key[0]] == words_b[key[0]] == 1)
    # Patience-style monotone anchor chain; avoids quadratic whole-book diff.
    from bisect import bisect_left
    tails: list[int] = []
    tail_indices: list[int] = []
    previous = [-1] * len(candidates)
    for k, (_, j) in enumerate(candidates):
        place = bisect_left(tails, j)
        if place == len(tails):
            tails.append(j)
            tail_indices.append(k)
        else:
            tails[place] = j
            tail_indices[place] = k
        if place:
            previous[k] = tail_indices[place - 1]
    chain = []
    if tail_indices:
        k = tail_indices[-1]
        while k >= 0:
            chain.append(candidates[k])
            k = previous[k]
    chain.reverse()
    result = []
    end_a = end_b = 0
    for i, j in chain:
        if i >= end_a and j >= end_b:
            result.append((i, j))
            end_a, end_b = i + size, j + size
    return result


def _align_gap(a: list[str], b: list[str]) -> list[tuple[int, int]]:
    """Bounded edit alignment; ties retain the earlier manuscript occurrence."""
    n, m = len(a), len(b)
    if not n or not m:
        return []
    if max(n, m) > 1500 or n * m > 600_000:
        matcher = SequenceMatcher(None, a, b, autojunk=True)
        return [(block.a + d, block.b + d) for block in matcher.get_matching_blocks()
                for d in range(block.size)]
    directions = bytearray((n + 1) * (m + 1))
    previous = [-j for j in range(m + 1)]
    for i in range(1, n + 1):
        current = [-i] + [0] * m
        row = i * (m + 1)
        for j in range(1, m + 1):
            up = previous[j] - 1
            left = current[j - 1] - 1
            diagonal = previous[j - 1] + (2 if a[i - 1] == b[j - 1] else -3)
            if up >= diagonal and up >= left:
                current[j], directions[row + j] = up, 2
            elif diagonal >= left:
                current[j], directions[row + j] = diagonal, 1
            else:
                current[j], directions[row + j] = left, 3
        previous = current
    result = []
    i, j = n, m
    while i and j:
        direction = directions[i * (m + 1) + j]
        if direction == 1:
            if a[i - 1] == b[j - 1]:
                result.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif direction == 2:
            i -= 1
        else:
            j -= 1
    return result[::-1]


def match_words(text: str, spoken: list[TranscriptWord]) -> tuple[list[WordAlignment], list[TextPassage], list[AlignmentIssue]]:
    source = manuscript_tokens(text)
    a = [normalize(token.text) for token in source]
    b = [normalize(word.text) for word in spoken]
    pairs: dict[int, int] = {}
    anchors = _anchors(a, b)
    ai = bi = 0
    for aj, bj in [*anchors, (len(a), len(b))]:
        for left, right in _align_gap(a[ai:aj], b[bi:bj]):
            pairs[ai + left] = bi + right
        if (aj, bj) != (len(a), len(b)):
            for d in range(4):
                pairs[aj + d] = bj + d
        ai, bi = aj + 4, bj + 4
    words = []
    matched_spoken = set()
    for i, token in enumerate(source):
        j = pairs.get(i)
        recognized = spoken[j] if j is not None else None
        timed = (recognized is not None and recognized.start is not None
                 and recognized.end > recognized.start)
        if j is not None:
            matched_spoken.add(j)
        words.append(WordAlignment(source_start=token.start, source_end=token.end,
                                   text=token.text, recognized=recognized.text if recognized else None,
                                   audio_start=recognized.start if timed else None,
                                   audio_end=recognized.end if timed else None,
                                   status="matched" if timed else "unmatched",
                                   confidence=recognized.score if timed else None))
    # Do not allow a later word to point backward in audio after imperfect ASR.
    last = 0.0
    for i, word in enumerate(words):
        if word.audio_start is not None:
            if word.audio_start + 0.02 < last:
                words[i] = word.model_copy(update={"audio_start": None, "audio_end": None,
                                                   "status": "unmatched"})
            else:
                last = max(last, word.audio_end)
    passages = []
    items = sorted(pairs.items())
    groups: list[list[tuple[int, int]]] = []
    for item in items:
        if not groups or item[0] != groups[-1][-1][0] + 1 or item[1] != groups[-1][-1][1] + 1:
            groups.append([])
        groups[-1].append(item)
    for group in groups:
        first, last_pair = group[0], group[-1]
        passages.append(TextPassage(source_start=source[first[0]].start,
                                    source_end=source[last_pair[0]].end,
                                    transcript_start=first[1], transcript_end=last_pair[1] + 1,
                                    matched_words=len(group)))
    issues = []
    for j, word in enumerate(spoken):
        if j not in matched_spoken:
            issues.append(AlignmentIssue(kind="spoken_insertion", message=f"Unmatched speech: {word.text}",
                                         audio_start=word.start, audio_end=word.end))
    return words, passages, issues


def paragraph_ranges(text: str) -> list[tuple[int, int]]:
    parts: list[tuple[int, int]] = []
    active: int | None = None
    offset = 0

    def append(begin: int, finish: int) -> None:
        while begin < finish and text[begin].isspace(): begin += 1
        while finish > begin and text[finish - 1].isspace(): finish -= 1
        if begin < finish:
            parts.append((begin, finish))

    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        special = bool(re.fullmatch(r"[ \t]*(?:#{1,6}[ \t]+\S.*|(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})", body))
        if not body.strip() or special:
            if active is not None:
                append(active, offset)
                active = None
            if special:
                append(offset, offset + len(body))
        elif active is None:
            active = offset
        offset += len(line)
    if active is not None:
        append(active, len(text))
    return parts


_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e", "g"}


def sentence_ranges(text: str) -> list[tuple[int, int]]:
    result = []
    for start, end in paragraph_ranges(text):
        part = text[start:end]
        if re.fullmatch(r"[ \t]*(?:#{1,6}\s+.*|(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})", part):
            result.append((start, end))
            continue
        begin = start
        i = start
        while i < end:
            if text[i] in ".!?" and not (text[i] == "." and i + 1 < end and text[i + 1] == "."):
                prior = re.search(r"([\w]+)$", text[begin:i])
                abbreviation = bool(prior and prior.group(1).casefold() in _ABBREVIATIONS)
                abbreviation |= bool(re.search(r"(?:\b[A-Za-z]\.)(?:[A-Za-z]\.)*[A-Za-z]$", text[begin:i]))
                decimal = i > 0 and i + 1 < end and text[i - 1].isdigit() and text[i + 1].isdigit()
                if not abbreviation and not decimal:
                    finish = i + 1
                    while finish < end and text[finish] in '.!?\"”\'’)]}': finish += 1
                    if finish == end or text[finish].isspace():
                        if text[begin:finish].strip():
                            result.append((begin, finish))
                        begin = finish
                        while begin < end and text[begin].isspace(): begin += 1
                        i = begin
                        continue
            i += 1
        if begin < end and text[begin:end].strip():
            result.append((begin, end))
    return result


def span_alignment(start: int, end: int, words: list[WordAlignment]) -> SpanAlignment:
    included = [w for w in words if start <= w.source_start and w.source_end <= end]
    aligned = [w for w in included if w.audio_start is not None]
    count = len(aligned)
    coverage = count / len(included) if included else 0.0
    return SpanAlignment(source_start=start, source_end=end,
                         audio_start=aligned[0].audio_start if aligned else None,
                         audio_end=aligned[-1].audio_end if aligned else None,
                         matched_words=count, unmatched_words=len(included) - count,
                         coverage=coverage,
                         status="unresolved" if not aligned else "matched" if coverage == 1 else "partial")


def derive_spans(text: str, words: list[WordAlignment]) -> tuple[list[SpanAlignment], list[SpanAlignment]]:
    return ([span_alignment(a, b, words) for a, b in sentence_ranges(text)],
            [span_alignment(a, b, words) for a, b in paragraph_ranges(text)])


def derive_scene_timings(scenes: list[Scene], words: list[WordAlignment]) -> list[SceneTiming]:
    result = []
    for scene in scenes:
        span = span_alignment(scene.start, scene.end, words)
        warnings = []
        if span.unmatched_words:
            warnings.append(f"{span.unmatched_words} manuscript words lack verified timing")
        if span.coverage < 0.5 or span.matched_words == 0:
            warnings.append("Insufficient matched speech; review required")
            span = span.model_copy(update={"audio_start": None, "audio_end": None,
                                           "status": "unresolved"})
        result.append(SceneTiming(**span.model_dump(exclude={"identifier"}), scene_id=scene.identifier,
                                  warnings=warnings))
    return result
