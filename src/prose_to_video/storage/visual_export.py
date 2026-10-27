"""Portable visual continuity exports."""
import json
from pathlib import Path
from prose_to_video.storage.visual_library import VisualLibrary


def export_json(library: VisualLibrary, path: Path):
    Path(path).write_text(json.dumps(library.export_data(), indent=2, ensure_ascii=False), encoding='utf-8')


def export_markdown(library: VisualLibrary, path: Path):
    data = library.export_data()
    lines = ['# Visual continuity guide', '', '## Project style', '']
    for key, value in data['style'].items():
        if value: lines.append(f'- **{key.replace("_", " ").capitalize()}:** {value}')
    for category in ('Character', 'Location', 'Robot', 'Vehicle', 'Prop or object', 'Architectural structure', 'Other'):
        members = [row for row in data['entities'] if row['entity']['entity_type'] == category]
        if not members: continue
        lines.extend(['', f'## {category}', ''])
        for row in members:
            e = row['entity']
            lines.extend([f'### {e["name"]}', '', f'ID: `{e["identifier"]}`  Status: {e["review_status"]}', ''])
            for field in ('aliases', 'description', 'appearance_description', 'distinguishing_features',
                          'colors_materials', 'size_proportions', 'clothing_accessories'):
                value = e[field]
                if value: lines.append(f'- **{field.replace("_", " ").capitalize()}:** {", ".join(value) if isinstance(value, list) else value}')
            lines.append(f'- **Scenes:** {", ".join(row["scene_ids"]) or "None"}')
            lines.append(f'- **Shots:** {", ".join(s["shot_id"] for s in row["shots"]) or "None"}')
            for variant in row['variants']:
                lines.append(f'- **Variant {variant["name"]}:** {variant["description"]} [{variant["status"]}]')
            for reference in row['references']:
                lines.append(f'- **Reference {reference["link"]["role"]}:** `{reference["asset"]["path"]}` [{reference["link"]["status"]}]')
            lines.append('')
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def export_style(library: VisualLibrary, path: Path):
    Path(path).write_text(library.style().model_dump_json(indent=2), encoding='utf-8')
