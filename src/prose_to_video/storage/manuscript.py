"""Persist review decisions independently of immutable source and production assets."""
from __future__ import annotations

from contextlib import closing

from prose_to_video.models.scene import source_hash
from prose_to_video.pipeline.manuscript import CLASSIFICATIONS, apply_decisions, parse_manuscript
from prose_to_video.storage.database import ProjectDatabase


class ManuscriptStore:
    def __init__(self, database: ProjectDatabase):
        self.database = database

    def sections(self, text: str):
        digest = source_hash(text)
        with closing(self.database._connect()) as con:
            self.database._check_schema(con)
            decisions = {row["section_id"]: dict(row) for row in con.execute(
                "SELECT * FROM manuscript_decision WHERE source_hash=?", (digest,))}
        return apply_decisions(parse_manuscript(text), decisions)

    def decide(self, text: str, section_id: str, classification: str, included: bool, review: bool):
        if classification not in CLASSIFICATIONS:
            raise ValueError("Invalid manuscript classification")
        if section_id not in {s.identifier for s in parse_manuscript(text)}:
            raise ValueError("Section is not part of the current manuscript")
        with closing(self.database._connect()) as con, con:
            self.database._check_schema(con)
            con.execute("INSERT INTO manuscript_decision VALUES(?,?,?,?,?) ON CONFLICT(source_hash,section_id) "
                "DO UPDATE SET classification=excluded.classification,included=excluded.included,review=excluded.review",
                (source_hash(text), section_id, classification, int(included), int(review)))
