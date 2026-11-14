"""Pure data and matching logic; no GUI or speech runtime imports."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
import hashlib
import json
import math
import re
import unicodedata


@dataclass
class Section:
    title: str
    text: str
    included: bool = True
    start: float | None = None
    end: float | None = None
    confidence: float = 0.0
    status: str = "Not analyzed"
    reviewed: bool = False


@dataclass
class Project:
    prose: str = ""
    audio: str = ""
    output: str = ""
    duration: float = 0.0
    sections: list[Section] = field(default_factory=list)
    waveform: list[float] = field(default_factory=list)
    model: str = "base.en"
    export_format: str = "MP3"
    audio_fingerprint: str = ""
    prose_fingerprint: str = ""

    part_padding: float = 0.0

    def cut_points(self, section: Section) -> tuple[float | None, float | None]:
        """Project-wide extra audio per end, without changing the original cuts."""
        start = max(0., section.start - self.part_padding) if section.start is not None else None
        end = min(self.duration, section.end + self.part_padding) if section.end is not None else None
        return start, end

    def validate_padding(self):
        if not math.isfinite(self.part_padding) or self.part_padding < 0:
            raise ValueError("Part widening must be a finite, non-negative number of seconds.")

    def save(self, path: str):
        target = Path(path)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(json.dumps({"version": 1, **asdict(self)}, indent=2), encoding="utf-8")
        temp.replace(target)

    @classmethod
    def load(cls, path: str) -> Project:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.pop("version", None) != 1:
            raise ValueError("Unsupported project version")
        # Accept projects from 0.1.2, preserving their actual edited cut points.
        data["sections"] = [Section(**{k: v for k, v in s.items() if k != "allow_overlap"})
                            for s in data["sections"]]
        p = cls(**data)
        p.validate_padding()
        if not math.isfinite(p.duration) or p.duration < 0:
            raise ValueError("Invalid project duration")
        for s in p.sections:
            for value in (s.start, s.end):
                if value is not None and (not math.isfinite(value) or value < 0 or value > p.duration):
                    raise ValueError("Invalid chapter timestamp in project")
        return p


def fingerprint(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_prose(path: str) -> list[Section]:
    text = Path(path).read_text(encoding="utf-8-sig")
    sections = []
    title, lines = None, []
    fenced = False
    fence = ""
    def finish():
        if title is not None:
            body = "\n".join(lines).strip()
            editorial = bool(re.match(r"^(change[s]?|notes?|todo|editing notes)\b", title, re.I))
            sections.append(Section(title, body, bool(body) and not editorial))
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if not fenced:
                fenced, fence = True, marker[1][0]
            elif marker[1][0] == fence:
                fenced = False
            continue
        if fenced:
            continue
        heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            finish()
            title, lines = heading[1].strip(), []
        elif title is not None:
            lines.append(line)
    finish()
    if not sections:
        raise ValueError("No Markdown headings found. Use headings such as '# 01 Chapter title'.")
    return sections


def tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text).casefold().replace("’", "'")
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.findall(r"[^\W_]+", text.replace("'", ""), re.UNICODE)


def match_sections(sections: list[Section], words: list[dict], duration: float,
                   silence: list[tuple[float, float]] | None = None) -> None:
    """Find ordered anchors, then locally align each section to timed ASR words.

    Never allocate an estimated time to a section with no textual evidence.
    Endpoints are separate so unmatched audio between chapters can be discarded.
    """
    transcript, timing = [], []
    for word in words:
        for token in tokens(word["word"]):
            transcript.append(token)
            timing.append((float(word["start"]), float(word["end"])))
    if not transcript:
        raise ValueError("No speech was recognized. Check the audio or choose a larger model.")
    index = defaultdict(list)
    for i in range(len(transcript) - 2):
        index[tuple(transcript[i:i + 3])].append(i)
    cursor = 0
    accepted = []
    for section in sections:
        if not section.included:
            continue
        section.start = section.end = None
        section.reviewed = False
        section.confidence = 0.0
        source = tokens(section.text)
        if len(source) < 3:
            section.status = "Needs review: too little text"
            continue
        votes = Counter()
        # Vote for the chapter's approximate offset using distributed distinctive phrases.
        for j in range(len(source) - 2):
            hits = index.get(tuple(source[j:j + 3]), [])
            if len(hits) > 12:
                continue
            for pos in hits:
                if pos >= cursor:
                    votes[round((pos - j) / 12)] += 1
        if not votes:
            section.status = "Needs review: no match"
            continue
        candidates = []
        for bucket, _ in votes.most_common(8):
            center = bucket * 12
            lo = max(cursor, center - max(60, len(source) // 4))
            hi = min(len(transcript), center + len(source) + max(90, len(source) // 3))
            matcher = SequenceMatcher(None, source, transcript[lo:hi], autojunk=False)
            blocks = [b for b in matcher.get_matching_blocks() if b.size >= 2]
            if not blocks:
                continue
            covered = sum(b.size for b in blocks)
            score = covered / len(source)
            candidates.append((score, lo, blocks))
        if not candidates:
            section.status = "Needs review: no reliable match"
            continue
        score, lo, blocks = max(candidates, key=lambda c: c[0])
        first, last = blocks[0], blocks[-1]
        # ASR commonly spells character names differently (Jeanie / Jeannie).
        # Extend an edge only when adjacent words have strong spelling evidence.
        if 0 < first.a <= 3 and first.b >= first.a:
            before = transcript[lo + first.b - first.a:lo + first.b]
            if all(SequenceMatcher(None, a, b).ratio() >= .75 for a, b in zip(source[:first.a], before)):
                first = first._replace(a=0, b=first.b - first.a, size=first.size + first.a)
        trailing = len(source) - (last.a + last.size)
        if 0 < trailing <= 3:
            after = transcript[lo + last.b + last.size:lo + last.b + last.size + trailing]
            if len(after) == trailing and all(SequenceMatcher(None, a, b).ratio() >= .75 for a, b in zip(source[-trailing:], after)):
                last = last._replace(size=last.size + trailing)
        # Only accept automatic endpoints when both edges are actually represented.
        edge_ok = first.a == 0 and last.a + last.size == len(source)
        start_i, end_i = lo + first.b, lo + last.b + last.size - 1
        section.confidence = round(score, 3)
        if score < .48:
            section.status = "Needs review: weak match"
            continue
        section.start = max(0., timing[start_i][0] - .10)
        section.end = min(duration, timing[end_i][1] + .15)
        # Snap only within a short distance of a matched edge, never across narration.
        for a, b in silence or []:
            midpoint = (a + b) / 2
            if a <= timing[start_i][0] <= b + .15 and abs(midpoint - section.start) < .35:
                section.start = max(0., midpoint)
            if a - .15 <= timing[end_i][1] <= b and abs(midpoint - section.end) < .35:
                section.end = min(duration, midpoint)
        section.status = "Matched" if score >= .85 and edge_ok else "Needs review: check edges/text"
        accepted.append(section)
        cursor = end_i + 1
    for prev, nxt in zip(accepted, accepted[1:]):
        if prev.end > nxt.start:
            split = (prev.end + nxt.start) / 2
            prev.end = nxt.start = split


def validate_export(project: Project) -> list[Section]:
    project.validate_padding()
    chosen = [s for s in project.sections if s.included]
    if not chosen:
        raise ValueError("Select at least one section to export.")
    previous = 0.
    for s in chosen:
        if s.start is None or s.end is None:
            raise ValueError(f"'{s.title}' has no cut points. Set its start/end or exclude it.")
        if not all(math.isfinite(x) for x in (s.start, s.end)) or not 0 <= s.start < s.end <= project.duration:
            raise ValueError(f"Invalid start/end for '{s.title}'.")
        if s.start < previous - .001:
            raise ValueError(f"'{s.title}' overlaps the previous section.")
        if s.status != "Matched" and not s.reviewed:
            raise ValueError(f"Review and approve '{s.title}' before exporting, or exclude it.")
        previous = s.end
    return chosen


def safe_filename(title: str) -> str:
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    return title.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .") or "Untitled"


def export_names(sections: list[Section], extension: str) -> list[str]:
    result, used = [], set()
    for i, s in enumerate(sections, 1):
        name = safe_filename(s.title)
        if not re.match(r"^\d+\b", name):
            name = f"{i:02d} {name}"
        base, suffix = name, 2
        while name.casefold() in used:
            name = f"{base} ({suffix})"
            suffix += 1
        used.add(name.casefold())
        result.append(f"{name}.{extension}")
    return result
