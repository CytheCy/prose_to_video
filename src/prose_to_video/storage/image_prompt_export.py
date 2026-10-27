"""Portable prompt exports; no credentials or image binaries."""
from __future__ import annotations

import json


def prompt_records(scenes, boards, store, *, shot_ids=None, project=None, library=None):
    records = []
    for scene in scenes:
        board = boards.get(scene.identifier)
        if not board: continue
        for shot in board.shots:
            if shot_ids is not None and shot.identifier not in shot_ids: continue
            prompt = store.active(shot.identifier)
            if not prompt: continue
            item = prompt.model_dump(mode='json')
            if project is not None and library is not None:
                issues = store.validate(prompt, scene, shot, board, project, library)
                item['validation_issues'] = issues
                item['review_status'] = store.state(prompt, scene, shot, board, project, library, issues=issues)
            item.update(scene_title=scene.title, shot_title=shot.title,
                audio_start=shot.audio_start, audio_end=shot.audio_end)
            records.append(item)
    return records


def export_prompt_json(scenes, boards, store, *, shot_ids=None, project=None, library=None):
    return json.dumps({'format': 'prose-to-video-image-prompts', 'version': 1,
                       'prompts': prompt_records(scenes, boards, store, shot_ids=shot_ids,
                                                 project=project, library=library)},
                      ensure_ascii=False, indent=2)


def export_prompt_markdown(scenes, boards, store, *, shot_ids=None, project=None, library=None):
    lines = ['# Image prompts', '']
    last_scene = None
    for item in prompt_records(scenes, boards, store, shot_ids=shot_ids,
                               project=project, library=library):
        if item['scene_id'] != last_scene:
            lines += [f"## {item['scene_title']}", '']
            last_scene = item['scene_id']
        times = (f"{item['audio_start']:.2f}–{item['audio_end']:.2f} s" if
                 item['audio_start'] is not None and item['audio_end'] is not None else 'Timing unresolved')
        lines += [f"### {item['shot_title']} · {times}", '',
            f"Version {item['version']} · {item['review_status']} · source {item['source_start']}:{item['source_end']}", '',
            item['positive_prompt'], '']
        if item['negative_prompt']: lines += [f"Negative: {item['negative_prompt']}", '']
        if item.get('validation_issues'):
            lines += [f"Review: {'; '.join(item['validation_issues'])}", '']
    return '\n'.join(lines)
