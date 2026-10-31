"""Transactional Stage Ten queue, profiles, issues and decisions."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from prose_to_video.models.production import ProductionProfile, ProductionRun, ProductionTask, QualityIssue
from prose_to_video.storage.database import ProjectDatabase


class ProductionStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.db = ProjectDatabase(self.root / 'project.sqlite')

    def save_profile(self, profile: ProductionProfile):
        project_id = self.db.load().identifier
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('INSERT OR REPLACE INTO production_profile VALUES(?,?,?,?)',
                        (profile.identifier, profile.name, project_id, profile.model_dump_json()))

    def profiles(self):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            return [ProductionProfile.model_validate_json(r[0]) for r in con.execute(
                'SELECT data_json FROM production_profile ORDER BY name')]

    def save_run(self, run: ProductionRun):
        run.updated_at = datetime.now(timezone.utc)
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            rows = con.execute('SELECT status,COUNT(*) FROM production_task WHERE run_id=? GROUP BY status',
                               (run.identifier,)).fetchall()
            counts = {r[0]: r[1] for r in rows}
            run.completed_task_count = counts.get('Completed', 0) + counts.get('Reused', 0)
            run.pending_task_count = counts.get('Pending', 0) + counts.get('Ready', 0)
            run.failed_task_count = counts.get('Failed', 0)
            run.blocked_task_count = counts.get('Blocked', 0) + counts.get('Awaiting Approval', 0)
            run.estimated_remaining_work = sum(counts.get(s, 0) for s in
                                                ('Pending', 'Ready', 'Running', 'Awaiting Approval', 'Blocked'))
            con.execute('INSERT INTO production_run VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(identifier) DO UPDATE SET '
                        'status=excluded.status,current_stage=excluded.current_stage,updated_at=excluded.updated_at,data_json=excluded.data_json',
                        (run.identifier, run.project_id, run.profile.identifier, run.status, run.current_stage,
                         run.started_at.isoformat(), run.updated_at.isoformat(), run.model_dump_json()))

    def run(self, identifier):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            row = con.execute('SELECT data_json FROM production_run WHERE identifier=?', (identifier,)).fetchone()
            return ProductionRun.model_validate_json(row[0]) if row else None

    def runs(self):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            return [ProductionRun.model_validate_json(r[0]) for r in con.execute(
                'SELECT data_json FROM production_run ORDER BY started_at DESC')]

    def save_graph(self, run: ProductionRun, tasks: list[ProductionTask]):
        """Insert a complete graph atomically; completed history is never replaced."""
        ids = {task.identifier for task in tasks}
        if len(ids) != len(tasks) or any(set(t.dependencies) - ids for t in tasks):
            raise ValueError('Invalid task dependency graph')
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            if con.execute('SELECT 1 FROM production_run WHERE identifier=?', (run.identifier,)).fetchone():
                raise ValueError('Production run already exists')
            con.execute('INSERT INTO production_run VALUES(?,?,?,?,?,?,?,?)',
                        (run.identifier, run.project_id, run.profile.identifier, run.status, run.current_stage,
                         run.started_at.isoformat(), run.updated_at.isoformat(), run.model_dump_json()))
            for task in tasks:
                task.run_id = run.identifier
                con.execute('INSERT INTO production_task VALUES(?,?,?,?,?,?,?)',
                            (task.identifier, run.identifier, task.kind, task.shot_id or task.scene_id,
                             task.fingerprint, task.status, task.model_dump_json()))
            con.executemany('INSERT INTO production_dependency VALUES(?,?)',
                            [(t.identifier, d) for t in tasks for d in t.dependencies])
        self.save_run(run)

    def tasks(self, run_id):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            return [ProductionTask.model_validate_json(r[0]) for r in con.execute(
                'SELECT data_json FROM production_task WHERE run_id=? ORDER BY rowid', (run_id,))]

    def add_tasks(self, run_id: str, tasks: list[ProductionTask]):
        if not tasks:
            return
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            existing = {r[0] for r in con.execute('SELECT identifier FROM production_task WHERE run_id=?', (run_id,))}
            incoming = {t.identifier for t in tasks}
            if len(incoming) != len(tasks) or any(set(t.dependencies) - existing - incoming for t in tasks):
                raise ValueError('Invalid expanded dependency graph')
            for task in tasks:
                task.run_id = run_id
                con.execute('INSERT INTO production_task VALUES(?,?,?,?,?,?,?)',
                    (task.identifier, run_id, task.kind, task.shot_id or task.scene_id,
                     task.fingerprint, task.status, task.model_dump_json()))
            con.executemany('INSERT INTO production_dependency VALUES(?,?)',
                            [(t.identifier, d) for t in tasks for d in t.dependencies])

    def sync_graph(self, run_id: str, tasks: list[ProductionTask]):
        """Expand dependencies and invalidate only tasks whose inputs changed."""
        ids = {t.identifier for t in tasks}
        if len(ids) != len(tasks) or any(set(t.dependencies) - ids for t in tasks):
            raise ValueError('Invalid production graph')
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            existing = {r['identifier']: ProductionTask.model_validate_json(r['data_json'])
                        for r in con.execute('SELECT identifier,data_json FROM production_task WHERE run_id=?', (run_id,))}
            for candidate in tasks:
                candidate.run_id = run_id
                old = existing.get(candidate.identifier)
                if old:
                    changed = old.fingerprint != candidate.fingerprint or set(old.dependencies) != set(candidate.dependencies)
                    if old.status in {'Running', 'Interrupted', 'Awaiting Approval'}:
                        continue
                    if changed:
                        old.fingerprint = candidate.fingerprint
                        old.inputs = candidate.inputs
                        old.dependencies = candidate.dependencies
                        old.output_ids = []
                        old.status = candidate.status
                        old.error = None
                    elif old.status == 'Reused' and candidate.status in {'Pending','Awaiting Approval'}:
                        old.status = candidate.status
                    elif old.status == 'Pending' and candidate.status == 'Reused':
                        old.status = 'Reused'
                    elif old.status == 'Pending' and candidate.status == 'Awaiting Approval':
                        old.status = 'Awaiting Approval'
                    con.execute('UPDATE production_task SET fingerprint=?,status=?,data_json=? WHERE identifier=?',
                                (old.fingerprint, old.status, old.model_dump_json(), old.identifier))
                    con.execute('DELETE FROM production_dependency WHERE task_id=?', (old.identifier,))
                    con.executemany('INSERT INTO production_dependency VALUES(?,?)',
                                    [(old.identifier, d) for d in old.dependencies])
                else:
                    con.execute('INSERT INTO production_task VALUES(?,?,?,?,?,?,?)',
                                (candidate.identifier, run_id, candidate.kind,
                                 candidate.shot_id or candidate.scene_id, candidate.fingerprint,
                                 candidate.status, candidate.model_dump_json()))
            # All tasks exist before edge insertion, so the FK checks remain valid.
            for candidate in tasks:
                if candidate.identifier not in existing:
                    con.executemany('INSERT INTO production_dependency VALUES(?,?)',
                                    [(candidate.identifier, d) for d in candidate.dependencies])

    def save_task(self, task: ProductionTask):
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('UPDATE production_task SET status=?,data_json=? WHERE identifier=?',
                        (task.status, task.model_dump_json(), task.identifier))
            if con.total_changes != 1:
                raise ValueError('Production task does not exist')

    def decide(self, run_id: str, task_id: str | None, checkpoint: str, origin: str, policy: str = ''):
        if origin not in {'Manual', 'Automatic'}:
            raise ValueError('Approval origin must be Manual or Automatic')
        identifier = str(uuid4())
        payload = {'task_id': task_id, 'checkpoint': checkpoint, 'origin': origin, 'policy': policy}
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('INSERT INTO production_checkpoint VALUES(?,?,?,?,?,?,?,?)',
                        (identifier, run_id, task_id, checkpoint, origin, policy,
                         datetime.now(timezone.utc).isoformat(), json.dumps(payload)))

    def decisions(self, run_id: str):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            return [dict(r) for r in con.execute('SELECT task_id,checkpoint,origin,policy,decided_at '
                                                  'FROM production_checkpoint WHERE run_id=?', (run_id,))]

    def save_issue(self, issue: QualityIssue):
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('INSERT INTO production_issue VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(identifier) DO UPDATE SET '
                        'status=excluded.status,data_json=excluded.data_json',
                        (issue.identifier, issue.run_id, issue.project_id, issue.scene_id, issue.shot_id,
                         issue.asset_id, issue.category, issue.severity, issue.status,
                         issue.created_at.isoformat(), issue.model_dump_json()))

    def issues(self, *, status=None, severity=None, category=None, scene_id=None):
        clauses, values = [], []
        for column, value in [('status',status), ('severity',severity), ('category',category), ('scene_id',scene_id)]:
            if value is not None:
                clauses.append(column+'=?'); values.append(value)
        query = 'SELECT data_json FROM production_issue' + (' WHERE '+' AND '.join(clauses) if clauses else '') + ' ORDER BY created_at DESC'
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            return [QualityIssue.model_validate_json(r[0]) for r in con.execute(query, values)]

    def save_manifest(self, run_id: str, path: str, data: dict):
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('INSERT INTO production_manifest VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET '
                        'path=excluded.path,data_json=excluded.data_json',
                        (run_id, path, json.dumps(data, sort_keys=True)))

    def record_usage(self, run_id: str, category: str, amount: float | None,
                     *, estimated=False, details=None):
        payload = {'category': category, 'amount': amount, 'estimated': estimated,
                   'details': details or {}}
        identifier = (str(uuid5(NAMESPACE_URL, f'{run_id}:{category}:{details["job_id"]}'))
                      if details and details.get('job_id') else str(uuid4()))
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('INSERT INTO production_usage VALUES(?,?,?,?,?,?) ON CONFLICT(identifier) DO UPDATE SET '
                        'amount=excluded.amount,estimated=excluded.estimated,data_json=excluded.data_json',
                (identifier, run_id, category, amount, int(estimated), json.dumps(payload)))

    def usage(self, run_id: str):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            return [dict(r) for r in con.execute('SELECT category,amount,estimated,data_json '
                                                'FROM production_usage WHERE run_id=?', (run_id,))]

    def record_fallback(self, run_id: str, task_id: str, from_mode: str, to_mode: str,
                        reason: str):
        payload = {'from_mode': from_mode, 'to_mode': to_mode, 'reason': reason}
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('INSERT INTO production_fallback VALUES(?,?,?,?)',
                        (str(uuid4()), run_id, task_id, json.dumps(payload)))

    def fallbacks(self, run_id: str):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            return [json.loads(r[0]) for r in con.execute('SELECT data_json FROM production_fallback '
                                                     'WHERE run_id=? ORDER BY rowid', (run_id,))]

    def recover(self):
        """Leave submitted paid jobs for their provider queues to reconcile."""
        recovered = []
        for run in self.runs():
            if run.status not in {'Preparing', 'Running'}:
                continue
            run.status = 'Interrupted'
            self.save_run(run)
            for task in self.tasks(run.identifier):
                if task.status == 'Running':
                    task.status = 'Interrupted'
                    task.error = 'Application stopped; reconcile any submitted provider job before retrying'
                    self.save_task(task)
            recovered.append(run)
        return recovered
