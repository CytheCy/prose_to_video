"""Transactional prompt versions, validation, and generation-ready packages."""
from __future__ import annotations

import sqlite3
import re
from uuid import uuid4
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from prose_to_video.models.image_prompt import ImagePrompt, PromptSettings
from prose_to_video.pipeline.image_prompt_generation import context_for_shot
from prose_to_video.storage.database import ProjectDatabase
from prose_to_video.storage.visual_library import VisualLibrary
from prose_to_video.models.scene import source_hash
from prose_to_video.utils.prose import read_prose


class PromptStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.db = ProjectDatabase(self.root / 'project.sqlite')

    def settings(self):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            row = con.execute('SELECT data_json FROM prompt_settings WHERE singleton=1').fetchone()
            return PromptSettings.model_validate_json(row[0]) if row else PromptSettings()

    def save_settings(self, settings: PromptSettings):
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('INSERT OR REPLACE INTO prompt_settings VALUES(1,?)', (settings.model_dump_json(),))

    def versions(self, shot_id):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            return [ImagePrompt.model_validate_json(r[0]) for r in con.execute(
                'SELECT data_json FROM image_prompt WHERE shot_id=? ORDER BY version DESC', (shot_id,))]

    def latest(self, shot_id):
        versions = self.versions(shot_id)
        return versions[0] if versions else None

    def active(self, shot_id):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            row = con.execute('SELECT p.data_json FROM image_prompt p JOIN prompt_active a ON a.prompt_id=p.identifier '
                              'WHERE a.shot_id=?', (shot_id,)).fetchone()
            return ImagePrompt.model_validate_json(row[0]) if row else None

    def approved(self, shot_id):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            row = con.execute('SELECT p.data_json FROM image_prompt p JOIN prompt_approved a ON a.prompt_id=p.identifier '
                              'WHERE a.shot_id=?', (shot_id,)).fetchone()
            return ImagePrompt.model_validate_json(row[0]) if row else None

    def save(self, prompt: ImagePrompt, *, activate=True):
        prompt = ImagePrompt.model_validate(prompt.model_dump())
        board = self.db.load_storyboard(prompt.scene_id)
        if not board or not any(s.identifier == prompt.shot_id and
                                (s.source_start, s.source_end) == (prompt.source_start, prompt.source_end)
                                for s in board.shots):
            raise ValueError('Prompt shot or source range is no longer current')
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            last = con.execute('SELECT MAX(version) FROM image_prompt WHERE shot_id=?', (prompt.shot_id,)).fetchone()[0]
            if prompt.version != (last or 0) + 1:
                raise ValueError('Prompt version must follow existing history')
            con.execute('INSERT INTO image_prompt VALUES(?,?,?,?,?,?,?,?)',
                (prompt.identifier, prompt.shot_id, prompt.scene_id, prompt.version,
                 prompt.review_status, prompt.dependency_fingerprint, prompt.created_at.isoformat(),
                 prompt.model_dump_json()))
            con.executemany('INSERT INTO prompt_reference VALUES(?,?,?)',
                [(prompt.identifier, r.asset_id, r.role) for r in prompt.references])
            if activate and not con.execute('SELECT 1 FROM prompt_approved WHERE shot_id=?', (prompt.shot_id,)).fetchone():
                con.execute('INSERT OR REPLACE INTO prompt_active VALUES(?,?)', (prompt.shot_id, prompt.identifier))

    def _update(self, prompt: ImagePrompt):
        prompt.modified_at = datetime.now(timezone.utc)
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            if not con.execute('SELECT 1 FROM image_prompt WHERE identifier=?', (prompt.identifier,)).fetchone():
                raise ValueError('Prompt version does not exist')
            con.execute('UPDATE image_prompt SET review_status=?, data_json=? WHERE identifier=?',
                        (prompt.review_status, prompt.model_dump_json(), prompt.identifier))

    def edit(self, shot_id, *, positive_prompt, negative_prompt, components, notes=''):
        current = self.active(shot_id)
        if not current: raise ValueError('No prompt to edit')
        edited = current.model_copy(deep=True)
        edited.identifier = str(uuid4())
        edited.version = self.latest(shot_id).version + 1
        edited.created_at = datetime.now(timezone.utc)
        edited.modified_at = edited.created_at
        edited.positive_prompt = positive_prompt
        edited.negative_prompt = negative_prompt
        edited.components = components
        edited.components.generation_notes = notes
        edited.manually_edited = True
        edited.review_status = 'Manually Edited'
        self.save(edited, activate=False)
        self.restore(shot_id, edited.version)
        return edited

    def restore(self, shot_id, version):
        prompt = next((p for p in self.versions(shot_id) if p.version == version), None)
        if not prompt: raise ValueError('Invalid prompt version')
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('INSERT OR REPLACE INTO prompt_active VALUES(?,?)', (shot_id, prompt.identifier))
        return prompt

    def approve(self, shot_id, version, scene, shot, board, project, library):
        prompt = next((p for p in self.versions(shot_id) if p.version == version), None)
        if not prompt: raise ValueError('Invalid prompt version')
        issues = self.validate(prompt, scene, shot, board, project, library)
        if issues: raise ValueError('Cannot approve prompt: ' + '; '.join(issues))
        prompt.review_status = 'Approved'
        self._update(prompt)
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('INSERT OR REPLACE INTO prompt_approved VALUES(?,?)', (shot_id, prompt.identifier))
            con.execute('INSERT OR REPLACE INTO prompt_active VALUES(?,?)', (shot_id, prompt.identifier))
        return prompt

    def validate(self, prompt, scene, shot, board, project, library, *, max_length=None):
        issues = []
        if self.settings() != prompt.settings:
            issues.append('Prompt generation settings changed')
        prose_path = project.resolved_prose(self.root)
        if prose_path:
            try:
                if not prose_path.is_file() or source_hash(read_prose(prose_path)) != board.prose_hash:
                    issues.append('Project prose changed or is missing')
            except (OSError, ValueError):
                issues.append('Project prose cannot be read')
        if prompt.shot_id != shot.identifier or prompt.scene_id != scene.identifier:
            issues.append('Invalid scene or shot association')
        if not prompt.positive_prompt.strip(): issues.append('Missing positive prompt')
        if (prompt.source_start, prompt.source_end) != (shot.source_start, shot.source_end):
            issues.append('Source range changed')
        if max_length and len(prompt.positive_prompt) > max_length:
            issues.append('Prompt exceeds provider length limit')
        ratios = set(re.findall(r'\b\d+\s*:\s*\d+\b', prompt.positive_prompt))
        if any(ratio.replace(' ', '') != prompt.aspect_ratio for ratio in ratios):
            issues.append('Contradictory aspect-ratio instruction')
        try:
            payload, refs, _, missing, digest = context_for_shot(shot, scene, board, library, project, prompt.settings)
            issues.extend(message for message in missing if not message.startswith('No approved image reference'))
            if digest != prompt.dependency_fingerprint:
                legacy_digest = context_for_shot(shot, scene, board, library, project,
                                                  prompt.settings, legacy=True)[4]
                if legacy_digest != prompt.dependency_fingerprint:
                    issues.append('Prompt dependencies changed')
            if prompt.aspect_ratio != payload['aspect_ratio']: issues.append('Aspect ratio changed')
            if any(eid not in {e['id'] for e in payload['entities']} for eid in prompt.entity_ids):
                issues.append('Invalid entity association')
            if any(e['name'].casefold() not in prompt.positive_prompt.casefold() for e in payload['entities']
                   if e['type'] in {'Character', 'Robot', 'Vehicle'}):
                issues.append('Required visual subject missing from prompt')
            if any(r.asset_id not in {a.asset_id for a in refs} for r in prompt.references):
                issues.append('Reference association changed')
        except ValueError as exc:
            issues.append(str(exc))
        return issues

    def state(self, prompt, scene, shot, board, project, library, *, issues=None):
        if issues is None:
            issues = self.validate(prompt, scene, shot, board, project, library)
        if any(issue in issues for issue in ('Prompt dependencies changed',
                'Prompt generation settings changed', 'Source range changed',
                'Project prose changed or is missing', 'Aspect ratio changed',
                'Reference association changed')):
            return 'Outdated'
        return 'Needs Review' if issues else prompt.review_status

    def generation_package(self, shot_id, scene, shot, board, project, library):
        prompt = self.approved(shot_id)
        if not prompt: raise ValueError('Shot has no approved prompt')
        issues = self.validate(prompt, scene, shot, board, project, library)
        if issues: raise ValueError('Approved prompt needs review: ' + '; '.join(issues))
        payload, _, _, _, _ = context_for_shot(shot, scene, board, library, project, prompt.settings)
        return {'shot_id': shot_id, 'prompt_id': prompt.identifier, 'version': prompt.version,
                'positive_prompt': prompt.positive_prompt, 'negative_prompt': prompt.negative_prompt,
                'aspect_ratio': prompt.aspect_ratio, 'visual_style': payload['style'],
                'references': [r.model_dump() for r in prompt.references],
                'entities': payload['entities'], 'dependency_fingerprint': prompt.dependency_fingerprint}
