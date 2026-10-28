"""Bounded-memory FFmpeg shot rendering, transition pieces, muxing, and probing."""
from __future__ import annotations

import hashlib
import json
import math
import os
import select
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Event

from prose_to_video.models.video_render import RenderPlan, PlannedSegment
from prose_to_video.pipeline.video_assembly import file_hash, validate_render_plan

RENDERER_VERSION = 2


class RenderCanceled(Exception):
    pass


class RenderFailure(Exception):
    pass


def probe(path: Path) -> dict:
    if shutil.which('ffprobe') is None:
        raise RenderFailure('FFprobe is missing')
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                             '-of', 'json', str(path)], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RenderFailure(f'FFprobe could not read output: {result.stderr.strip()}')
    return json.loads(result.stdout)


def validate_output(path: Path, plan: RenderPlan) -> dict:
    if not path.is_file() or path.stat().st_size == 0:
        raise RenderFailure('Output MP4 is missing or empty')
    data = probe(path)
    streams = data.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    if not video or not audio:
        raise RenderFailure('Output must contain both video and narration audio')
    settings = plan.settings
    if video.get('codec_name') != 'h264' or audio.get('codec_name') != 'aac':
        raise RenderFailure('Output codecs differ from requested H.264/AAC')
    if (video.get('width'), video.get('height')) != (settings.width, settings.height):
        raise RenderFailure('Output dimensions differ from render plan')
    numerator, denominator = map(int, video['avg_frame_rate'].split('/'))
    if abs(numerator / denominator - settings.frame_rate) > .001:
        raise RenderFailure('Output frame rate differs from render plan')
    expected = plan.segments[-1].end_frame / settings.frame_rate
    video_duration = float(video.get('duration') or data['format']['duration'])
    audio_duration = float(audio.get('duration') or data['format']['duration'])
    if abs(video_duration - expected) > 2 / settings.frame_rate:
        raise RenderFailure(f'Output duration {video_duration:.3f}s differs from planned {expected:.3f}s')
    if abs(video_duration - audio_duration) > max(.12, 3/settings.frame_rate):
        raise RenderFailure('Audio and video stream durations differ unexpectedly')
    frames = int(video.get('nb_frames') or round(video_duration * settings.frame_rate))
    if abs(frames - plan.segments[-1].end_frame) > 1:
        raise RenderFailure('Output frame count differs from render plan')
    sample = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{expected/2:.6f}', '-i', str(path),
        '-map', '0:v:0', '-frames:v', '1', '-f', 'null', '-'], capture_output=True, text=True, timeout=60)
    if sample.returncode:
        raise RenderFailure(f'Output sample frame cannot be decoded: {sample.stderr.strip()}')
    return {'format': data.get('format', {}), 'video': video, 'audio': audio,
            'frames': frames, 'duration': video_duration, 'audio_duration': audio_duration}


