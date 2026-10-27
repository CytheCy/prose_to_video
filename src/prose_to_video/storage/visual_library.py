"""Transactional visual continuity library backed by a project's SQLite database."""
from __future__ import annotations

import hashlib
from difflib import SequenceMatcher
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4
from PySide6.QtGui import QImageReader
from PySide6.QtCore import Qt
from prose_to_video.models.visual_reference import (AppearanceVariant, ContinuityWarning, EntityReference,
    ReferenceAsset, SourceEvidence, VisualEntity, VisualStyle, now)
from prose_to_video.storage.database import ProjectDatabase, ProjectDatabaseError
from prose_to_video.models.scene import source_hash
from prose_to_video.utils.prose import read_prose

class VisualLibrary:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.db = ProjectDatabase(self.root / 'project.sqlite')

    @contextmanager
    def transaction(self):
        try:
            with closing(self.db._connect()) as con, con:
                self.db._check_schema(con)
                yield con
        except sqlite3.Error as exc:
            raise ProjectDatabaseError(f'Visual library database error: {exc}') from exc

    def _audit(self, con, action, **details):
        con.execute('INSERT INTO visual_audit(occurred_at, action, details_json) VALUES(?,?,?)',
                    (now().isoformat(), action, json.dumps(details, sort_keys=True)))

    @staticmethod
    def _hash_file(path):
        digest = hashlib.sha256()
        with Path(path).open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        return digest.hexdigest()

    def audit_log(self):
        with self.transaction() as con:
            return [dict(r) for r in con.execute('SELECT * FROM visual_audit ORDER BY identifier')]

    def entities(self, entity_type=None):
        with self.transaction() as con:
            rows = con.execute('SELECT data_json FROM visual_entity WHERE (? IS NULL OR entity_type=?) ORDER BY name COLLATE NOCASE',
                               (entity_type, entity_type)).fetchall()
            result = []
            for row in rows:
                entity = VisualEntity.model_validate_json(row[0])
                entity.aliases = [r[0] for r in con.execute('SELECT alias FROM entity_alias WHERE entity_id=? ORDER BY alias', (entity.identifier,))]
                result.append(entity)
            return result

    def entity(self, identifier):
        return next((e for e in self.entities() if e.identifier == identifier), None)

    def save_entity(self, entity: VisualEntity):
        entity = VisualEntity.model_validate(entity.model_dump())
        with self.transaction() as con:
            old = con.execute('SELECT data_json FROM visual_entity WHERE identifier=?', (entity.identifier,)).fetchone()
            previous = VisualEntity.model_validate_json(old[0]) if old else None
            if old:
                entity.created_at = previous.created_at
                entity.modified_at = now()
            con.execute('INSERT OR REPLACE INTO visual_entity VALUES(?,?,?,?,?,?)',
                        (entity.identifier, entity.name, entity.entity_type, entity.review_status,
                         int(entity.recurring), entity.model_dump_json()))
            con.execute('DELETE FROM entity_alias WHERE entity_id=?', (entity.identifier,))
            con.executemany('INSERT INTO entity_alias VALUES(?,?)', [(entity.identifier, a) for a in entity.aliases])
            for field in ('description', 'appearance_description', 'distinguishing_features',
                          'colors_materials', 'size_proportions', 'clothing_accessories'):
                value = getattr(entity, field)
                if value and (previous is None or value != getattr(previous, field)):
                    evidence = SourceEvidence(field=field, value=value, kind='user', status='Approved')
                    con.execute('INSERT INTO entity_evidence VALUES(?,?,?,?)',
                                (evidence.identifier, entity.identifier, None, evidence.model_dump_json()))
            prior_aliases = {a.casefold() for a in previous.aliases} if previous else set()
            for alias in entity.aliases:
                if alias.casefold() not in prior_aliases:
                    evidence = SourceEvidence(field='alias', value=alias, kind='user', status='Approved')
                    con.execute('INSERT INTO entity_evidence VALUES(?,?,?,?)',
                                (evidence.identifier, entity.identifier, None, evidence.model_dump_json()))
            self._audit(con, 'entity_updated' if old else 'entity_created', entity_id=entity.identifier)
        return entity

    def delete_entity(self, identifier, *, confirmed=False):
        if not confirmed: raise ValueError('Deleting an entity requires explicit confirmation')
        with self.transaction() as con:
            if not con.execute('SELECT 1 FROM visual_entity WHERE identifier=?', (identifier,)).fetchone():
                raise ValueError('Entity does not exist')
            con.execute('DELETE FROM entity_reference WHERE entity_id=?', (identifier,))
            con.execute('DELETE FROM entity_shot WHERE entity_id=?', (identifier,))
            con.execute('DELETE FROM entity_shot_exclusion WHERE entity_id=?', (identifier,))
            con.execute('DELETE FROM entity_scene WHERE entity_id=?', (identifier,))
            con.execute('DELETE FROM appearance_variant WHERE entity_id=?', (identifier,))
            con.execute('DELETE FROM visual_entity WHERE identifier=?', (identifier,))
            self._audit(con, 'entity_deleted', entity_id=identifier)

    def merge_entities(self, target_id, source_id, *, confirmed=False):
        if not confirmed: raise ValueError('Merging entities requires explicit confirmation')
        if target_id == source_id: raise ValueError('Cannot merge an entity with itself')
        target, source = self.entity(target_id), self.entity(source_id)
        if not target or not source: raise ValueError('Both entities must exist')
        original_target = target.model_dump(mode='json')
        original_source = source.model_dump(mode='json')
        conflict = False
        for field in ('description', 'appearance_description', 'distinguishing_features',
                      'colors_materials', 'size_proportions', 'clothing_accessories'):
            if getattr(target, field) and getattr(source, field) and getattr(target, field) != getattr(source, field):
                conflict = True
            if not getattr(target, field) and getattr(source, field): setattr(target, field, getattr(source, field))
        if source.review_status == 'Approved' and target.review_status != 'Approved':
            target.review_status = 'Needs Review' if conflict else 'Approved'
        aliases = list(dict.fromkeys([*target.aliases, source.name, *source.aliases]))
        aliases = [a for a in aliases if a.casefold() != target.name.casefold()]
        target.aliases = list({a.casefold(): a for a in aliases}.values())
        target = VisualEntity.model_validate(target.model_dump())
        with self.transaction() as con:
            for table, columns in [('entity_scene', 'scene_id'), ('entity_shot', 'shot_id, variant_id')]:
                con.execute(f'INSERT OR IGNORE INTO {table} SELECT ?, {columns} FROM {table} WHERE entity_id=?', (target_id, source_id))
            con.execute('INSERT OR IGNORE INTO entity_shot_exclusion SELECT ?, shot_id FROM entity_shot_exclusion WHERE entity_id=?',
                        (target_id, source_id))
            for row in con.execute('SELECT identifier, data_json FROM appearance_variant WHERE entity_id=?', (source_id,)):
                variant = AppearanceVariant.model_validate_json(row['data_json'])
                variant.entity_id = target_id
                con.execute('UPDATE appearance_variant SET entity_id=?, data_json=? WHERE identifier=?',
                            (target_id, variant.model_dump_json(), row['identifier']))
            con.execute('UPDATE entity_evidence SET entity_id=? WHERE entity_id=?', (target_id, source_id))
            con.execute('INSERT OR IGNORE INTO entity_reference SELECT ?, asset_id, variant_id, role, description, status, is_primary FROM entity_reference WHERE entity_id=?', (target_id, source_id))
            for ref in con.execute('SELECT asset_id, status, is_primary FROM entity_reference WHERE entity_id=?', (source_id,)):
                if ref['status'] == 'Approved':
                    con.execute('UPDATE entity_reference SET status=? WHERE entity_id=? AND asset_id=?',
                                ('Approved', target_id, ref['asset_id']))
            con.execute('DELETE FROM entity_reference WHERE entity_id=?', (source_id,))
            con.execute('DELETE FROM entity_shot WHERE entity_id=?', (source_id,))
            con.execute('DELETE FROM entity_shot_exclusion WHERE entity_id=?', (source_id,))
            con.execute('DELETE FROM entity_scene WHERE entity_id=?', (source_id,))
            con.execute('DELETE FROM visual_entity WHERE identifier=?', (source_id,))
            con.execute('UPDATE visual_entity SET name=?, entity_type=?, status=?, recurring=?, data_json=? WHERE identifier=?',
                        (target.name, target.entity_type, target.review_status, int(target.recurring), target.model_dump_json(), target_id))
            con.executemany('INSERT OR IGNORE INTO entity_alias VALUES(?,?)', [(target_id, a) for a in target.aliases])
            self._audit(con, 'entities_merged', target_id=target_id, source_id=source_id,
                        target_before=original_target, source_before=original_source)
        return target

    def separate(self, entity_id, scene_ids=(), shot_ids=(), asset_ids=(), evidence_ids=(),
                 variant_ids=(), *, new_name=None):
        """Move selected associations into a new identity; copy evidence and links for review."""
        source = self.entity(entity_id)
        if not source: raise ValueError('Entity does not exist')
        created = VisualEntity(name=new_name or f'{source.name} (separate)', entity_type=source.entity_type,
                               review_status='Needs Review', recurring=source.recurring)
        with self.transaction() as con:
            con.execute('INSERT INTO visual_entity VALUES(?,?,?,?,?,?)',
                        (created.identifier, created.name, created.entity_type, created.review_status, int(created.recurring), created.model_dump_json()))
            for scene_id in scene_ids:
                if con.execute('DELETE FROM entity_scene WHERE entity_id=? AND scene_id=?', (entity_id, scene_id)).rowcount:
                    con.execute('INSERT INTO entity_scene VALUES(?,?)', (created.identifier, scene_id))
            for shot_id in shot_ids:
                row = con.execute('SELECT variant_id FROM entity_shot WHERE entity_id=? AND shot_id=?', (entity_id, shot_id)).fetchone()
                if row:
                    con.execute('DELETE FROM entity_shot WHERE entity_id=? AND shot_id=?', (entity_id, shot_id))
                    con.execute('INSERT OR IGNORE INTO entity_shot_exclusion VALUES(?,?)', (entity_id, shot_id))
                    con.execute('INSERT INTO entity_shot VALUES(?,?,?)',
                                (created.identifier, shot_id, row[0] if row[0] in variant_ids else None))
            for evidence_id in evidence_ids:
                con.execute('UPDATE entity_evidence SET entity_id=? WHERE entity_id=? AND identifier=?',
                            (created.identifier, entity_id, evidence_id))
            for variant_id in variant_ids:
                row = con.execute('SELECT data_json FROM appearance_variant WHERE identifier=? AND entity_id=?',
                                  (variant_id, entity_id)).fetchone()
                if row:
                    variant = AppearanceVariant.model_validate_json(row[0]); variant.entity_id = created.identifier
                    for shot_link in con.execute('SELECT shot_id FROM entity_shot WHERE entity_id=? AND variant_id=?',
                                                 (entity_id, variant_id)):
                        con.execute('DELETE FROM entity_shot WHERE entity_id=? AND shot_id=?',
                                    (entity_id, shot_link['shot_id']))
                        con.execute('INSERT OR IGNORE INTO entity_shot_exclusion VALUES(?,?)',
                                    (entity_id, shot_link['shot_id']))
                        con.execute('INSERT OR REPLACE INTO entity_shot VALUES(?,?,?)',
                                    (created.identifier, shot_link['shot_id'], variant_id))
                    con.execute('UPDATE entity_reference SET entity_id=? WHERE entity_id=? AND variant_id=?',
                                (created.identifier, entity_id, variant_id))
                    con.execute('UPDATE appearance_variant SET entity_id=?, data_json=? WHERE identifier=?',
                                (created.identifier, variant.model_dump_json(), variant_id))
            for asset_id in asset_ids:
                con.execute('UPDATE entity_reference SET entity_id=? WHERE entity_id=? AND asset_id=?',
                            (created.identifier, entity_id, asset_id))
                row = con.execute('SELECT variant_id FROM entity_reference WHERE entity_id=? AND asset_id=?',
                                  (created.identifier, asset_id)).fetchone()
                if row and row[0] and row[0] not in variant_ids:
                    con.execute('UPDATE entity_reference SET variant_id=NULL WHERE entity_id=? AND asset_id=?',
                                (created.identifier, asset_id))
            self._audit(con, 'entity_separated', source_id=entity_id, new_id=created.identifier,
                        scene_ids=list(scene_ids), shot_ids=list(shot_ids), asset_ids=list(asset_ids),
                        evidence_ids=list(evidence_ids), variant_ids=list(variant_ids))
        return created

    def add_evidence(self, entity_id, evidence: SourceEvidence):
        evidence = SourceEvidence.model_validate(evidence.model_dump())
        with self.transaction() as con:
            con.execute('INSERT INTO entity_evidence VALUES(?,?,?,?)',
                        (evidence.identifier, entity_id, evidence.scene_id, evidence.model_dump_json()))
        return evidence

    def evidence(self, entity_id):
        with self.transaction() as con:
            return [SourceEvidence.model_validate_json(r[0]) for r in con.execute(
                'SELECT data_json FROM entity_evidence WHERE entity_id=?', (entity_id,))]

    def set_evidence_status(self, identifier, status):
        with self.transaction() as con:
            row = con.execute('SELECT entity_id, data_json FROM entity_evidence WHERE identifier=?', (identifier,)).fetchone()
            if not row: raise ValueError('Evidence does not exist')
            evidence = SourceEvidence.model_validate_json(row['data_json'])
            evidence.status = status
            evidence = SourceEvidence.model_validate(evidence.model_dump())
            con.execute('UPDATE entity_evidence SET data_json=? WHERE identifier=?', (evidence.model_dump_json(), identifier))
            if status == 'Approved' and evidence.field == 'alias':
                entity_row = con.execute('SELECT data_json FROM visual_entity WHERE identifier=?',
                                         (row['entity_id'],)).fetchone()
                if entity_row:
                    entity = VisualEntity.model_validate_json(entity_row[0])
                    if evidence.value.casefold() not in {x.casefold() for x in [entity.name, *entity.aliases]}:
                        entity.aliases.append(evidence.value)
                        entity.modified_at = now()
                        entity = VisualEntity.model_validate(entity.model_dump())
                        con.execute('UPDATE visual_entity SET data_json=? WHERE identifier=?',
                                    (entity.model_dump_json(), entity.identifier))
                        con.execute('INSERT INTO entity_alias VALUES(?,?)', (entity.identifier, evidence.value))
            self._audit(con, 'evidence_reviewed', evidence_id=identifier, status=status)

    def add_variant(self, variant: AppearanceVariant):
        variant = AppearanceVariant.model_validate(variant.model_dump())
        with self.transaction() as con:
            if variant.is_default:
                con.execute('UPDATE appearance_variant SET is_default=0 WHERE entity_id=?', (variant.entity_id,))
            con.execute('INSERT INTO appearance_variant VALUES(?,?,?,?,?)',
                        (variant.identifier, variant.entity_id, variant.status, int(variant.is_default), variant.model_dump_json()))
            self._audit(con, 'variant_added', entity_id=variant.entity_id, variant_id=variant.identifier)
        return variant

    def update_variant(self, variant: AppearanceVariant):
        variant = AppearanceVariant.model_validate(variant.model_dump())
        with self.transaction() as con:
            row = con.execute('SELECT entity_id FROM appearance_variant WHERE identifier=?', (variant.identifier,)).fetchone()
            if not row or row[0] != variant.entity_id: raise ValueError('Variant does not belong to entity')
            if variant.is_default:
                con.execute('UPDATE appearance_variant SET is_default=0 WHERE entity_id=?', (variant.entity_id,))
            con.execute('UPDATE appearance_variant SET status=?, is_default=?, data_json=? WHERE identifier=?',
                        (variant.status, int(variant.is_default), variant.model_dump_json(), variant.identifier))
            self._audit(con, 'variant_updated', variant_id=variant.identifier)
        return variant

    def variants(self, entity_id):
        with self.transaction() as con:
            result = []
            for row in con.execute('SELECT data_json, is_default FROM appearance_variant WHERE entity_id=?', (entity_id,)):
                variant = AppearanceVariant.model_validate_json(row[0]); variant.is_default = bool(row[1]); result.append(variant)
            return result

    def relate_scene(self, entity_id, scene_id, present=True):
        if scene_id not in {s.identifier for s in self.db.load_scenes()[1]}: raise ValueError('Scene does not exist')
        with self.transaction() as con:
            con.execute('INSERT OR IGNORE INTO entity_scene VALUES(?,?)' if present else
                        'DELETE FROM entity_scene WHERE entity_id=? AND scene_id=?', (entity_id, scene_id))
            self._audit(con, 'scene_link_changed', entity_id=entity_id, scene_id=scene_id, present=present)

    def scenes_for(self, entity_id):
        with self.transaction() as con:
            return [r[0] for r in con.execute('SELECT scene_id FROM entity_scene WHERE entity_id=?', (entity_id,))]

    def _shot_exists(self, shot_id):
        with self.transaction() as con:
            return bool(con.execute('SELECT 1 FROM shot s JOIN active_storyboard a ON a.run_id=s.run_id WHERE s.identifier=?', (shot_id,)).fetchone())

    def relate_shot(self, entity_id, shot_id, variant_id=None, present=True, *, automatic=False):
        if not self._shot_exists(shot_id): raise ValueError('Shot does not exist')
        if variant_id and not any(v.identifier == variant_id and v.status == 'Approved'
                                  for v in self.variants(entity_id)):
            raise ValueError('Shot variant must be approved and belong to this entity')
        with self.transaction() as con:
            if automatic and con.execute('SELECT 1 FROM entity_shot_exclusion WHERE entity_id=? AND shot_id=?',
                                         (entity_id, shot_id)).fetchone(): return
            if present:
                if automatic:
                    con.execute('INSERT OR IGNORE INTO entity_shot VALUES(?,?,?)', (entity_id, shot_id, variant_id))
                else:
                    con.execute('INSERT OR REPLACE INTO entity_shot VALUES(?,?,?)', (entity_id, shot_id, variant_id))
                    con.execute('DELETE FROM entity_shot_exclusion WHERE entity_id=? AND shot_id=?', (entity_id, shot_id))
            else:
                con.execute('DELETE FROM entity_shot WHERE entity_id=? AND shot_id=?', (entity_id, shot_id))
                con.execute('INSERT OR IGNORE INTO entity_shot_exclusion VALUES(?,?)', (entity_id, shot_id))
            self._audit(con, 'shot_link_changed', entity_id=entity_id, shot_id=shot_id, variant_id=variant_id, present=present)

    def shots_for(self, entity_id):
        with self.transaction() as con:
            return [dict(r) for r in con.execute('SELECT shot_id, variant_id FROM entity_shot WHERE entity_id=?', (entity_id,))]

    def entities_for_shot(self, shot_id):
        ids = {r['shot_id']: None for r in []}
        with self.transaction() as con:
            ids = [r[0] for r in con.execute('SELECT entity_id FROM entity_shot WHERE shot_id=?', (shot_id,))]
        return [e for e in self.entities() if e.identifier in ids]

    def save_style(self, style: VisualStyle, preset_name=None):
        style = VisualStyle.model_validate(style.model_dump())
        with self.transaction() as con:
            con.execute('INSERT OR REPLACE INTO visual_style VALUES(1,?)', (style.model_dump_json(),))
            if preset_name: con.execute('INSERT OR REPLACE INTO visual_style_preset VALUES(?,?)', (preset_name, style.model_dump_json()))
            self._audit(con, 'style_saved', preset=preset_name)

    def save_style_override(self, scope: str, target_id: str, style: VisualStyle | None):
        if scope not in {'scene', 'shot'}: raise ValueError('Style override scope must be scene or shot')
        if scope == 'scene' and target_id not in {s.identifier for s in self.db.load_scenes()[1]}:
            raise ValueError('Scene does not exist')
        if scope == 'shot' and not self._shot_exists(target_id): raise ValueError('Shot does not exist')
        with self.transaction() as con:
            if style is None:
                con.execute('DELETE FROM visual_style_override WHERE scope=? AND target_id=?', (scope, target_id))
            else:
                style = VisualStyle.model_validate(style.model_dump())
                con.execute('INSERT OR REPLACE INTO visual_style_override VALUES(?,?,?)',
                            (scope, target_id, style.model_dump_json()))
            self._audit(con, 'style_override_changed', scope=scope, target_id=target_id)

    def style_for_shot(self, shot_id: str):
        with self.transaction() as con:
            shot = con.execute('SELECT s.scene_id FROM shot s JOIN active_storyboard a ON a.run_id=s.run_id '
                               'WHERE s.identifier=?', (shot_id,)).fetchone()
            if not shot: raise ValueError('Shot does not exist')
            style = self.style()
            for scope, target in [('scene', shot[0]), ('shot', shot_id)]:
                row = con.execute('SELECT data_json FROM visual_style_override WHERE scope=? AND target_id=?',
                                  (scope, target)).fetchone()
                if row:
                    override = VisualStyle.model_validate_json(row[0])
                    style = style.model_copy(update={key: value for key, value in override.model_dump().items() if value})
            return style

    def style(self):
        with self.transaction() as con:
            row = con.execute('SELECT data_json FROM visual_style WHERE singleton=1').fetchone()
            return VisualStyle.model_validate_json(row[0]) if row else VisualStyle()

    def style_presets(self):
        with self.transaction() as con:
            return [r[0] for r in con.execute('SELECT name FROM visual_style_preset ORDER BY name')]

    def load_style_preset(self, name):
        with self.transaction() as con:
            row = con.execute('SELECT data_json FROM visual_style_preset WHERE name=?', (name,)).fetchone()
            if not row: raise ValueError('Style preset does not exist')
            return VisualStyle.model_validate_json(row[0])

    def _asset_path(self, asset):
        path = (self.root / asset.path).resolve()
        if not path.is_relative_to(self.root.resolve()): raise ValueError('Unsafe asset path')
        return path

    def import_image(self, source: Path):
        source = Path(source)
        if source.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}: raise ValueError('Choose a PNG, JPEG, or WebP image')
        if not source.is_file(): raise ValueError('Image file is missing')
        reader = QImageReader(str(source)); reader.setAutoTransform(True)
        if not reader.canRead(): raise ValueError('Image is invalid or corrupted')
        fmt = bytes(reader.format()).decode().upper()
        image = reader.read()
        if image.isNull(): raise ValueError('Image is invalid or corrupted')
        if fmt not in {'PNG', 'JPEG', 'WEBP'}: raise ValueError('Unsupported image content')
        digest = self._hash_file(source)
        with self.transaction() as con:
            row = con.execute('SELECT * FROM reference_asset WHERE sha256=?', (digest,)).fetchone()
            if row and self._asset_path(ReferenceAsset(**dict(row))).is_file():
                return ReferenceAsset(**dict(row))
        directory = self.root / 'references'
        directory.mkdir(exist_ok=True)
        identifier = str(uuid4())
        extension = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}[fmt]
        dest = directory / f'{identifier}{extension}'
        thumb = directory / f'{identifier}.thumb.png'
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix='.reference-', delete=False) as stream:
                temporary = Path(stream.name)
                with source.open('rb') as input_stream: shutil.copyfileobj(input_stream, stream)
                stream.flush(); os.fsync(stream.fileno())
            if self._hash_file(temporary) != digest:
                raise ValueError('Image changed during import')
            os.replace(temporary, dest); temporary = None
            if not image.scaled(256, 256, Qt.KeepAspectRatio, Qt.SmoothTransformation).save(str(thumb), 'PNG'):
                raise ValueError('Could not create image thumbnail')
            asset = ReferenceAsset(identifier=identifier, path=f'references/{dest.name}', sha256=digest,
                                   width=image.width(), height=image.height(), format=fmt)
            with self.transaction() as con:
                con.execute('INSERT INTO reference_asset VALUES(?,?,?,?,?,?,?)',
                            (asset.identifier, asset.path, asset.sha256, asset.width, asset.height,
                             asset.format, asset.imported_at.isoformat()))
                self._audit(con, 'asset_imported', asset_id=asset.identifier)
            return asset
        except Exception:
            dest.unlink(missing_ok=True); thumb.unlink(missing_ok=True)
            raise
        finally:
            if temporary: temporary.unlink(missing_ok=True)

    def assets(self):
        with self.transaction() as con:
            return [ReferenceAsset(**dict(r)) for r in con.execute('SELECT * FROM reference_asset')]

    def asset(self, identifier):
        with self.transaction() as con:
            row = con.execute('SELECT * FROM reference_asset WHERE identifier=?', (identifier,)).fetchone()
            return ReferenceAsset(**dict(row)) if row else None

    def thumbnail_path(self, asset):
        return self.root / 'references' / f'{asset.identifier}.thumb.png'

    def asset_issues(self):
        issues = []
        for asset in self.assets():
            path = self._asset_path(asset)
            if not path.is_file(): issues.append(ContinuityWarning(severity='confirmed', code='missing_asset', message=f'Missing image: {asset.path}'))
            elif self._hash_file(path) != asset.sha256:
                issues.append(ContinuityWarning(severity='confirmed', code='changed_asset', message=f'Image changed outside project: {asset.path}'))
        return issues

    def attach_image(self, reference: EntityReference):
        reference = EntityReference.model_validate(reference.model_dump())
        if reference.variant_id and not any(v.identifier == reference.variant_id for v in self.variants(reference.entity_id)):
            raise ValueError('Variant does not belong to entity')
        with self.transaction() as con:
            old = con.execute('SELECT status FROM entity_reference WHERE entity_id=? AND asset_id=?',
                              (reference.entity_id, reference.asset_id)).fetchone()
            if old and old[0] == 'Approved' and reference.status != 'Approved':
                raise ValueError('Approved reference requires an explicit replacement action')
            if reference.is_primary:
                con.execute('UPDATE entity_reference SET is_primary=0 WHERE entity_id=? AND variant_id IS ?',
                            (reference.entity_id, reference.variant_id))
            con.execute('INSERT OR REPLACE INTO entity_reference VALUES(?,?,?,?,?,?,?)',
                        (reference.entity_id, reference.asset_id, reference.variant_id, reference.role,
                         reference.description, reference.status, int(reference.is_primary)))
            self._audit(con, 'reference_attached', entity_id=reference.entity_id, asset_id=reference.asset_id)

    def references(self, entity_id):
        with self.transaction() as con:
            return [EntityReference(**dict(r)) for r in con.execute('SELECT * FROM entity_reference WHERE entity_id=?', (entity_id,))]

    def remove_reference(self, entity_id, asset_id, *, confirmed=False):
        with self.transaction() as con:
            row = con.execute('SELECT status FROM entity_reference WHERE entity_id=? AND asset_id=?',
                              (entity_id, asset_id)).fetchone()
            if row and row[0] == 'Approved' and not confirmed:
                raise ValueError('Removing an approved reference requires explicit confirmation')
            con.execute('DELETE FROM entity_reference WHERE entity_id=? AND asset_id=?', (entity_id, asset_id))
            self._audit(con, 'reference_removed', entity_id=entity_id, asset_id=asset_id)

    def relate_shot_asset(self, shot_id, asset_id, present=True):
        if not self._shot_exists(shot_id): raise ValueError('Shot does not exist')
        asset = self.asset(asset_id)
        if not asset: raise ValueError('Reference image does not exist')
        if present and not any(r.asset_id == asset_id and r.status == 'Approved'
                               for e in self.entities_for_shot(shot_id) for r in self.references(e.identifier)):
            raise ValueError('Shot image must be an approved reference of an associated entity')
        with self.transaction() as con:
            if present: con.execute('INSERT OR IGNORE INTO shot_reference VALUES(?,?)', (shot_id, asset_id))
            else: con.execute('DELETE FROM shot_reference WHERE shot_id=? AND asset_id=?', (shot_id, asset_id))
            self._audit(con, 'shot_asset_changed', shot_id=shot_id, asset_id=asset_id, present=present)

    def shot_assets(self, shot_id):
        with self.transaction() as con:
            ids = [r[0] for r in con.execute('SELECT asset_id FROM shot_reference WHERE shot_id=?', (shot_id,))]
        return [asset for asset_id in ids if (asset := self.asset(asset_id))]

    def cleanup_unreferenced_assets(self, *, confirmed=False):
        if not confirmed: raise ValueError('Asset cleanup requires explicit confirmation')
        with self.transaction() as con:
            rows = con.execute('SELECT * FROM reference_asset a WHERE NOT EXISTS '
                '(SELECT 1 FROM entity_reference e WHERE e.asset_id=a.identifier) AND NOT EXISTS '
                '(SELECT 1 FROM shot_reference s WHERE s.asset_id=a.identifier)').fetchall()
            assets = [ReferenceAsset(**dict(r)) for r in rows]
            for asset in assets:
                con.execute('DELETE FROM reference_asset WHERE identifier=?', (asset.identifier,))
                self._audit(con, 'unused_asset_removed', asset_id=asset.identifier)
        for asset in assets:
            self._asset_path(asset).unlink(missing_ok=True)
            self.thumbnail_path(asset).unlink(missing_ok=True)
        return len(assets)

    def set_primary(self, entity_id, asset_id):
        with self.transaction() as con:
            row = con.execute('SELECT variant_id FROM entity_reference WHERE entity_id=? AND asset_id=?', (entity_id, asset_id)).fetchone()
            if not row: raise ValueError('Image is not attached to entity')
            con.execute('UPDATE entity_reference SET is_primary=0 WHERE entity_id=? AND variant_id IS ?', (entity_id, row[0]))
            con.execute('UPDATE entity_reference SET is_primary=1 WHERE entity_id=? AND asset_id=?', (entity_id, asset_id))
            self._audit(con, 'primary_reference_set', entity_id=entity_id, asset_id=asset_id)

    def package_for_shot(self, shot_id):
        with self.transaction() as con:
            rows = con.execute('SELECT entity_id, variant_id FROM entity_shot WHERE shot_id=?', (shot_id,)).fetchall()
        package = {'shot_id': shot_id, 'style': self.style_for_shot(shot_id).model_dump(mode='json'), 'entities': []}
        for row in rows:
            entity = self.entity(row['entity_id'])
            if not entity: continue
            variant = next((v for v in self.variants(entity.identifier) if v.identifier == row['variant_id']), None)
            refs = []
            for ref in self.references(entity.identifier):
                if ref.status != 'Approved' or (ref.variant_id and ref.variant_id != row['variant_id']): continue
                asset = self.asset(ref.asset_id)
                if asset:
                    refs.append({'asset': asset.model_dump(mode='json'), 'role': ref.role,
                                 'description': ref.description, 'primary': ref.is_primary})
            package['entities'].append({'id': entity.identifier, 'name': entity.name, 'type': entity.entity_type,
                'appearance': entity.appearance_description if entity.review_status == 'Approved' else '',
                'approved_design': {key: getattr(entity, key) for key in
                    ('description', 'appearance_description', 'distinguishing_features',
                     'colors_materials', 'size_proportions', 'clothing_accessories') if getattr(entity, key)}
                    if entity.review_status == 'Approved' else {},
                'variant': variant.model_dump(mode='json') if variant and variant.status == 'Approved' else None,
                'references': refs,
                'constraints': [e.model_dump(mode='json') for e in self.evidence(entity.identifier)
                                if e.kind == 'explicit' and e.status in {'Reviewed', 'Approved'}],
                'approved_details': [e.model_dump(mode='json') for e in self.evidence(entity.identifier)
                                     if e.status == 'Approved']})
        with self.transaction() as con:
            selected = [r[0] for r in con.execute('SELECT asset_id FROM shot_reference WHERE shot_id=?', (shot_id,))]
        approved_ids = {r.asset_id for e in self.entities() for r in self.references(e.identifier)
                        if r.status == 'Approved'}
        package['shot_references'] = [a.model_dump(mode='json') for asset_id in selected
                                      if asset_id in approved_ids and (a := self.asset(asset_id))]
        return package

    def inspect_continuity(self):
        warnings = self.asset_issues()
        entities = self.entities()
        current_source_hash, current_scenes = self.db.load_scenes()
        current_scene_ids = {s.identifier for s in current_scenes}
        prose_path = self.db.load().resolved_prose(self.root)
        if prose_path and not prose_path.is_file():
            warnings.append(ContinuityWarning(severity='confirmed', code='missing_prose',
                message='Imported prose file is missing'))
        elif prose_path and current_source_hash and source_hash(read_prose(prose_path)) != current_source_hash:
            warnings.append(ContinuityWarning(severity='confirmed', code='stale_source',
                message='Scene source ranges are outdated because the prose changed'))
        for i, entity in enumerate(entities):
            for other in entities[i+1:]:
                if entity.entity_type == other.entity_type and (
                    entity.name.casefold() == other.name.casefold() or
                    entity.name.casefold() in [a.casefold() for a in other.aliases] or
                    other.name.casefold() in [a.casefold() for a in entity.aliases] or
                    (min(len(entity.name), len(other.name)) >= 4 and
                     SequenceMatcher(None, entity.name.casefold(), other.name.casefold()).ratio() >= .78)):
                    warnings.append(ContinuityWarning(severity='possible', code='duplicate_identity',
                        message=f'{entity.name} and {other.name} may be the same identity',
                        entity_ids=[entity.identifier, other.identifier]))
            if entity.entity_type == 'Character' and len(self.scenes_for(entity.identifier)) >= 3 and not any(
                    r.status == 'Approved' for r in self.references(entity.identifier)):
                warnings.append(ContinuityWarning(severity='possible', code='missing_reference',
                    message=f'{entity.name} recurs without an approved image', entity_ids=[entity.identifier]))
            evidence = self.evidence(entity.identifier)
            for scene_id in self.scenes_for(entity.identifier):
                if scene_id not in current_scene_ids:
                    warnings.append(ContinuityWarning(severity='confirmed', code='missing_scene',
                        message=f'{entity.name} links to a missing narrative scene', entity_ids=[entity.identifier]))
            if current_source_hash and any(e.source_hash and e.source_hash != current_source_hash for e in evidence):
                warnings.append(ContinuityWarning(severity='confirmed', code='stale_evidence',
                    message=f'{entity.name} has source evidence from an older manuscript', entity_ids=[entity.identifier]))
            for field in {e.field for e in evidence}:
                values = {e.value.casefold() for e in evidence if e.field == field and e.kind == 'explicit' and e.value}
                if len(values) > 1:
                    scenes_with_values = {e.scene_id for e in evidence if e.field == field and e.kind == 'explicit' and e.value}
                    contextual_variants = [v for v in self.variants(entity.identifier)
                                           if v.status == 'Approved' and v.scene_ids]
                    if len(scenes_with_values) > 1 and contextual_variants:
                        continue
                    warnings.append(ContinuityWarning(severity='possible', code='description_conflict',
                        message=f'{entity.name} has differing source descriptions for {field}', entity_ids=[entity.identifier]))
        with self.transaction() as con:
            for row in con.execute('SELECT es.entity_id, es.shot_id FROM entity_shot es LEFT JOIN visual_entity e '
                                   'ON e.identifier=es.entity_id WHERE e.identifier IS NULL'):
                warnings.append(ContinuityWarning(severity='confirmed', code='missing_entity',
                    message=f'Shot {row["shot_id"]} references missing entity {row["entity_id"]}',
                    shot_ids=[row['shot_id']]))
            for row in con.execute('SELECT entity_id, shot_id, variant_id FROM entity_shot'):
                if not self._shot_exists(row['shot_id']):
                    warnings.append(ContinuityWarning(severity='confirmed', code='missing_shot',
                        message=f'Entity link targets missing shot {row["shot_id"]}', shot_ids=[row['shot_id']]))
                if row['variant_id']:
                    variant = next((v for v in self.variants(row['entity_id']) if v.identifier == row['variant_id']), None)
                    if not variant:
                        warnings.append(ContinuityWarning(severity='confirmed', code='invalid_variant_link',
                            message=f'Shot {row["shot_id"]} references a variant belonging to another entity',
                            shot_ids=[row['shot_id']]))
                        continue
                    if variant and (variant.scene_ids or variant.shot_ids):
                        shot = con.execute('SELECT scene_id FROM shot WHERE identifier=?', (row['shot_id'],)).fetchone()
                        if shot and ((variant.scene_ids and shot[0] not in variant.scene_ids) or
                                     (variant.shot_ids and row['shot_id'] not in variant.shot_ids)):
                            warnings.append(ContinuityWarning(severity='possible', code='variant_context',
                                message=f'Variant {variant.name} may not fit this scene', shot_ids=[row['shot_id']]))
        return warnings

    def export_data(self):
        with self.transaction() as con:
            overrides = [{'scope': r['scope'], 'target_id': r['target_id'],
                          'style': VisualStyle.model_validate_json(r['data_json']).model_dump(mode='json')}
                         for r in con.execute('SELECT * FROM visual_style_override')]
        return {'style': self.style().model_dump(mode='json'), 'style_overrides': overrides, 'entities': [
            {'entity': e.model_dump(mode='json'), 'evidence': [v.model_dump(mode='json') for v in self.evidence(e.identifier)],
             'variants': [v.model_dump(mode='json') for v in self.variants(e.identifier)],
             'references': [{'link': r.model_dump(mode='json'), 'asset': self.asset(r.asset_id).model_dump(mode='json')}
                            for r in self.references(e.identifier) if self.asset(r.asset_id)],
             'scene_ids': self.scenes_for(e.identifier), 'shots': self.shots_for(e.identifier)} for e in self.entities()]}
