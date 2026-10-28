"""Transactional motion prompts, persistent video jobs, and portable clip assets."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path
from uuid import uuid4
from prose_to_video.models.generated_video import MotionPrompt, GeneratedVideo, now
from prose_to_video.models.video_job import VideoGenerationSettings, VideoJob, DurationCoverage
from prose_to_video.storage.database import ProjectDatabase

class VideoStore:
    def __init__(self, root):
        self.root=Path(root)
        self.db=ProjectDatabase(self.root/'project.sqlite')

    def settings(self):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            row=con.execute('SELECT data_json FROM video_generation_settings WHERE singleton=1').fetchone()
            return VideoGenerationSettings.model_validate_json(row[0]) if row else VideoGenerationSettings()

    def save_settings(self, settings):
        settings=VideoGenerationSettings.model_validate(settings.model_dump())
        if settings.workflow_path:
            source=Path(settings.workflow_path)
            if not source.is_absolute():source=self.root/source
            if not source.is_file():raise ValueError('Video workflow JSON is missing')
            workflow=json.loads(source.read_text(encoding='utf-8'))
            if not isinstance(workflow,dict):raise ValueError('Video workflow must be an API-format JSON object')
            data=source.read_bytes();digest=hashlib.sha256(data).hexdigest()
            directory=self.root/'storyboard'/'video_workflows';directory.mkdir(parents=True,exist_ok=True)
            target=directory/f'{digest}.json'
            if source.resolve()!=target.resolve() and not target.exists():target.write_bytes(data)
            settings.workflow_path=target.relative_to(self.root).as_posix()
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('INSERT OR REPLACE INTO video_generation_settings VALUES(1,?)',(settings.model_dump_json(),))
        return settings

    def production(self, shot_id):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            row=con.execute('SELECT mode,coverage_json,draft_fallback FROM shot_production WHERE shot_id=?',(shot_id,)).fetchone()
            return (row[0],json.loads(row[1]),bool(row[2])) if row else ('Camera Motion',{},False)

    def set_production(self, shot_id, mode, coverage=None, draft_fallback=False):
        if mode not in {'Static Image','Camera Motion','AI Animation'}:raise ValueError('Unknown shot production mode')
        policy=DurationCoverage.model_validate(coverage or {})
        if mode=='AI Animation' and policy.strategy in {'still_tail','camera_tail'}:
            from prose_to_video.storage.generated_images import ImageStore
            approved=ImageStore(self.root).approved(shot_id)
            if not approved or approved[0].identifier!=policy.tail_image_id:
                raise ValueError('Tail image must be the approved still for this shot')
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            if not con.execute('SELECT 1 FROM shot s JOIN active_storyboard a ON s.run_id=a.run_id WHERE s.identifier=?',
                               (shot_id,)).fetchone():
                raise ValueError('Shot production mode needs an active storyboard shot')
            con.execute('INSERT OR REPLACE INTO shot_production VALUES(?,?,?,?)',
                (shot_id,mode,policy.model_dump_json(),int(draft_fallback)))

    def prompts(self, shot_id):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            return [MotionPrompt.model_validate_json(r[0]) for r in con.execute(
                'SELECT data_json FROM motion_prompt WHERE shot_id=? ORDER BY version DESC',(shot_id,))]

    def prompt(self, prompt_id):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            row=con.execute('SELECT data_json FROM motion_prompt WHERE identifier=?',(prompt_id,)).fetchone()
            return MotionPrompt.model_validate_json(row[0]) if row else None

    def approved_prompt(self, shot_id):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            row=con.execute('SELECT p.data_json FROM motion_prompt p JOIN approved_motion_prompt a ON p.identifier=a.prompt_id WHERE a.shot_id=?',(shot_id,)).fetchone()
            return MotionPrompt.model_validate_json(row[0]) if row else None

    def save_prompt(self, prompt):
        board=self.db.load_storyboard(prompt.scene_id)
        if not board or not any(s.identifier==prompt.shot_id for s in board.shots):
            raise ValueError('Motion prompt shot is missing from active storyboard')
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            version=con.execute('SELECT MAX(version) FROM motion_prompt WHERE shot_id=?',(prompt.shot_id,)).fetchone()[0]
            if prompt.version!=(version or 0)+1:raise ValueError('Motion prompt version must follow history')
            con.execute('INSERT INTO motion_prompt VALUES(?,?,?,?,?,?)',
                (prompt.identifier,prompt.shot_id,prompt.scene_id,prompt.version,prompt.review_status,prompt.model_dump_json()))
        return prompt

    def edit_prompt(self, prompt_id, **changes):
        old=self.prompt(prompt_id)
        if not old:raise ValueError('Motion prompt does not exist')
        from prose_to_video.storage.generated_images import ImageStore
        edited=old.model_copy(update={**changes,'identifier':str(uuid4()),'version':self.prompts(old.shot_id)[0].version+1,
            'review_status':'Draft','created_at':now(),'modified_at':now()})
        from prose_to_video.pipeline.motion_prompt_generation import source_fingerprint, resolve_ending_image, motion_references, workflow_fingerprint
        board=self.db.load_storyboard(old.scene_id)
        shot=next(s for s in board.shots if s.identifier==old.shot_id)
        image=ImageStore(self.root).asset(edited.starting_image_id)
        ending=resolve_ending_image(self.root,edited.ending_image_id)
        settings=self.settings();refs=motion_references(self.root,shot.identifier,settings.use_references)
        if edited.ending_image_id and not settings.use_last_frame:
            raise ValueError('Last-frame conditioning is disabled')
        if not image:raise ValueError('Starting image is missing')
        for asset in [image,*([ending] if ending else [])]:
            if not (self.root/asset.path).is_file() or hash_file(self.root/asset.path)!=asset.sha256:
                raise ValueError('Starting or ending image is missing or changed')
        for ref in refs:
            if not (self.root/ref['path']).is_file() or hash_file(self.root/ref['path'])!=ref['sha256']:
                raise ValueError('Continuity reference is missing or changed')
        edited.dependency_fingerprint=source_fingerprint(shot,image,ending,settings,
            [(r['identifier'],r['sha256']) for r in refs],workflow_fingerprint(self.root,settings))
        return self.save_prompt(MotionPrompt.model_validate(edited.model_dump()))

    def approve_prompt(self, prompt_id):
        prompt=self.prompt(prompt_id)
        if not prompt:raise ValueError('Motion prompt does not exist')
        from prose_to_video.pipeline.motion_prompt_generation import source_fingerprint, resolve_ending_image, motion_references, workflow_fingerprint
        from prose_to_video.storage.generated_images import ImageStore
        selected=ImageStore(self.root).approved(prompt.shot_id)
        if not selected or selected[0].identifier!=prompt.starting_image_id:
            raise ValueError('Approved starting image changed')
        ending=resolve_ending_image(self.root,prompt.ending_image_id)
        if prompt.ending_image_id and not ending:raise ValueError('Ending image is missing')
        if ending and abs(selected[0].width/selected[0].height-ending.width/ending.height)>.02:
            raise ValueError('Ending image aspect ratio differs')
        board=self.db.load_storyboard(prompt.scene_id)
        shot=next((s for s in board.shots if s.identifier==prompt.shot_id),None) if board else None
        if not shot:raise ValueError('Storyboard shot changed')
        settings=self.settings();refs=motion_references(self.root,shot.identifier,settings.use_references)
        if prompt.ending_image_id and not settings.use_last_frame:
            raise ValueError('Last-frame conditioning is disabled')
        for asset in [selected[0],*([ending] if ending else [])]:
            if not (self.root/asset.path).is_file() or hash_file(self.root/asset.path)!=asset.sha256:
                raise ValueError('Starting or ending image is missing or changed')
        for ref in refs:
            if not (self.root/ref['path']).is_file() or hash_file(self.root/ref['path'])!=ref['sha256']:
                raise ValueError('Continuity reference is missing or changed')
        current=source_fingerprint(shot,selected[0],ending,settings,
            [(r['identifier'],r['sha256']) for r in refs],workflow_fingerprint(self.root,settings))
        if current!=prompt.dependency_fingerprint:raise ValueError('Motion prompt dependencies changed')
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            prompt.review_status='Approved';prompt.modified_at=now()
            con.execute('UPDATE motion_prompt SET status=?,data_json=? WHERE identifier=?',
                ('Approved',prompt.model_dump_json(),prompt_id))
            con.execute('INSERT OR REPLACE INTO approved_motion_prompt VALUES(?,?)',(prompt.shot_id,prompt_id))
        return prompt

    def save_job(self, job):
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            if not con.execute('SELECT 1 FROM motion_prompt WHERE identifier=? AND shot_id=?',(job.prompt_id,job.shot_id)).fetchone():
                raise ValueError('Motion prompt version is missing')
            con.execute('INSERT INTO video_job VALUES(?,?,?,?,?,?)',
                (job.identifier,job.shot_id,job.prompt_id,job.status,job.provider_job_id,job.model_dump_json()))

    def update_job(self, job):
        allowed={'Pending':{'Submitted','Canceled','Failed'},'Submitted':{'Running','Completed','Failed','Canceled','Needs Reconciliation'},
                 'Running':{'Completed','Failed','Canceled','Needs Reconciliation'},
                 'Needs Reconciliation':{'Running','Completed','Failed','Canceled'},'Completed':set(),'Failed':set(),'Canceled':set()}
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            old=con.execute('SELECT status FROM video_job WHERE identifier=?',(job.identifier,)).fetchone()
            if not old:raise ValueError('Video job does not exist')
            if job.status!=old[0] and job.status not in allowed[old[0]]:raise ValueError('Invalid video job transition')
            con.execute('UPDATE video_job SET status=?,provider_job_id=?,data_json=? WHERE identifier=?',
                (job.status,job.provider_job_id,job.model_dump_json(),job.identifier))

    def jobs(self, shot_id=None):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            return [VideoJob.model_validate_json(r[0]) for r in con.execute('SELECT data_json FROM video_job'+
                (' WHERE shot_id=?' if shot_id else '')+' ORDER BY rowid',(shot_id,) if shot_id else ())]

    def job(self, identifier):
        return next((j for j in self.jobs() if j.identifier==identifier),None)

    def clips(self, shot_id=None):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            return [GeneratedVideo.model_validate_json(r[0]) for r in con.execute('SELECT data_json FROM generated_video'+
                (' WHERE shot_id=?' if shot_id else '')+' ORDER BY rowid DESC',(shot_id,) if shot_id else ())]

    def clip(self, identifier):
        return next((c for c in self.clips() if c.identifier==identifier),None)

    def approved(self, shot_id):
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            row=con.execute('SELECT clip_ids_json,locked FROM approved_video WHERE shot_id=?',(shot_id,)).fetchone()
            ids=[r[0] for r in con.execute('SELECT clip_id FROM approved_video_clip WHERE shot_id=? ORDER BY sequence',(shot_id,))]
        if not row:return ([],False)
        clips=[self.clip(cid) for cid in ids]
        return (clips,bool(row[1]))

    def approve(self, shot_id, clip_ids, lock=False):
        if not clip_ids or len(set(clip_ids))!=len(clip_ids):raise ValueError('Select unique clips')
        clips=[self.clip(cid) for cid in clip_ids]
        if any(c is None or c.shot_id!=shot_id or c.approval_status=='Rejected' for c in clips):raise ValueError('Invalid clip selection')
        for clip in clips:
            path=self.root/clip.path
            if not path.is_file() or hash_file(path)!=clip.sha256:raise ValueError('Clip file changed or is missing')
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            old=con.execute('SELECT clip_ids_json,locked FROM approved_video WHERE shot_id=?',(shot_id,)).fetchone()
            if old and old[1] and json.loads(old[0])!=clip_ids:raise ValueError('Unlock approved clips before replacing')
            if old and old[1]:lock=True
            if old:
                for previous_id in json.loads(old[0]):
                    if previous_id not in clip_ids:
                        row=con.execute('SELECT data_json FROM generated_video WHERE identifier=?',(previous_id,)).fetchone()
                        if row:
                            previous=GeneratedVideo.model_validate_json(row[0]);previous.approval_status='Historical'
                            con.execute('UPDATE generated_video SET status=?,data_json=? WHERE identifier=?',
                                ('Historical',previous.model_dump_json(),previous_id))
            for clip in clips:
                clip.approval_status='Approved'
                con.execute('UPDATE generated_video SET status=?,data_json=? WHERE identifier=?',
                    ('Approved',clip.model_dump_json(),clip.identifier))
            con.execute('INSERT OR REPLACE INTO approved_video VALUES(?,?,?)',(shot_id,json.dumps(clip_ids),int(lock)))
            con.execute('DELETE FROM approved_video_clip WHERE shot_id=?',(shot_id,))
            con.executemany('INSERT INTO approved_video_clip VALUES(?,?,?)',
                [(shot_id,index,clip_id) for index,clip_id in enumerate(clip_ids,1)])
            con.execute('INSERT INTO video_approval_history(shot_id,clip_ids_json,approved_at) VALUES(?,?,?)',
                (shot_id,json.dumps(clip_ids),now().isoformat()))

    def lock(self, shot_id, locked=True):
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            if not con.execute('SELECT 1 FROM approved_video WHERE shot_id=?',(shot_id,)).fetchone():raise ValueError('No approved clips')
            con.execute('UPDATE approved_video SET locked=? WHERE shot_id=?',(int(locked),shot_id))

    def reject(self, clip_id, notes='', issue=''):
        clip=self.clip(clip_id)
        if not clip:raise ValueError('Clip does not exist')
        if clip_id in [c.identifier for c in self.approved(clip.shot_id)[0]]:raise ValueError('Approved clip cannot be rejected')
        clip.approval_status='Rejected';clip.review_notes=notes
        if issue:clip.review_issues.append(issue)
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('UPDATE generated_video SET status=?,data_json=? WHERE identifier=?',
                ('Rejected',clip.model_dump_json(),clip_id))

    def save_video(self, job, output, candidate_number=1):
        suffix=Path(output.filename).suffix.lower()
        if suffix not in {'.mp4','.webm','.mov','.mkv'}:raise ValueError('Unsupported video container')
        if output.path:
            source=Path(output.path)
            if not source.is_file() or source.stat().st_size<1024:raise ValueError('Video output is empty')
        elif len(output.data)<1024:raise ValueError('Video output is empty')
        clip_id=str(uuid4())
        directory=(self.root/'video_clips'/f'scene_{job.scene_id}'/f'shot_{job.shot_id}').resolve()
        if not directory.is_relative_to((self.root/'video_clips').resolve()):
            raise ValueError('Unsafe video asset path')
        directory.mkdir(parents=True,exist_ok=True)
        rel=f'video_clips/scene_{job.scene_id}/shot_{job.shot_id}/{clip_id}{suffix}'
        poster_rel=f'video_clips/scene_{job.scene_id}/shot_{job.shot_id}/{clip_id}.poster.jpg'
        target=self.root/rel
        try:
            with tempfile.NamedTemporaryFile(dir=directory,prefix='.clip-',delete=False) as stream:
                temp=Path(stream.name)
                if output.path:
                    with Path(output.path).open('rb') as source:shutil.copyfileobj(source,stream,length=1024*1024)
                else:stream.write(output.data)
                stream.flush();os.fsync(stream.fileno())
            os.replace(temp,target)
            meta=validate_video(target)
            container=meta['format_name']
            families={'.mp4':('mp4','mov'),'.mov':('mov','mp4'),'.webm':('webm','matroska'),'.mkv':('matroska',)}
            if not any(name in container for name in families[suffix]):
                raise ValueError('Video container differs from filename extension')
            frame=subprocess.run(['ffmpeg','-v','error','-y','-i',str(target),'-map','0:v:0','-frames:v','1',str(self.root/poster_rel)],capture_output=True,text=True,timeout=90)
            if frame.returncode:raise ValueError(f'Could not extract clip poster: {frame.stderr[-500:]}')
            clip=GeneratedVideo(identifier=clip_id,job_id=job.identifier,shot_id=job.shot_id,path=rel,
                poster_path=poster_rel,sha256=hash_file(target),format=suffix.lstrip('.'),
                codec=meta['codec'],width=meta['width'],height=meta['height'],frame_rate=meta['frame_rate'],
                frame_count=meta['frame_count'],duration=meta['duration'],has_audio=meta['has_audio'],
                provider=job.provider,model=output.model or job.model,prompt_id=job.prompt_id,
                starting_image_id=job.starting_image_id,ending_image_id=job.ending_image_id,
                parent_id=job.parent_clip_id,candidate_number=candidate_number,
                dependency_fingerprint=job.dependency_fingerprint,
                warnings=(['Generated audio will be discarded in final render'] if meta['has_audio'] else [])+
                    ([f'Actual dimensions {meta["width"]}×{meta["height"]} differ from requested {job.requested_width}×{job.requested_height}']
                      if (meta['width'],meta['height'])!=(job.requested_width,job.requested_height) else [])+
                    ([f'Actual duration {meta["duration"]:.3f}s differs from requested {job.requested_duration:.3f}s']
                      if abs(meta['duration']-job.requested_duration)>.1 else [])+
                    ([f'Actual frame rate {meta["frame_rate"]:.3f} differs from requested {job.settings.get("frame_rate")}']
                      if job.settings.get('frame_rate') and abs(meta['frame_rate']-job.settings['frame_rate'])>.1 else [])+
                    ([f'Actual output format {suffix.lstrip(".")} differs from requested {job.settings.get("output_format")}']
                      if job.settings.get('output_format') and suffix.lstrip('.')!=job.settings['output_format'] else [])+
                    (['Unusual source frame rate; output will be normalized'] if meta['frame_rate']>120 else []))
            with closing(self.db._connect()) as con, con:
                self.db._check_schema(con)
                con.execute('INSERT INTO generated_video VALUES(?,?,?,?,?,?,?,?)',
                    (clip.identifier,clip.job_id,clip.shot_id,clip.parent_id,clip.path,clip.sha256,
                     clip.approval_status,clip.model_dump_json()))
            return clip
        except Exception:
            target.unlink(missing_ok=True);(self.root/poster_rel).unlink(missing_ok=True)
            raise

    def recover(self):
        changed=[]
        for job in self.jobs():
            if job.status in {'Submitted','Running'}:
                job.status='Needs Reconciliation';self.update_job(job);changed.append(job)
        return changed

def hash_file(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()

def validate_video(path):
    from prose_to_video.rendering.ffmpeg_renderer import probe
    path=Path(path)
    if not path.is_file() or path.stat().st_size<1024:raise ValueError('Video file is empty')
    data=probe(path)
    stream=next((s for s in data.get('streams',[]) if s.get('codec_type')=='video'),None)
    if not stream:raise ValueError('No video stream')
    width,height=stream.get('width',0),stream.get('height',0)
    numerator,denominator=map(int,(stream.get('avg_frame_rate') or '0/1').split('/'))
    fps=numerator/denominator if denominator else 0
    duration=float(stream.get('duration') or data.get('format',{}).get('duration') or 0)
    count=int(stream.get('nb_frames') or round(duration*fps))
    if width<=0 or height<=0 or fps<=0 or duration<=0 or count<=0:raise ValueError('Invalid video metadata')
    counted=subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-count_frames',
        '-show_entries','stream=nb_read_frames','-of','default=noprint_wrappers=1:nokey=1',str(path)],
        capture_output=True,text=True,timeout=120)
    if counted.returncode:raise ValueError('Could not count video frames')
    actual_count=counted.stdout.strip()
    if actual_count.isdigit():
        if stream.get('nb_frames') and abs(int(actual_count)-count)>1:
            raise ValueError('Video frame count differs from metadata')
        count=int(actual_count)
    check=subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(path),'-map','0:v:0','-f','null','-'],
        capture_output=True,text=True,timeout=120)
    if check.returncode:raise ValueError(f'Video decoding failed: {check.stderr[-500:]}')
    return {'codec':stream.get('codec_name',''),'width':width,'height':height,'frame_rate':fps,
            'duration':duration,'frame_count':count,'format_name':data.get('format',{}).get('format_name',''),
            'has_audio':any(s.get('codec_type')=='audio' for s in data.get('streams',[]))}

def extract_final_frame(path, output):
    output=Path(output)
    if output.exists():raise FileExistsError(output)
    meta=validate_video(path)
    result=subprocess.run(['ffmpeg','-v','error','-i',str(path),'-map','0:v:0',
        '-vf','select=eq(n\\,'+str(meta['frame_count']-1)+')','-frames:v','1',str(output)],
        capture_output=True,text=True,timeout=120)
    if result.returncode or not output.is_file():raise ValueError('Could not extract final frame')
    return output