def _key(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


# Five-by-seven glyphs keep draft labeling independent of Qt and installed fonts.
_FONT = {
    'A': ('01110','10001','10001','11111','10001','10001','10001'),
    'B': ('11110','10001','10001','11110','10001','10001','11110'),
    'C': ('01111','10000','10000','10000','10000','10000','01111'),
    'D': ('11110','10001','10001','10001','10001','10001','11110'),
    'E': ('11111','10000','10000','11110','10000','10000','11111'),
    'F': ('11111','10000','10000','11110','10000','10000','10000'),
    'G': ('01111','10000','10000','10111','10001','10001','01111'),
    'H': ('10001','10001','10001','11111','10001','10001','10001'),
    'I': ('11111','00100','00100','00100','00100','00100','11111'),
    'J': ('00111','00010','00010','00010','10010','10010','01100'),
    'K': ('10001','10010','10100','11000','10100','10010','10001'),
    'L': ('10000','10000','10000','10000','10000','10000','11111'),
    'M': ('10001','11011','10101','10101','10001','10001','10001'),
    'N': ('10001','11001','10101','10011','10001','10001','10001'),
    'O': ('01110','10001','10001','10001','10001','10001','01110'),
    'P': ('11110','10001','10001','11110','10000','10000','10000'),
    'Q': ('01110','10001','10001','10001','10101','10010','01101'),
    'R': ('11110','10001','10001','11110','10100','10010','10001'),
    'S': ('01111','10000','10000','01110','00001','00001','11110'),
    'T': ('11111','00100','00100','00100','00100','00100','00100'),
    'U': ('10001','10001','10001','10001','10001','10001','01110'),
    'V': ('10001','10001','10001','10001','10001','01010','00100'),
    'W': ('10001','10001','10001','10101','10101','10101','01010'),
    'X': ('10001','10001','01010','00100','01010','10001','10001'),
    'Y': ('10001','10001','01010','00100','00100','00100','00100'),
    'Z': ('11111','00001','00010','00100','01000','10000','11111'),
    '0': ('01110','10001','10011','10101','11001','10001','01110'),
    '1': ('00100','01100','00100','00100','00100','00100','01110'),
    '2': ('01110','10001','00001','00010','00100','01000','11111'),
    '3': ('11110','00001','00001','01110','00001','00001','11110'),
    '4': ('00010','00110','01010','10010','11111','00010','00010'),
    '5': ('11111','10000','10000','11110','00001','00001','11110'),
    '6': ('01111','10000','10000','11110','10001','10001','01110'),
    '7': ('11111','00001','00010','00100','01000','01000','01000'),
    '8': ('01110','10001','10001','01110','10001','10001','01110'),
    '9': ('01110','10001','10001','01111','00001','00001','11110'),
    '-': ('00000','00000','00000','11111','00000','00000','00000'),
    '_': ('00000','00000','00000','00000','00000','00000','11111'),
    ' ': ('00000',)*7,
}


def _placeholder(cache: Path, segment: PlannedSegment) -> Path:
    label = f'DRAFT MISSING IMAGE\nSCENE {segment.scene_id}\nSHOT {segment.shot_id}'.upper()
    key = hashlib.sha256((label + str(RENDERER_VERSION)).encode()).hexdigest()
    path = cache / f'placeholder-{key}.ppm'
    if path.exists(): return path
    width, height, scale = 640, 360, 2
    pixels = bytearray(bytes((48, 48, 56)) * (width * height))
    rows = label.splitlines()
    top = (height - len(rows) * 22) // 2
    for line_number, line in enumerate(rows):
        # UUIDs fit at this scale. Other identifiers are still included in full in the manifest.
        line = line[:50]
        left = max(0, (width - len(line) * 6 * scale) // 2)
        for index, char in enumerate(line):
            glyph = _FONT.get(char, _FONT['-'])
            for gy, row in enumerate(glyph):
                for gx, bit in enumerate(row):
                    if bit == '1':
                        for py in range(scale):
                            y = top + line_number * 22 + gy * scale + py
                            for px in range(scale):
                                x = left + index * 6 * scale + gx * scale + px
                                if 0 <= x < width and 0 <= y < height:
                                    offset = (y * width + x) * 3
                                    pixels[offset:offset+3] = b'\xff\xff\xff'
    temporary = path.with_suffix('.part')
    try:
        with temporary.open('wb') as out:
            out.write(f'P6\n{width} {height}\n255\n'.encode())
            out.write(pixels)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _color(value: str) -> str:
    return 'black' if value == 'black' else '0x' + value.lstrip('#')


def image_filter(segment: PlannedSegment, background: str) -> str:
    w, h, n, fps = segment.width, segment.height, segment.frames, segment.frame_rate
    if segment.fit_policy == 'fit':
        base = f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={_color(background)}'
    else:
        base = f'scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}'
    camera = segment.camera
    if camera.kind == 'Static' or n < 2:
        return base + f',fps={fps},format=yuv420p'
    # Motion is bounded by the resolved framing, with no pixels outside the image.
    t = f'on/{n-1}'
    zoom = f'{camera.start_zoom:.8f}+({camera.end_zoom-camera.start_zoom:.8f})*{t}'
    xpos = f'{camera.start_x:.8f}+({camera.end_x-camera.start_x:.8f})*{t}'
    ypos = f'{camera.start_y:.8f}+({camera.end_y-camera.start_y:.8f})*{t}'
    return (base + f",zoompan=z='{zoom}':x='(iw-iw/zoom)*({xpos})':"
            f"y='(ih-ih/zoom)*({ypos})':d={n}:s={w}x{h}:fps={fps},format=yuv420p")


class FFmpegRenderer:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.cache = self.root / 'video_clips' / 'render_cache'
        self.cache.mkdir(parents=True, exist_ok=True)

    def clear_cache(self):
        shutil.rmtree(self.cache)
        self.cache.mkdir(parents=True, exist_ok=True)

    def prune_cache(self, max_bytes=20 * 1024**3):
        files = sorted(self.cache.glob('*.mp4'), key=lambda p: p.stat().st_mtime_ns)
        total = sum(p.stat().st_size for p in files)
        for path in files:
            if total <= max_bytes: break
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size

    def _run(self, args: list[str], cancel: Event | None, progress=None, stage='Rendering'):
        if shutil.which('ffmpeg') is None:
            raise RenderFailure('FFmpeg is missing')
        with tempfile.TemporaryFile(mode='w+t', encoding='utf-8') as errors:
            process = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-progress', 'pipe:1', '-y', *args], stdout=subprocess.PIPE, stderr=errors,
                text=True, start_new_session=True)
            try:
                while True:
                    if cancel and cancel.is_set():
                        try: os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError: pass
                        try: process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            try: os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError: pass
                            process.wait()
                        raise RenderCanceled('Render canceled')
                    ready, _, _ = select.select([process.stdout], [], [], .2)
                    if ready:
                        line = process.stdout.readline()
                        if line.startswith('out_time_us=') and progress:
                            try: progress(stage, int(line.split('=', 1)[1]) / 1_000_000)
                            except ValueError: pass
                    if process.poll() is not None:
                        for line in process.stdout:
                            if line.startswith('out_time_us=') and progress:
                                try: progress(stage, int(line.split('=', 1)[1]) / 1_000_000)
                                except ValueError: pass
                        break
                if process.returncode:
                    errors.seek(0)
                    raise RenderFailure((errors.read() or 'FFmpeg failed').strip()[-3000:])
            finally:
                process.stdout.close()
                if process.poll() is None:
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    process.wait()

    def _encode(self, plan: RenderPlan) -> list[str]:
        s = plan.settings
        return ['-c:v', s.video_codec, '-preset', s.preset, '-crf', str(s.crf),
                '-pix_fmt', 'yuv420p', '-r', str(s.frame_rate), '-an', '-movflags', '+faststart']

    def _segment(self, plan: RenderPlan, segment: PlannedSegment, cancel, progress) -> Path:
        if segment.asset_kind == 'video':
            source=Path(segment.source_path)
            if not source.is_file() or file_hash(source)!=segment.source_fingerprint:
                raise RenderFailure(f'Source video changed or is missing: {segment.shot_id}')
            s=plan.settings
            key=_key({'version':RENDERER_VERSION,'video':segment.source_fingerprint,
                'frames':segment.frames,'dimensions':(segment.width,segment.height),
                'fps':segment.frame_rate,'fit':segment.fit_policy,'crf':s.crf,'preset':s.preset})
            target=self.cache/f'{key}.mp4'
            if target.is_file() and target.stat().st_size>0:return target
            temporary=target.with_suffix('.part')
            if segment.fit_policy=='fit':
                sizing=f'scale={segment.width}:{segment.height}:force_original_aspect_ratio=decrease,'
                sizing+=f'pad={segment.width}:{segment.height}:(ow-iw)/2:(oh-ih)/2:color={_color(s.background_color)}'
            else:
                sizing=f'scale={segment.width}:{segment.height}:force_original_aspect_ratio=increase,'
                sizing+=f'crop={segment.width}:{segment.height}'
            filt=f'{sizing},fps={segment.frame_rate},tpad=stop_mode=clone:stop_duration={2/segment.frame_rate:.9f},trim=end_frame={segment.frames},setpts=PTS-STARTPTS,format=yuv420p'
            try:
                self._run(['-i',str(source),'-map','0:v:0','-vf',filt,'-frames:v',str(segment.frames),
                    *self._encode(plan),'-f','mp4',str(temporary)],cancel,progress,f'Shot {segment.shot_id}')
                os.replace(temporary,target)
                return target
            finally:temporary.unlink(missing_ok=True)
        s = plan.settings
        source = Path(segment.source_path) if segment.source_path else _placeholder(self.cache, segment)
        digest = file_hash(source)
        if segment.source_fingerprint and digest != segment.source_fingerprint:
            raise RenderFailure(f'Source image changed during rendering: {segment.shot_id}')
        key = _key({'version': RENDERER_VERSION, 'asset': digest, 'frames': segment.frames,
                    'dimensions': (segment.width, segment.height), 'fps': segment.frame_rate,
                    'fit': segment.fit_policy, 'color': s.background_color,
                    'camera': segment.camera.model_dump(), 'crf': s.crf, 'preset': s.preset})
        target = self.cache / f'{key}.mp4'
        if target.is_file() and target.stat().st_size > 0: return target
        temporary = target.with_suffix('.part')
        try:
            self._run(['-loop', '1', '-framerate', str(s.frame_rate), '-i', str(source),
                '-vf', image_filter(segment, s.background_color), '-frames:v', str(segment.frames),
                *self._encode(plan), '-f', 'mp4', str(temporary)], cancel, progress,
                f'Shot {segment.shot_id}')
            os.replace(temporary, target)
            return target
        finally:
            temporary.unlink(missing_ok=True)

    def _effect(self, plan, segment, source, kind, cancel, progress):
        d = segment.transition_frames
        key = _key({'version': RENDERER_VERSION, 'effect': kind, 'source': source.name,
                    'frames': d, 'crf': plan.settings.crf, 'preset': plan.settings.preset})
        target = self.cache / f'{key}.mp4'
        if target.exists(): return target
        tmp = target.with_suffix('.part')
        try:
            if kind == 'tail':
                filt = f'trim=start_frame={d},setpts=PTS-STARTPTS'
            elif kind == 'fade_in':
                filt = f'fade=t=in:st=0:d={d/segment.frame_rate:.8f}'
            else:
                filt = f'fade=t=out:st={(segment.frames-d)/segment.frame_rate:.8f}:d={d/segment.frame_rate:.8f}'
            frames = segment.frames-d if kind == 'tail' else segment.frames
            self._run(['-i', str(source), '-vf', filt, '-frames:v', str(frames),
                *self._encode(plan), '-f', 'mp4', str(tmp)], cancel, progress, kind)
            os.replace(tmp, target)
            return target
        finally: tmp.unlink(missing_ok=True)

    def _transition(self, plan, prev, current, prev_file, current_file, cancel, progress):
        d = current.transition_frames
        key = _key({'version': RENDERER_VERSION, 'transition': current.transition,
                    'prev': prev_file.name, 'next': current_file.name,
                    'frames': d, 'crf': plan.settings.crf, 'preset': plan.settings.preset})
        target = self.cache / f'{key}.mp4'
        if target.exists(): return target
        tmp = target.with_suffix('.part')
        effect = 'fadeblack' if current.transition == 'Fade through black' else 'fade'
        filt = (f'[0:v]trim=start_frame={prev.frames-1}:end_frame={prev.frames},setpts=PTS-STARTPTS,'
                f'tpad=stop_mode=clone:stop_duration={(d+2)/prev.frame_rate:.8f},'
                f'trim=end_frame={d+1},setpts=PTS-STARTPTS[a];'
                f'[1:v]trim=end_frame={d+1},setpts=PTS-STARTPTS[b];'
                f'[a][b]xfade=transition={effect}:duration={d/current.frame_rate:.8f}:offset=0,'
                f'trim=end_frame={d},setpts=PTS-STARTPTS[v]')
        try:
            self._run(['-i', str(prev_file), '-i', str(current_file), '-filter_complex', filt,
                '-map', '[v]', '-frames:v', str(d), *self._encode(plan), '-f', 'mp4', str(tmp)],
                cancel, progress, f'{current.transition} {current.shot_id}')
            os.replace(tmp, target)
            return target
        finally: tmp.unlink(missing_ok=True)

    def render(self, plan: RenderPlan, output: Path, *, cancel: Event | None = None,
               progress=None, accept_warnings=False) -> dict:
        if any(i.severity == 'error' for i in plan.issues) or not validate_render_plan(plan).ready:
            raise RenderFailure('Render plan has validation errors')
        if any(i.severity == 'warning' for i in plan.issues) and not accept_warnings:
            raise RenderFailure('Render plan has warnings; explicit approval is required')
        if output.exists(): raise FileExistsError(f'Output already exists: {output}')
        output.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        clips = []
        pieces = []
        temporary = output.with_name('.' + output.name + '.part.mp4')
        visual = output.with_name('.' + output.name + '.visual.mp4')
        try:
            completed_frames = 0
            for segment in plan.segments:
                if cancel and cancel.is_set(): raise RenderCanceled('Render canceled')
                base = completed_frames / plan.settings.frame_rate
                shot_progress = (lambda stage, seconds, base=base: progress(stage, base + seconds)) if progress else None
                clips.append(self._segment(plan, segment, cancel, shot_progress))
                completed_frames += segment.frames
                if progress: progress(f'Shot {segment.shot_id} complete', completed_frames / plan.settings.frame_rate)
            for i, segment in enumerate(plan.segments):
                clip = clips[i]
                if i and segment.transition in {'Crossfade', 'Fade through black'} and segment.transition_frames:
                    pieces.append(self._transition(plan, plan.segments[i-1], segment,
                        clips[i-1], clip, cancel, None))
                    clip = self._effect(plan, segment, clip, 'tail', cancel, None)
                elif segment.transition == 'Fade in' and segment.transition_frames:
                    clip = self._effect(plan, segment, clip, 'fade_in', cancel, None)
                if segment.transition == 'Fade out' and segment.transition_frames:
                    clip = self._effect(plan, segment, clip, 'fade_out', cancel, None)
                pieces.append(clip)
            with tempfile.NamedTemporaryFile(mode='w', suffix='.ffconcat', dir=self.cache,
                                             encoding='utf-8', delete=False) as listing:
                concat_path = Path(listing.name)
                listing.write('ffconcat version 1.0\n')
                for piece in pieces:
                    escaped = str(piece.resolve()).replace("'", "'\\''")
                    listing.write(f"file '{escaped}'\n")
            try:
                if progress: progress('Assembling video', completed_frames / plan.settings.frame_rate)
                self._run(['-safe', '0', '-f', 'concat', '-i', str(concat_path),
                    '-map', '0:v:0', '-c:v', 'copy', '-an', '-f', 'mp4', str(visual)],
                    cancel, None, 'Assembling video')
            finally: concat_path.unlink(missing_ok=True)
            for segment in plan.segments:
                if segment.source_path and file_hash(Path(segment.source_path)) != segment.source_fingerprint:
                    raise RenderFailure(f'Source image changed during rendering: {segment.shot_id}')
            if file_hash(Path(plan.audio_path)) != plan.audio_fingerprint:
                raise RenderFailure('Narration source changed during rendering')
            duration = plan.source_end-plan.source_start
            audio_args = ['-ss', f'{plan.source_start:.9f}'] if plan.source_start else []
            audio_stream = next((s for s in probe(Path(plan.audio_path))['streams'] if s.get('codec_type') == 'audio'), {})
            copy_audio = audio_stream.get('codec_name') == 'aac' and plan.scope == 'full' and plan.source_start == 0
            audio_codec_args = ['-c:a', 'copy'] if copy_audio else ['-c:a', 'aac', '-b:a', '192k']
            if progress: progress('Adding narration', completed_frames / plan.settings.frame_rate)
            self._run(['-i', str(visual), *audio_args, '-i', plan.audio_path,
                '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', *audio_codec_args,
                '-t', f'{duration:.9f}', '-movflags', '+faststart', '-f', 'mp4', str(temporary)],
                cancel, None, 'Adding narration')
            if progress: progress('Validating output', completed_frames / plan.settings.frame_rate)
            metadata = validate_output(temporary, plan)
            metadata['format']['filename'] = str(output)
            if progress: progress('Validated output', plan.segments[-1].end_frame / plan.settings.frame_rate)
            metadata['elapsed_seconds'] = time.monotonic()-started
            os.replace(temporary, output)
            self.prune_cache()
            return metadata
        finally:
            temporary.unlink(missing_ok=True)
            visual.unlink(missing_ok=True)
