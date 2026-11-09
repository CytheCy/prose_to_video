"""Order-preserving manuscript/transcript matching with exact source offsets."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from prose_to_video.pipeline.speech_normalization import comparison_key

MATCHING_VERSION = 2

from prose_to_video.models.alignment import (AlignmentIssue, SceneTiming, SpanAlignment,
                                             TextPassage, TranscriptWord, WordAlignment)
from prose_to_video.models.scene import Scene
_MATCH_TOKEN_RE = re.compile(r"\b(?:[A-Za-z]\.){2,}|\b\d+(?:,\d{3})*(?:\.\d+)?\b|\b[\w\u0300-\u036f]+(?:['’][\w\u0300-\u036f]+)*(?![\w\u0300-\u036f])", re.UNICODE)
_SPELLED_YEAR_RE = re.compile(r"\btwenty[- ](?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?\b", re.I)



@dataclass(frozen=True)
class Token:
    text: str
    start: int
    end: int


def normalize(word: str) -> str:
    """Public comparison helper; matching keeps numeric boundaries internally."""
    return comparison_key(word).replace('#', '')


def manuscript_tokens(text: str, *, combine_years: bool = False) -> list[Token]:
    pattern = (re.compile(_SPELLED_YEAR_RE.pattern + "|" + _MATCH_TOKEN_RE.pattern, re.I | re.UNICODE)
               if combine_years else _MATCH_TOKEN_RE)
    return [Token(match.group(), match.start(), match.end()) for match in pattern.finditer(text)]


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


def _group_matches(source, spoken, text, boundaries):
    """Bounded phrase alignment. Joined words share a single verified phrase range."""
    a = [comparison_key(t.text) for t in source]
    b = [comparison_key(w.text) for w in spoken]
    def keys(items, manuscript=False):
        result = {}
        for i in range(len(items)):
            for size in range(1, min(8, len(items)-i)+1):
                if manuscript:
                    first, last = items[i], items[i+size-1]
                    if any(first.start < cut < last.end for cut in boundaries):
                        break
                    if size > 1:
                        gap = text[items[i+size-2].end:last.start]
                        if not re.fullmatch(r"[ \t\-’']*", gap):
                            break
                    value = text[first.start:last.end]
                else:
                    if size > 1 and re.search(r'[.!?,;:]', items[i+size-2].text.rstrip('.')):
                        break
                    value = ' '.join(w.text for w in items[i:i+size])
                result[i, size] = comparison_key(value)
        return result
    ak, bk = keys(source, True), keys(spoken)
    sizes = [(1,1), *[(v,1) for v in range(2,9)], *[(1,v) for v in range(2,9)]]
    def gap(ai, aj, bi, bj):
        n, m = aj-ai, bj-bi
        if not n or not m:
            return []
        if n*m > 600_000 or max(n,m) > 1500:
            return [(ai+i, ai+i+1, bi+j, bi+j+1) for i,j in _align_gap(a[ai:aj], b[bi:bj])]
        scores = [[0]*(m+1) for _ in range(n+1)]
        directions = {}
        for i in range(n+1):
            for j in range(m+1):
                if i: scores[i][j] = scores[i-1][j]-1; directions[i,j] = (1,0)
                if j and (not i or scores[i][j-1]-1 > scores[i][j]):
                    scores[i][j] = scores[i][j-1]-1; directions[i,j] = (0,1)
                for x,y in sizes:
                    if x > i or y > j: continue
                    key = ak.get((ai+i-x,x))
                    if not key or key != bk.get((bi+j-y,y)): continue
                    value = scores[i-x][j-y] + x+y
                    if value > scores[i][j]:
                        scores[i][j] = value; directions[i,j] = (x,y)
        result=[]; i,j=n,m
        while i or j:
            x,y=directions[i,j]
            if x and y: result.append((ai+i-x,ai+i,bi+j-y,bi+j))
            i-=x; j-=y
        return result[::-1]
    result=[]; ai=bi=0
    for aj,bj in [*_anchors(a,b), (len(a),len(b))]:
        result.extend(gap(ai,aj,bi,bj))
        if (aj,bj)!=(len(a),len(b)):
            result.extend((aj+d,aj+d+1,bj+d,bj+d+1) for d in range(4))
        ai,bi=aj+4,bj+4
    return result


def match_words(text: str, spoken: list[TranscriptWord],
                included: list[tuple[int, int]] | None = None,
                boundaries: list[int] | None = None) -> tuple[list[WordAlignment], list[TextPassage], list[AlignmentIssue]]:
    source = manuscript_tokens(text)
    eligible = [t for t in source if included is None or any(a <= t.start and t.end <= b for a,b in included)]
    cuts = set(boundaries or []) | {p for span in included or [] for p in span}
    matches = _group_matches(eligible, spoken, text, cuts)
    by_start = {eligible[a].start: (a,b,c,d) for a,b,c,d in matches}
    covered=set(); matched_spoken=set(); words=[]; passages=[]
    eligible_starts={t.start for t in eligible}
    last=0.0
    for token in source:
        if token.start in covered: continue
        match=by_start.get(token.start)
        if match:
            a,b,c,d=match; tokens=eligible[a:b]; recognized=spoken[c:d]
            covered.update(t.start for t in tokens[1:]); matched_spoken.update(range(c,d))
            start,end=recognized[0].start,recognized[-1].end
            timed=all(w.start is not None and w.end > w.start for w in recognized)
            timed=timed and all(left.end <= right.start+.02 for left,right in zip(recognized,recognized[1:]))
            timed=timed and start+.02 >= last
            if timed: last=max(last,end)
            words.append(WordAlignment(source_start=token.start,source_end=tokens[-1].end,
                text=text[token.start:tokens[-1].end],recognized=' '.join(w.text for w in recognized),
                audio_start=start if timed else None,audio_end=end if timed else None,
                status='matched' if timed else 'unmatched',
                confidence=min((w.score for w in recognized if w.score is not None),default=None)))
            passages.append(TextPassage(source_start=token.start,source_end=tokens[-1].end,
                transcript_start=c,transcript_end=d,matched_words=b-a))
        else:
            words.append(WordAlignment(source_start=token.start,source_end=token.end,text=token.text,
                status='unmatched' if token.start in eligible_starts else 'excluded'))
    issues=[AlignmentIssue(kind='spoken_insertion',message=f'Unmatched speech: {word.text}',
        audio_start=word.start,audio_end=word.end) for j,word in enumerate(spoken) if j not in matched_spoken]
    return words,passages,issues


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
    included = [w for w in words if w.status != "excluded" and start <= w.source_start and w.source_end <= end]
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
