"""Whole-manuscript character catalog with distinct-paragraph recurrence."""
from __future__ import annotations

import json
import re
from threading import Event
from pydantic import BaseModel, ConfigDict, Field, field_validator
from prose_to_video.pipeline.providers import AIResponseError
from prose_to_video.models.scene import source_hash
from prose_to_video.models.visual_reference import VisualEntity, SourceEvidence
from prose_to_video.pipeline.entity_extraction import APPEARANCE_FIELDS


class CharacterProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    paragraph_ids: list[int]
    appearance_description: str = ''
    distinguishing_features: str = ''
    colors_materials: str = ''
    size_proportions: str = ''
    clothing_accessories: str = ''

    @field_validator('aliases', *sorted(APPEARANCE_FIELDS), mode='before')
    @classmethod
    def blank_unknowns(cls, value, info):
        if value is None:
            return [] if info.field_name == 'aliases' else ''
        return value


class CharacterProposals(BaseModel):
    model_config = ConfigDict(extra='forbid')
    characters: list[CharacterProposal]


class CharacterMention(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    paragraph_ids: list[int]


class CharacterMentions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    characters: list[CharacterMention]


# Pronouns are contextual references, never global aliases for a person.
PRONOUNS = {'he', 'him', 'his', 'she', 'her', 'hers', 'they', 'them', 'their',
            'theirs', 'it', 'its', 'i', 'me', 'my', 'mine', 'we', 'us', 'our', 'you', 'your'}


def character_paragraphs(text):
    normalized = text.replace('\r\n', '\n').replace('\r', '\n').strip()
    # Blank-line manuscripts may contain soft wraps inside paragraphs. For prose
    # without blank separators, match the app's line-separated manuscript support.
    separator = r'\n[ \t]*\n' if re.search(r'\n[ \t]*\n', normalized) else r'\n'
    return [p for p in re.split(separator, normalized) if p.strip()]


class CharactersCanceled(ValueError):
    pass


def listed_characters(library):
    return sorted((e for e in library.entities('Character') if len(e.character_paragraphs) > 1),
                  key=lambda e: (min(e.character_paragraphs), e.name))


def _key(value):
    return ' '.join(value.split()).casefold()


def detect_characters(text, library, provider, settings, *, cancel=None, progress=None, source_check=None):
    if not text.strip():
        raise ValueError('Import nonempty prose before generating characters')
    settings.validate()
    cancel = cancel or Event()
    progress = progress or (lambda message: None)
    def check():
        if cancel.is_set():
            raise CharactersCanceled('Character generation canceled; saved characters were preserved')
        if source_check:
            source_check()
    # Split long paragraphs for provider limits, retaining the same ID so they count once.
    paragraphs = character_paragraphs(text)
    batches, batch, size = [], [], 0
    for pid, paragraph in enumerate(paragraphs, 1):
        for start in range(0, len(paragraph), settings.chunk_chars):
            piece = paragraph[start:start + settings.chunk_chars]
            if batch and size + len(piece) > settings.chunk_chars:
                batches.append(batch); batch, size = [], 0
            batch.append({'id': pid, 'prose': piece}); size += len(piece)
    if batch:
        batches.append(batch)
    catalog = []
    context = ''
    pending = [(batch, 0) for batch in batches]
    completed = 0
    successful_batches = []
    while pending:
        batch, depth = pending.pop(0)
        check()
        progress(f'Analyzing characters: {completed + 1}/{completed + len(pending) + 1}')
        try:
            response = provider.detect_characters(json.dumps({'paragraphs': batch,
                'preceding_context_only': context,
                'known_characters': [_identity_context(r, paragraphs) for r in catalog]}, ensure_ascii=False))
            result = CharacterProposals.model_validate(response)
        except AIResponseError as exc:
            check()
            if not exc.retry_smaller or depth >= 3:
                raise
            if len(batch) > 1:
                midpoint = len(batch) // 2
                smaller = [batch[:midpoint], batch[midpoint:]]
            elif len(batch[0]['prose']) >= 1000:
                passage = batch[0]
                midpoint = len(passage['prose']) // 2
                smaller = [[{'id': passage['id'], 'prose': passage['prose'][:midpoint]}],
                           [{'id': passage['id'], 'prose': passage['prose'][midpoint:]}]]
            else:
                raise
            progress('AI output was incomplete; retrying this section in smaller chunks…')
            pending[0:0] = [(part, depth + 1) for part in smaller]
            continue
        completed += 1
        successful_batches.append(batch)
        valid_ids = {p['id'] for p in batch}
        for proposal in result.characters:
            if not proposal.name.strip() or not proposal.paragraph_ids or not set(proposal.paragraph_ids) <= valid_ids:
                raise ValueError('AI returned an invalid character name or paragraph ID')
            if _key(proposal.name) in PRONOUNS:
                continue
            proposal.aliases = [a for a in proposal.aliases if _key(a) not in PRONOUNS]
            names = {_key(n) for n in [proposal.name, *proposal.aliases] if n.strip()}
            matches = [r for r in catalog if names & {_key(n) for n in [r['name'], *r['aliases']]}]
            if len(matches) > 1:
                raise ValueError(f'Ambiguous character identity: {proposal.name}')
            record = matches[0] if matches else {'name': proposal.name.strip(), 'aliases': [], 'ids': set(),
                                                'details': {field: [] for field in APPEARANCE_FIELDS}}
            if not matches:
                catalog.append(record)
            record['aliases'] = list({_key(n): n.strip() for n in [*record['aliases'], proposal.name, *proposal.aliases]
                                      if n.strip() and _key(n) != _key(record['name'])}.values())
            record['ids'].update(proposal.paragraph_ids)
            for field in APPEARANCE_FIELDS:
                value = getattr(proposal, field).strip()
                if value and value not in record['details'][field]:
                    record['details'][field].append(value)
        context = ('\n\n'.join(p['prose'] for p in batch))[-settings.overlap_chars:] if settings.overlap_chars else ''
    # A separate, compact task counts participation rather than physical details.
    # It sees the complete identity catalog, including names introduced later.
    if catalog:
        _resolve_mentions(successful_batches, paragraphs, catalog, provider, settings, check, progress)
    check()
    existing = library.entities('Character')
    digest = source_hash(text)
    with library.transaction() as con:
        for entity in existing:
            entity.character_paragraphs = []
            con.execute('UPDATE visual_entity SET data_json=? WHERE identifier=?', (entity.model_dump_json(), entity.identifier))
        for record in catalog:
            if len(record['ids']) < 2:
                continue
            names = {_key(n) for n in [record['name'], *record['aliases']]}
            matches = [e for e in existing if names & {_key(n) for n in [e.name, *e.aliases]}]
            if len(matches) > 1:
                raise ValueError(f'Ambiguous saved character: {record["name"]}')
            entity = matches[0].model_copy(deep=True) if matches else VisualEntity(name=record['name'], entity_type='Character')
            entity.character_paragraphs = sorted(record['ids'])
            entity.source_hash = digest
            entity.recurring = True
            evidence = [SourceEvidence.model_validate_json(row[0]) for row in con.execute(
                'SELECT data_json FROM entity_evidence WHERE entity_id=?', (entity.identifier,))]
            protected = {e.field for e in evidence if e.kind == 'user' or e.status == 'Approved'}
            for field, values in record['details'].items():
                if field not in protected and field not in entity.character_edited_fields and entity.review_status != 'Approved':
                    setattr(entity, field, '; '.join(values))
            entity.aliases = list({_key(n): n for n in [*entity.aliases, record['name'], *record['aliases']]
                                   if _key(n) != _key(entity.name)}.values())
            con.execute('INSERT INTO visual_entity VALUES(?,?,?,?,?,?) ON CONFLICT(identifier) DO UPDATE SET '
                'recurring=excluded.recurring, data_json=excluded.data_json',
                (entity.identifier, entity.name, entity.entity_type, entity.review_status, 1, entity.model_dump_json()))
            con.executemany('INSERT OR IGNORE INTO entity_alias VALUES(?,?)', [(entity.identifier, a) for a in entity.aliases])
            for field, values in record['details'].items():
                value = '; '.join(values)
                if value and not any(e.field == field and e.value == value and e.source_hash == digest for e in evidence):
                    suggestion = SourceEvidence(field=field, value=value, source_hash=digest, kind='ai_proposed')
                    con.execute('INSERT INTO entity_evidence VALUES(?,?,?,?)',
                        (suggestion.identifier, entity.identifier, None, suggestion.model_dump_json()))
            library._audit(con, 'characters_generated', entity_id=entity.identifier, source_hash=digest)
    return listed_characters(library)


def _identity_context(record, paragraphs):
    # Keep the most recent grounded passage, so an identity survives a long run
    # of pronouns even when its name is outside the raw overlap window.
    latest = max(record['ids']) if record['ids'] else None
    return {'name': record['name'], 'aliases': record['aliases'],
            'last_appearance_paragraph_id': latest,
            'last_appearance_excerpt': paragraphs[latest - 1][:600] if latest else ''}


def _resolve_mentions(batches, paragraphs, catalog, provider, settings, check, progress):
    pending = [(batch, 0) for batch in batches]
    context = ''
    completed = 0
    by_name = {_key(r['name']): r for r in catalog}
    while pending:
        batch, depth = pending.pop(0)
        check()
        progress(f'Resolving character pronouns: {completed + 1}/{completed + len(pending) + 1}')
        first_id = batch[0]['id']
        identities = []
        for record in catalog:
            # Anchor each identity to its closest known appearance before this
            # batch; use its first appearance when it is introduced later.
            prior = [i for i in record['ids'] if i < first_id]
            anchor = max(prior) if prior else min(record['ids'])
            identities.append({'name': record['name'], 'aliases': record['aliases'],
                'anchor_paragraph_id': anchor, 'anchor_excerpt': paragraphs[anchor - 1][:600]})
        try:
            result = CharacterMentions.model_validate(provider.resolve_character_mentions(json.dumps({
                'paragraphs': batch, 'preceding_context_only': context,
                'known_characters': identities}, ensure_ascii=False)))
        except AIResponseError as exc:
            check()
            if not exc.retry_smaller or depth >= 3:
                raise
            if len(batch) > 1:
                middle = len(batch) // 2
                parts = [batch[:middle], batch[middle:]]
            elif len(batch[0]['prose']) >= 1000:
                passage = batch[0]; middle = len(passage['prose']) // 2
                parts = [[{'id': passage['id'], 'prose': passage['prose'][:middle]}],
                         [{'id': passage['id'], 'prose': passage['prose'][middle:]}]]
            else:
                raise
            progress('Pronoun analysis output was incomplete; retrying smaller chunks…')
            pending[0:0] = [(part, depth + 1) for part in parts]
            continue
        valid_ids = {p['id'] for p in batch}
        for mention in result.characters:
            record = by_name.get(_key(mention.name))
            if record is None or not set(mention.paragraph_ids) <= valid_ids:
                raise ValueError('Pronoun analysis returned an unknown character or paragraph ID')
            record['ids'].update(mention.paragraph_ids)
        completed += 1
        # Retain a small context even when scene-overlap is disabled: this is
        # identity resolution, not scene boundary detection.
        context = '\n\n'.join(p['prose'] for p in batch)[-max(600, settings.overlap_chars):]
