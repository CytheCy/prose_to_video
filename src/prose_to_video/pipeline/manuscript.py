"""Source-preserving Markdown structure and cautious preproduction suggestions.

All ranges are half-open Python character offsets into the unchanged UTF-8 file.
Suggestions are deliberately reviewable; they are not manuscript facts.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from prose_to_video.utils.prose import WORD_RE

CLASSIFICATIONS = ("Narrated Story", "Heading or Chapter Label", "Editorial Note",
                   "Excluded from Production", "Requires Review")
FORMATS = ("dramatic", "dialogue", "radio", "television", "advertisement", "news",
           "exposition", "historical summary", "memory", "video call", "surveillance",
           "screen interface", "internal reflection", "action", "montage")
VIEWPOINTS = ("direct", "radio", "television", "video call", "surveillance", "drone",
              "recorded footage", "screen interface", "illustrative")
_ATX = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_SETEXT = re.compile(r"^[ \t]{0,3}(=+|-+)[ \t]*$")
_NUMBERED = re.compile(r"^(\d+)(?:[.):-]|[ \t]+)[ \t]*(.+)$")
_YEAR = re.compile(r"\b(?:19|20|21)\d{2}\b")
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
         "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
         "six": 6, "seven": 7, "eight": 8, "nine": 9}
_SPELLED_YEAR = re.compile(r"\b(twenty)[- ](twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[- ](one|two|three|four|five|six|seven|eight|nine))?\b", re.I)
_AGE_WORD = r"(?:\d{1,3}|(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?)"
_AGE_CLAIM = re.compile(rf"\b(?:at\s+(?P<at>{_AGE_WORD})\s*,\s*(?P<at_name>[A-Z][a-z]+)|"
    rf"(?P<name>[A-Z][a-z]+)\s+(?:is|was)\s+(?P<is>{_AGE_WORD})|"
    rf"(?P<adjective>{_AGE_WORD})[- ]year[- ]old\s+(?P<adj_name>[A-Z][a-z]+))\b", re.I)
_EDITORIAL = re.compile(r"\b(?:revise|revision|remove|delete|change|replace|rewrite|draft|todo|editorial|note to|insert|cut this)\b", re.I)
_DECORATION = re.compile(r"^[\s.*_—–-]+$")


@dataclass(frozen=True)
class ManuscriptSection:
    identifier: str
    start: int
    end: int
    heading_start: int | None
    heading_end: int | None
    body_start: int
    heading: str
    number: int | None
    level: int | None
    classification: str
    included: bool
    review: bool
    formats: tuple[str, ...]
    viewpoint: str
    explicit_years: tuple[int, ...]
    rationale: str

    def source(self, text: str) -> str:
        return text[self.start:self.end]

    def narration_range(self) -> tuple[int, int] | None:
        return (self.body_start, self.end) if self.included and self.body_start < self.end else None


def _headings(text: str) -> list[tuple[int, int, str, int]]:
    lines = list(re.finditer(r"[^\r\n]*(?:\r\n|\n|\r|$)", text))
    result = []
    fenced = False
    for i, line in enumerate(lines):
        raw = line.group().rstrip("\r\n")
        if line.start() == 0 and raw.startswith("\ufeff"):
            raw = raw[1:]
        if re.match(r"^[ \t]{0,3}(```|~~~)", raw):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _ATX.match(raw)
        if match:
            result.append((line.start(), line.end(), match.group(2).strip(), len(match.group(1))))
        elif i and _SETEXT.match(raw) and lines[i - 1].group().strip() and not _ATX.match(lines[i - 1].group().rstrip("\r\n")):
            previous = lines[i - 1]
            result.append((previous.start(), line.end(), previous.group().strip(), 1 if raw.lstrip().startswith("=") else 2))
    return result


def _format(body: str, heading: str) -> tuple[tuple[str, ...], str]:
    sample = (heading + "\n" + body).casefold()
    found = []
    def add(name: str, pattern: str) -> None:
        if re.search(pattern, sample): found.append(name)
    add("advertisement", r"\b(?:advertisement|commercial|advert|product plug|our founder|our (?:shoes|products))\b")
    add("radio", r"\b(?:radio (?:plays|show|host)|call-in show|on the radio|tuned in)\b")
    add("television", r"\b(?:on (?:the )?(?:tv|television|news)|television report|newscast)\b")
    add("news", r"\b(?:news report|law enforcement reports|news presenter|weather forecast|on the news)\b")
    add("surveillance", r"\b(?:surveillance|live stream|camera feed|cam footage|street cam|public cams|drone live feed)\b")
    add("drone", r"\b(?:drone (?:camera|live feed)|controls? a camera on the drone)\b")
    add("recorded footage", r"\b(?:cam recording|recorded footage|video (?:pops up|recording))\b")
    add("video call", r"\b(?:video (?:call|chat|talk)|live video chat)\b")
    add("screen interface", r"\b(?:tablet|laptop|screen|app|interface|map shows|notification)\b")
    add("exposition", r"\b(?:technology|infrastructure|network|artificial intelligence|a\.i\.|scientific|data centers)\b")
    add("memory", r"\b(?:remembers? when|recalled?|flashback|when .{1,30} was a kid|years ago)\b")
    add("internal reflection", r"\b(?:he wonders|she wonders|he thinks|she thinks|how would|is he just|what if)\b")
    add("action", r"\b(?:sprints?|runs? around|escapes?|chases?|fights?)\b")
    if len(re.findall(r"[“\"]", body)) >= 8: found.append("dialogue")
    if not found: found.append("dramatic")
    viewpoint = next((v for marker, v in (("video call", "video call"), ("drone", "drone"),
        ("surveillance", "surveillance"), ("radio", "radio"), ("television", "television"),
        ("recorded footage", "recorded footage"), ("screen interface", "screen interface")) if marker in found), "direct")
    return tuple(dict.fromkeys(found)), viewpoint


def classify_narrative(text: str, heading: str = "") -> tuple[tuple[str, ...], str]:
    """Suggest local scene format without treating a whole section as one scene."""
    return _format(text, heading)


def parse_manuscript(text: str) -> list[ManuscriptSection]:
    if not text: return []
    headings = _headings(text)
    boundaries = [(0, 0, "", None)] if not headings or headings[0][0] else []
    boundaries += [(a, b, h, level) for a, b, h, level in headings]
    sections = []
    for index, (start, heading_end, heading, level) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(text)
        body = text[heading_end:end]
        number_match = _NUMBERED.match(heading)
        number = int(number_match.group(1)) if number_match else None
        title = number_match.group(2) if number_match else heading
        compact = body.strip()
        editorial = bool(_EDITORIAL.search(title)) and (bool(_EDITORIAL.search(compact[:500])) or not compact)
        label = not compact or bool(_DECORATION.fullmatch(compact))
        if editorial: classification, included, review, reason = "Editorial Note", False, True, "Possible revision instruction"
        elif label: classification, included, review, reason = "Heading or Chapter Label", False, False, "No narrative body"
        elif not heading and _EDITORIAL.search(compact[:200]):
            classification, included, review, reason = "Requires Review", False, True, "Unheaded possible instruction"
        else: classification, included, review, reason = "Narrated Story", True, False, "Story content suggested"
        formats, viewpoint = _format(body, title)
        years = tuple(dict.fromkeys([*(int(m.group()) for m in _YEAR.finditer(body)),
            *(2000 + _TENS[m.group(2).lower()] + _ONES.get((m.group(3) or "").lower(), 0)
              for m in _SPELLED_YEAR.finditer(body))]))
        identity = hashlib.sha256((str(level) + "\0" + heading + "\0" + body[:120]).encode()).hexdigest()[:20]
        sections.append(ManuscriptSection(identity, start, end, start if level else None,
            heading_end if level else None, heading_end, title, number, level,
            classification, included, review, formats, viewpoint, years, reason))
    return sections


def apply_decisions(sections: list[ManuscriptSection], decisions: dict[str, dict]) -> list[ManuscriptSection]:
    result = []
    for section in sections:
        decision = decisions.get(section.identifier, {})
        classification = decision.get("classification", section.classification)
        if classification not in CLASSIFICATIONS: raise ValueError("Invalid section classification")
        result.append(replace(section, classification=classification,
            included=bool(decision.get("included", section.included)),
            review=bool(decision.get("review", section.review)),
            rationale="User decision" if decision else section.rationale))
    return result


def included_ranges(sections: list[ManuscriptSection]) -> list[tuple[int, int]]:
    return [span for section in sections if (span := section.narration_range())]


def inclusion_fingerprint(sections: list[ManuscriptSection]) -> str:
    payload = [(s.identifier, s.included, s.body_start, s.end) for s in sections]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def scenes_match_inclusion(scenes, sections: list[ManuscriptSection]) -> bool:
    """Detect decisions that require scene boundary reconciliation."""
    if not scenes: return False
    starts = {scene.start for scene in scenes}
    transitions = {sections[i].start for i in range(1, len(sections))
                   if sections[i].included != sections[i - 1].included
                   and sections[i - 1].body_start < sections[i - 1].end}
    if not transitions <= starts: return False
    for scene in scenes:
        expected = any(s.included and s.body_start < scene.end and s.end > scene.start
                       for s in sections)
        if scene.production_included != expected: return False
    return True


def chronology_claims(text: str, sections: list[ManuscriptSection]) -> list[dict]:
    claims = []
    for section in sections:
        # A dated heading is evidence for the section even when the body omits
        # the date. Multiple years in a section remain uncertain.
        heading_years = [int(m.group()) for m in _YEAR.finditer(section.heading)]
        year = section.explicit_years[0] if section.explicit_years else heading_years[0] if heading_years else None
        for match in _AGE_CLAIM.finditer(text, section.body_start, section.end):
            word = match.group("at") or match.group("is") or match.group("adjective")
            name = match.group("at_name") or match.group("name") or match.group("adj_name")
            if name.casefold() in {"year", "she", "he", "it", "they", "this", "that"}:
                continue
            if word.isdigit(): age = int(word)
            else:
                bits = re.split(r"[- ]", word.lower())
                age = _TENS.get(bits[0], 0) + (_ONES.get(bits[1], 0) if len(bits) > 1 else 0)
            if age <= 0 or age > 110: continue
            claims.append({"character": name, "age": age, "year": year,
                "source_start": match.start(), "source_end": match.end(),
                "quote": text[match.start():match.end()], "section_id": section.identifier,
                "confidence": "explicit age; inferred section year" if year else "explicit age; year unknown"})
    return claims


def chronology_conflicts(claims: list[dict], tolerance: int = 2) -> list[dict]:
    issues = []
    dated = [claim for claim in claims if claim["year"] is not None]
    for i, first in enumerate(dated):
        for second in dated[i + 1:]:
            if first["character"].casefold() != second["character"].casefold() or first["year"] == second["year"]:
                continue
            age_difference = second["age"] - first["age"]
            year_difference = second["year"] - first["year"]
            if abs(age_difference - year_difference) > tolerance:
                issues.append({"kind": "age_year_conflict", "character": first["character"],
                    "claims": [first, second], "expected_age_difference": year_difference,
                    "stated_age_difference": age_difference,
                    "impact": "Review period-specific appearance before approving character imagery"})
    return issues


def chronology_timeline(text: str, sections: list[ManuscriptSection]) -> list[dict]:
    """Keep presentation position separate from supported story-year ordering."""
    events = []
    for index, section in enumerate(sections, 1):
        heading_text = text[section.heading_start:section.heading_end] if section.heading_start is not None else ""
        heading_match = _YEAR.search(heading_text)
        opening = text[section.body_start:min(section.end, section.body_start + 150)]
        opening_match = re.search(r"\b(?:the year is|in the year)\s+((?:19|20|21)\d{2}|twenty[- ](?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?)", opening, re.I)
        if opening_match:
            raw = opening_match.group(1)
            year = int(raw) if raw.isdigit() else None
            if year is None:
                parts = re.split(r"[- ]", raw.casefold())
                year = 2000 + _TENS[parts[1]] + (_ONES.get(parts[2], 0) if len(parts) > 2 else 0)
            evidence_start = section.body_start + opening_match.start(1)
            evidence_end = section.body_start + opening_match.end(1)
            confidence = "explicit narration year"
        elif heading_match:
            year = int(heading_match.group())
            evidence_start = section.heading_start + heading_match.start()
            evidence_end = section.heading_start + heading_match.end()
            confidence = "heading year; review narration context"
        else:
            year = evidence_start = evidence_end = None
            confidence = "undated"
        events.append({"section_id": section.identifier, "presentation_order": index,
            "year": year, "source_start": evidence_start, "source_end": evidence_end,
            "confidence": confidence, "time_jump": False, "chronological_rank": None})
    dated = sorted((event for event in events if event["year"] is not None),
                   key=lambda e: (e["year"], e["presentation_order"]))
    for rank, event in enumerate(dated, 1): event["chronological_rank"] = rank
    prior = None
    for event in events:
        if event["year"] is not None:
            event["time_jump"] = prior is not None and event["year"] != prior
            prior = event["year"]
    return events


def recurring_candidates(text: str, sections: list[ManuscriptSection]) -> dict[str, list[dict]]:
    """Lexical leads for review; repetition alone never establishes identity."""
    character_pattern = re.compile(r"\b([A-Z][a-z]{2,})\s+(?:says?|said|asks?|asked|walks?|watches?|looks?|sits?|stands?|runs?|is|was|has|had)\b")
    location_pattern = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b")
    object_pattern = re.compile(r"\b(?:a|an|the|his|her)\s+([a-z]{4,})\b", re.I)
    location_endings = {"shoes", "factory", "building", "tavern", "bar", "house", "office",
                        "station", "school", "restaurant", "parlor", "store", "warehouse"}
    excluded_objects = {"that", "this", "there", "same", "other", "time", "year", "next",
                        "first", "last", "thing", "people", "woman", "man", "girl", "boy",
                        "back", "building", "couple", "door", "face", "front", "head", "house",
                        "street", "chest", "desk", "evening", "eyes", "floor", "hand", "left",
                        "room", "way", "place", "side", "life", "day", "night", "work", "car",
                        "parking", "problem", "public", "road", "roof", "voice", "alley",
                        "counter", "deep", "doors", "fast", "hour", "internet", "office",
                        "pocket", "receptionist", "size", "third", "wall", "window", "world",
                        "video", "front", "north", "south", "woman", "person", "company"}
    buckets: dict[str, dict[str, dict]] = {"characters": {}, "locations": {}, "objects": {}}
    for section in sections:
        if not section.included: continue
        body = text[section.body_start:section.end]
        candidates = [
            ("characters", m.group(1), m.start(1), m.end(1)) for m in character_pattern.finditer(body)
            if m.group(1).casefold() not in {"the", "she", "he", "his", "her", "this", "that", "when", "with", "after", "before", "what", "why", "you", "there", "how", "where", "who", "they"}]
        candidates += [("characters", c["character"],
                        c["source_start"] + c["quote"].casefold().rfind(c["character"].casefold()) - section.body_start,
                        c["source_start"] + c["quote"].casefold().rfind(c["character"].casefold()) + len(c["character"]) - section.body_start)
                       for c in chronology_claims(text, [section])]
        candidates += [("locations", m.group(1), m.start(1), m.end(1)) for m in location_pattern.finditer(body)
                       if m.group(1).split()[-1].casefold() in location_endings]
        if section.heading_start is not None:
            heading_offset = text.find(section.heading, section.heading_start, section.heading_end)
            candidates += [("locations", m.group(1),
                            heading_offset + m.start(1) - section.body_start,
                            heading_offset + m.end(1) - section.body_start)
                           for m in location_pattern.finditer(section.heading)
                           if heading_offset >= 0 and m.group(1).split()[-1].casefold() in location_endings]
        candidates += [("objects", m.group(1), m.start(1), m.end(1)) for m in object_pattern.finditer(body)
                       if m.group(1).casefold() not in excluded_objects]
        for kind, value, start, end in candidates:
            key = value.casefold().rstrip("s") if kind == "objects" else value.casefold()
            item = buckets[kind].setdefault(key, {"name": value, "section_ids": set(), "source_ranges": []})
            item["section_ids"].add(section.identifier)
            if len(item["source_ranges"]) < 8:
                item["source_ranges"].append([section.body_start + start, section.body_start + end])
    return {kind: [{"name": item["name"], "section_count": len(item["section_ids"]),
                   "source_ranges": item["source_ranges"], "status": "lexical candidate; review identity"}
                  for item in sorted(items.values(), key=lambda x: (-len(x["section_ids"]), x["name"]))
                  if len(item["section_ids"]) >= 2][:30]
            for kind, items in buckets.items()}


def analysis_report(text: str, sections: list[ManuscriptSection]) -> dict:
    years = sorted({year for s in sections for year in s.explicit_years})
    claims = chronology_claims(text, sections)
    recurring = recurring_candidates(text, sections)
    included_words = sum(len(WORD_RE.findall(text[a:b])) for a, b in included_ranges(sections))
    return {"source_sha256": hashlib.sha256(text.encode()).hexdigest(), "characters": len(text),
        "words": len(WORD_RE.findall(text)), "sections": [asdict(s) for s in sections],
        "section_count": len(sections), "candidate_scenes": sum(s.included for s in sections),
        "formats": sorted({f for s in sections for f in s.formats}), "explicit_years": years,
        "chronology_claims": claims, "chronology_conflicts": chronology_conflicts(claims),
        "chronology": chronology_timeline(text, sections),
        "recurring_characters": recurring["characters"],
        "recurring_locations": recurring["locations"],
        "recurring_objects": recurring["objects"],
        "review_count": sum(s.review for s in sections),
        "excluded_count": sum(not s.included for s in sections),
        "expected_storyboard_workload": {"included_words": included_words,
            "rough_shots_low": max(1, round(included_words / 45)) if included_words else 0,
            "rough_shots_high": max(1, round(included_words / 12)) if included_words else 0,
            "basis": "Word-count planning range only; actual shot count and timing depend on narration and selected style"},
        "alignment_difficulties": ["Headings and excluded sections may be unspoken",
            "Numbers, acronyms, and repeated ellipses require narration review"],
        "interpretation_notice": "Classifications and scene counts are suggestions; source text is authoritative."}


def report_markdown(report: dict) -> str:
    lines = ["# Manuscript analysis", "", report["interpretation_notice"], "",
        f"Characters: {report['characters']:,}; words: {report['words']:,}; sections: {report['section_count']}",
        f"Candidate sections for scenes: {report['candidate_scenes']}; review: {report['review_count']}",
        f"Rough shot workload: {report['expected_storyboard_workload']['rough_shots_low']}–"
        f"{report['expected_storyboard_workload']['rough_shots_high']} ({report['expected_storyboard_workload']['basis']})",
        "", "## Sections", ""]
    for section in report["sections"]:
        lines.append(f"- {section['heading'] or '(Preamble)'} [{section['start']}, {section['end']}): "
            f"{section['classification']}; {'included' if section['included'] else 'excluded'}; "
            f"formats: {', '.join(section['formats'])}")
    lines.extend(["", "## Chronology", ""])
    for event in report["chronology"]:
        if event["year"] is not None:
            lines.append(f"- Presentation {event['presentation_order']}; story year {event['year']}; "
                f"story rank {event['chronological_rank']}; source {event['source_start']}:{event['source_end']}; "
                f"{event['confidence']}")
    lines.extend(["", "## Recurring lexical candidates", "",
                  "These repeated names and words need identity review before becoming continuity facts.", ""])
    for key, label in (("recurring_characters", "Characters"),
                       ("recurring_locations", "Locations"), ("recurring_objects", "Objects")):
        items = report[key]
        lines.append(f"- {label}: {', '.join(item['name'] for item in items) if items else 'none detected'}")
    if report["chronology_conflicts"]:
        lines.extend(["", "## Chronology warnings", ""])
        for issue in report["chronology_conflicts"]:
            first, second = issue["claims"]
            lines.append(f"- {issue['character']}: {first['quote']} ({first['year']}, {first['source_start']}:{first['source_end']}) "
                f"versus {second['quote']} ({second['year']}, {second['source_start']}:{second['source_end']}). "
                f"{issue['impact']}.")
    return "\n".join(lines) + "\n"


def export_report(path: Path, report: dict) -> None:
    path.write_text(report_markdown(report) if path.suffix.lower() == ".md" else
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
