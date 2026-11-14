from pathlib import Path

import pytest

from podcast_splitter.core import Project, Section, export_names, match_sections, parse_prose, tokens, validate_export


def timed(text, offset=0):
    return [{"word": w, "start": offset + i * .4, "end": offset + i * .4 + .3} for i, w in enumerate(text.split())]


def test_prose_excludes_editorial_and_code(tmp_path):
    p = tmp_path / "prose.md"
    p.write_text("# Change\nFix this.\n# 01 One\nHello.\n```\n# not a heading\n```\n## 02 Two\nWorld.\n# Empty\n")
    s = parse_prose(str(p))
    assert [x.title for x in s] == ["Change", "01 One", "02 Two", "Empty"]
    assert [x.included for x in s] == [False, True, True, False]
    assert s[1].text == "Hello."


def test_match_ignores_intro_and_outro_and_preserves_chapter_order():
    one = "The little boat crossed the blue ocean toward the distant island under a cloudless sky"
    two = "Marvin walked into the kitchen carrying a strange device and put it inside the drawer"
    words = timed("Welcome to our show enjoy the episode", 0) + timed(one, 12) + timed(two, 25) + timed("Thanks for listening subscribe today", 45)
    sections = [Section("One", one), Section("Two", two)]
    match_sections(sections, words, 60)
    assert sections[0].start == pytest.approx(11.9)
    assert sections[1].end < 40
    assert all(s.status == "Matched" for s in sections)
    validate_export(Project(duration=60, sections=sections))


def test_missing_section_is_flagged_not_fabricated():
    sections = [Section("Absent", "A completely missing scene about intricate handmade footwear")]
    match_sections(sections, timed("The fisherman caught a large fish in the river"), 20)
    assert sections[0].start is None
    with pytest.raises(ValueError, match="no cut points"):
        validate_export(Project(duration=20, sections=sections))


def test_imperfect_speech_and_contractions():
    text = "He doesn’t know why the unusual old building across the busy street has no sign"
    heard = "He doesn't know why the unusual old building across a busy street has no sign"
    sections = [Section("One", text)]
    match_sections(sections, timed(heard, 4), 20)
    assert sections[0].start == pytest.approx(3.9)
    assert sections[0].confidence > .85


def test_weak_edges_require_approval():
    text = "completely omitted opening words before the beautiful green garden behind the old house in the countryside"
    sections = [Section("One", text)]
    match_sections(sections, timed("the beautiful green garden behind the old house in the countryside", 5), 20)
    assert sections[0].start is not None
    with pytest.raises(ValueError, match="Review and approve"):
        validate_export(Project(duration=20, sections=sections))
    sections[0].reviewed = True
    validate_export(Project(duration=20, sections=sections))


def test_name_spelling_at_start_does_not_clip_first_word():
    sections = [Section("One", "Frolik watches a child walk across the busy street and enter the shop")]
    match_sections(sections, timed("Frolic watches a child walk across the busy street and enter the shop", 5), 20)
    assert sections[0].start == pytest.approx(4.9)
    assert sections[0].status == "Matched"


@pytest.mark.parametrize("start,end", [(float("nan"), 5), (1, float("inf")), (-1, 5), (5, 1), (1, 11)])
def test_invalid_cuts_rejected(start, end):
    with pytest.raises(ValueError):
        validate_export(Project(duration=10, sections=[Section("One", "", start=start, end=end, reviewed=True)]))


def test_overlap_rejected():
    sections = [Section("One", "", start=1, end=5, reviewed=True), Section("Two", "", start=4, end=8, reviewed=True)]
    with pytest.raises(ValueError, match="overlaps"):
        validate_export(Project(duration=10, sections=sections))


def test_names_are_safe_and_unique():
    names = export_names([Section("01 Bad/name", ""), Section("01 Bad/name", ""), Section("No number", "")], "mp3")
    assert names == ["01 Bad_name.mp3", "01 Bad_name (2).mp3", "03 No number.mp3"]


def test_project_roundtrip(tmp_path):
    path = str(tmp_path / "project.json")
    p = Project(audio="some file.mp3", duration=20, sections=[Section("Chapter", "text", start=1, end=8, reviewed=True)])
    p.save(path)
    assert Project.load(path) == p


def test_project_widening_roundtrip_and_source_limits(tmp_path):
    sections = [Section("One", "", start=.2, end=5, reviewed=True),
                Section("Two", "", start=5, end=9.8, reviewed=True),
                Section("Missing", "", included=False)]
    p = Project(duration=10, sections=sections, part_padding=.5)
    assert p.cut_points(sections[0]) == (0, 5.5)
    assert p.cut_points(sections[1]) == (4.5, 10)
    assert p.cut_points(sections[2]) == (None, None)
    assert validate_export(p) == sections[:2]  # Shared padding is allowed.
    path = str(tmp_path / "project.json")
    p.save(path)
    assert Project.load(path) == p
    p.part_padding = 0
    assert p.cut_points(sections[0]) == (.2, 5)


@pytest.mark.parametrize("padding", [-1, float("nan"), float("inf")])
def test_invalid_widening_rejected(tmp_path, padding):
    p = Project(duration=10, part_padding=padding,
                sections=[Section("One", "", start=1, end=5, reviewed=True)])
    with pytest.raises(ValueError, match="widening"):
        validate_export(p)
    path = str(tmp_path / "project.json")
    p.save(path)
    with pytest.raises(ValueError, match="widening"):
        Project.load(path)


def test_old_project_defaults_to_no_widening(tmp_path):
    import json
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"version": 1, "duration": 10, "sections": []}))
    assert Project.load(str(path)).part_padding == 0
