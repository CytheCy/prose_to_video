"""Transactional image records and safe project-local image files."""
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import closing
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize, Qt
from PySide6.QtGui import QImageReader
from prose_to_video.models.generated_image import GeneratedImage
from prose_to_video.models.image_job import ImageGenerationSettings, ImageJob, ProviderImage
from prose_to_video.storage.database import ProjectDatabase


class ImageStore:
    def __init__(self, root):
        self.root=Path(root)
        self.db=ProjectDatabase(self.root/'project.sqlite')

    def settings(self):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            row=con.execute('SELECT data_json FROM image_generation_settings WHERE singleton=1').fetchone()
            if row:return ImageGenerationSettings.model_validate_json(row[0])
            video=self.db.load().settings
            scale=min(1536/video.width,1536/video.height)
            width=max(64,round(video.width*scale/16)*16)
            height=max(64,round(video.height*scale/16)*16)
            return ImageGenerationSettings(width=width,height=height)

    def save_settings(self, value):
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('INSERT OR REPLACE INTO image_generation_settings VALUES(1,?)',(value.model_dump_json(),))

    def save_job(self, job):
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            if not con.execute('SELECT 1 FROM image_prompt WHERE identifier=?',(job.prompt_id,)).fetchone():
                raise ValueError('Approved prompt version is missing')
            con.execute('INSERT INTO image_job VALUES(?,?,?,?,?,?,?,?,?)',
                (job.identifier,job.project_id,job.scene_id,job.shot_id,job.prompt_id,job.status,
                 job.provider_job_id,job.submitted_at.isoformat() if job.submitted_at else None,job.model_dump_json()))

    def update_job(self, job):
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            old=con.execute('SELECT status FROM image_job WHERE identifier=?',(job.identifier,)).fetchone()
            if not old: raise ValueError('Image job does not exist')
            allowed={'Pending':{'Submitted','Running','Completed','Failed','Canceled','Needs Reconciliation'},
                     'Submitted':{'Running','Completed','Failed','Canceled','Needs Reconciliation'},
                     'Running':{'Completed','Failed','Canceled','Needs Reconciliation'},
                     'Needs Reconciliation':{'Running','Completed','Failed','Canceled'},
                     'Failed':{'Pending'}, 'Completed':set(), 'Canceled':set()}
            if job.status != old[0] and job.status not in allowed[old[0]]:
                raise ValueError(f'Invalid image job transition: {old[0]} → {job.status}')
            con.execute('UPDATE image_job SET status=?,provider_job_id=?,submitted_at=?,data_json=? WHERE identifier=?',
                (job.status,job.provider_job_id,job.submitted_at.isoformat() if job.submitted_at else None,
                 job.model_dump_json(),job.identifier))

    def job(self, identifier):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            row=con.execute('SELECT data_json FROM image_job WHERE identifier=?',(identifier,)).fetchone()
            return ImageJob.model_validate_json(row[0]) if row else None

    def jobs(self, status=None):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            query='SELECT data_json FROM image_job' + (' WHERE status=?' if status else '') + ' ORDER BY rowid'
            return [ImageJob.model_validate_json(r[0]) for r in con.execute(query,(status,) if status else ())]

    def assets(self, shot_id=None):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            query='SELECT data_json FROM generated_image' + (' WHERE shot_id=?' if shot_id else '') + ' ORDER BY rowid DESC'
            return [GeneratedImage.model_validate_json(r[0]) for r in con.execute(query,(shot_id,) if shot_id else ())]

    def asset(self, identifier):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            row=con.execute('SELECT data_json FROM generated_image WHERE identifier=?',(identifier,)).fetchone()
            return GeneratedImage.model_validate_json(row[0]) if row else None

    def approved(self, shot_id):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            row=con.execute('SELECT g.data_json,a.locked FROM approved_image a JOIN generated_image g ON a.asset_id=g.identifier WHERE a.shot_id=?',(shot_id,)).fetchone()
            return (GeneratedImage.model_validate_json(row[0]),bool(row[1])) if row else None

    def approved_map(self):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            return {row['shot_id']:(GeneratedImage.model_validate_json(row['data_json']),bool(row['locked']))
                for row in con.execute('SELECT a.shot_id,g.data_json,a.locked FROM approved_image a '
                                       'JOIN generated_image g ON a.asset_id=g.identifier')}

    def approve(self, asset_id, *, lock=False):
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            row=con.execute('SELECT data_json FROM generated_image WHERE identifier=?',(asset_id,)).fetchone()
            if not row: raise ValueError('Image candidate does not exist')
            asset=GeneratedImage.model_validate_json(row[0])
            path=self.root/asset.path
            if not path.is_file(): raise ValueError('Image file is missing')
            if hashlib.sha256(path.read_bytes()).hexdigest()!=asset.sha256:
                raise ValueError('Image file integrity check failed')
            old=con.execute('SELECT g.data_json,a.locked FROM approved_image a JOIN generated_image g ON a.asset_id=g.identifier WHERE a.shot_id=?',(asset.shot_id,)).fetchone()
            if old and old[1] and GeneratedImage.model_validate_json(old[0]).identifier != asset_id:
                raise ValueError('Unlock the approved image before replacing it')
            if old and GeneratedImage.model_validate_json(old[0]).identifier != asset_id:
                previous=GeneratedImage.model_validate_json(old[0]); previous.approval_status='Historical'
                con.execute('UPDATE generated_image SET approval_status=?,data_json=? WHERE identifier=?',
                    (previous.approval_status,previous.model_dump_json(),previous.identifier))
            asset.approval_status='Approved'
            con.execute('UPDATE generated_image SET approval_status=?,data_json=? WHERE identifier=?',
                (asset.approval_status,asset.model_dump_json(),asset_id))
            con.execute('INSERT OR REPLACE INTO approved_image VALUES(?,?,?)',(asset.shot_id,asset_id,int(lock)))
        return asset

    def lock(self, shot_id, locked=True):
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            if not con.execute('SELECT 1 FROM approved_image WHERE shot_id=?',(shot_id,)).fetchone():
                raise ValueError('No approved image to lock')
            con.execute('UPDATE approved_image SET locked=? WHERE shot_id=?',(int(locked),shot_id))

    def reject(self, asset_id):
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            row=con.execute('SELECT data_json FROM generated_image WHERE identifier=?',(asset_id,)).fetchone()
            if not row: raise ValueError('Image candidate does not exist')
            asset=GeneratedImage.model_validate_json(row[0])
            if con.execute('SELECT 1 FROM approved_image WHERE asset_id=?',(asset_id,)).fetchone():
                raise ValueError('Approved image cannot be rejected until another is selected')
            asset.approval_status='Rejected'
            con.execute('UPDATE generated_image SET approval_status=?,data_json=? WHERE identifier=?',
                        ('Rejected',asset.model_dump_json(),asset_id))

    def save_image(self, job, output: ProviderImage, number, *, parent_id=None):
        if parent_id:
            parent=self.asset(parent_id)
            if not parent or parent.shot_id!=job.shot_id:raise ValueError('Invalid parent image')
        if len(output.data)<100: raise ValueError('Generated image is empty or unusually small')
        if len(output.data)>100_000_000: raise ValueError('Generated image exceeds 100 MB')
        reader=QImageReader(); reader.setDecideFormatFromContent(True)
        buffer=QBuffer(); buffer.setData(QByteArray(output.data)); buffer.open(QIODevice.ReadOnly)
        reader.setDevice(buffer)
        fmt=bytes(reader.format()).decode('ascii','ignore').lower()
        if fmt=='jpg': fmt='jpeg'
        if fmt not in {'png','jpeg','webp'} or not reader.canRead(): raise ValueError('Corrupted or unsupported generated image')
        image=reader.read()
        if image.isNull(): raise ValueError('Generated image cannot be decoded')
        width,height=image.width(),image.height()
        warnings=[]
        if (width,height)!=(job.width,job.height): warnings.append(f'Actual dimensions {width}×{height} differ from requested {job.width}×{job.height}')
        digest=hashlib.sha256(output.data).hexdigest()
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            if con.execute('SELECT 1 FROM generated_image WHERE hash=? LIMIT 1',(digest,)).fetchone():
                warnings.append('Duplicate image content hash')
        asset_id=str(uuid4())
        directory=self.root/'images'/f'scene_{job.scene_id}'/f'shot_{job.shot_id}'
        directory.mkdir(parents=True,exist_ok=True)
        rel=f'images/scene_{job.scene_id}/shot_{job.shot_id}/{asset_id}.{fmt if fmt != "jpeg" else "jpg"}'
        thumb_rel=f'images/scene_{job.scene_id}/shot_{job.shot_id}/{asset_id}.thumb.png'
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(dir=directory,prefix='.image-',delete=False) as stream:
                temporary=Path(stream.name); stream.write(output.data); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary,self.root/rel); temporary=None
            thumbnail=image.scaled(QSize(240,160),Qt.KeepAspectRatio,Qt.SmoothTransformation)
            if not thumbnail.save(str(self.root/thumb_rel),'PNG'): raise OSError('Could not create image thumbnail')
            asset=GeneratedImage(identifier=asset_id,job_id=job.identifier,shot_id=job.shot_id,
                path=rel,thumbnail_path=thumb_rel,format=fmt,width=width,height=height,sha256=digest,
                candidate_number=number,provider=job.provider,model=output.model,seed=output.seed,
                prompt_id=job.prompt_id,dependency_fingerprint=job.dependency_fingerprint,
                parent_id=parent_id,warnings=warnings)
            with closing(self.db._connect()) as con, con:
                self.db._check_schema(con)
                con.execute('INSERT INTO generated_image VALUES(?,?,?,?,?,?,?,?)',
                    (asset.identifier,asset.job_id,asset.shot_id,asset.parent_id,asset.path,asset.sha256,
                     asset.approval_status,asset.model_dump_json()))
            return asset
        except Exception:
            (self.root/rel).unlink(missing_ok=True); (self.root/thumb_rel).unlink(missing_ok=True)
            raise
        finally:
            if temporary: temporary.unlink(missing_ok=True)

    def recover(self):
        changed=[]
        for job in self.jobs():
            if job.status in {'Submitted','Running'}:
                job.status='Needs Reconciliation'; self.update_job(job); changed.append(job)
        return changed

    def missing_assets(self):
        return [a for a in self.assets() if not (self.root/a.path).is_file()]

    def totals(self):
        jobs=self.jobs()
        return {'images':len(self.assets()),'completed':sum(j.status=='Completed' for j in jobs),
                'failed':sum(j.status=='Failed' for j in jobs),
                'pending':sum(j.status in {'Pending','Submitted','Running','Needs Reconciliation'} for j in jobs),
                'actual_cost':sum(j.actual_cost or 0 for j in jobs),
                'actual_cost_count':sum(j.actual_cost is not None for j in jobs),
                'estimated_cost':sum(j.estimated_cost or 0 for j in jobs),
                'estimated_cost_count':sum(j.estimated_cost is not None for j in jobs)}
