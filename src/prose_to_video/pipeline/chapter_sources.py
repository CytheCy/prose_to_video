"""Resolve selected Start parts to exported, heading-free chapter prose."""
from prose_to_video.utils.prose import read_prose, without_headings


def normalize(text):
    return text.replace('\r\n', '\n').replace('\r', '\n').strip()


def chapter_source(session, section, source_text):
    """Require an unambiguous export matching this part; never read sibling folders."""
    body = source_text[section.body_start:section.end]
    expected = normalize(without_headings(body))
    from podcast_splitter.core import safe_filename
    from prose_to_video.utils.prose import heading_title
    import re
    title_line = source_text[section.heading_start:section.heading_end].splitlines()[0] if section.heading_start is not None else ''
    stem = safe_filename(heading_title(title_line) or section.heading or 'Opening prose')
    output = session.root / 'output' / 'chapters'
    paths = [p for p in sorted(output.glob('*.md'))
             if re.fullmatch(r'(?:\d+ )?' + re.escape(stem) + r'(?: \(\d+\))?', p.stem)]
    matches = []
    for path in paths:
        content = read_prose(path)
        if normalize(without_headings(content)) == expected:
            matches.append((path, content))
    if len(matches) != 1:
        raise ValueError(f'Export the matching Markdown for {section.heading or "Opening prose"} '
                         'in Podcast Splitter first. Expected one matching .md file in '
                         f'{session.root / "output" / "chapters"}.')
    return matches[0]


def analysis_source(session, sections, source_text):
    """Read selected chapter files into source coordinates, blanking everything else.

    Source prose supplies structure and offset validation only. Every non-whitespace
    character sent to analysis comes from a selected chapter export.
    """
    result = [c if c.isspace() else ' ' for c in source_text]
    for section in sections:
        if not section.included or not source_text[section.body_start:section.end].strip():
            continue
        _, content = chapter_source(session, section, source_text)
        content = normalize(without_headings(content))
        # Map normalized newlines back to the original CRLF/CR source coordinates.
        original = source_text[section.body_start:section.end]
        start = section.body_start + len(original) - len(original.lstrip())
        position = start
        for char in content:
            if char == '\n' and source_text[position:position+2] == '\r\n':
                result[position:position+2] = '\r\n'
                position += 2
            else:
                result[position] = '\r' if char == '\n' and source_text[position] == '\r' else char
                position += 1
    return ''.join(result)
