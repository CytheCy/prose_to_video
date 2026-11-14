"""JSON-line worker protocol used by the GUI and command line."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from .core import Project, export_names, fingerprint, match_sections, validate_export


def emit(event, **data):
    print(json.dumps({"event": event, **data}), flush=True)


def progress(value, message):
    emit("progress", value=value, message=message)


def run(command):
    completed = subprocess.run(command, capture_output=True)
    if completed.returncode:
        raise RuntimeError(completed.stderr.decode(errors="replace")[-3000:])
    return completed.stdout


def probe(path):
    data = json.loads(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path]))
    return float(data["format"]["duration"])


def cache_dir():
    root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "podcast-splitter"
    root.mkdir(parents=True, exist_ok=True)
    return root


def decode(path, start=None, duration=None):
    import numpy as np
    args = ["ffmpeg", "-v", "error"]
    if start is not None:
        args += ["-ss", str(start)]
    args += ["-i", path]
    if duration is not None:
        args += ["-t", str(duration)]
    raw = run(args + ["-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"])
    return np.frombuffer(raw, dtype="<f4").copy()


def waveform_and_silence(audio):
    import numpy as np
    window = 320  # 20 ms
    usable = audio[:len(audio) // window * window].reshape(-1, window)
    rms = np.sqrt(np.mean(usable ** 2, axis=1))
    silent = rms < max(.003, float(np.percentile(rms, 85)) * .045)
    gaps, begin = [], None
    for i, quiet in enumerate(silent):
        if quiet and begin is None:
            begin = i
        if not quiet and begin is not None:
            if i - begin >= 6:
                gaps.append((begin * .02, i * .02))
            begin = None
    if begin is not None:
        gaps.append((begin * .02, len(rms) * .02))
    bins = np.array_split(np.abs(audio), min(2400, len(audio)))
    peaks = np.array([float(np.max(x)) for x in bins])
    scale = max(float(np.percentile(peaks, 98)), .001)
    return np.minimum(peaks / scale, 1).round(3).tolist(), gaps


def analyze(project, model):
    progress(1, "Checking source files…")
    project.audio_fingerprint = fingerprint(project.audio)
    project.prose_fingerprint = fingerprint(project.prose)
    project.duration = probe(project.audio)
    project.model = model
    cached = cache_dir() / f"{project.audio_fingerprint}-{model}-v1.json"
    words = None
    recognizer = None
    if cached.exists():
        try:
            saved = json.loads(cached.read_text())
            words = saved["words"]
            project.waveform, gaps = saved["waveform"], saved["silence"]
            progress(85, "Using cached transcription…")
        except (ValueError, KeyError):
            words = None
    if words is None:
        progress(3, "Decoding audio and building waveform…")
        audio = decode(project.audio)
        project.waveform, gaps = waveform_and_silence(audio)
        emit("waveform", values=project.waveform, duration=project.duration)
        progress(5, f"Loading {model}. First use downloads the model; this can take several minutes…")
        from faster_whisper import WhisperModel
        recognizer = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=min(4, os.cpu_count() or 2),
                                  download_root=str(cache_dir() / "models"))
        progress(8, "Transcribing locally on CPU…")
        segments, _ = recognizer.transcribe(audio, language="en", word_timestamps=True,
                                            vad_filter=True, beam_size=5, condition_on_previous_text=False)
        words = []
        for segment in segments:
            words.extend({"word": w.word, "start": w.start, "end": w.end} for w in segment.words or [])
            progress(min(84, 8 + round(76 * segment.end / project.duration)),
                     f"Transcribing {segment.end / 60:.1f} / {project.duration / 60:.1f} minutes…")
        temp = cached.with_suffix(".tmp")
        temp.write_text(json.dumps({"words": words, "waveform": project.waveform, "silence": gaps}))
        temp.replace(cached)
    progress(90, "Matching prose sections and refining cut points…")
    match_sections(project.sections, words, project.duration, gaps)
    # A word at a segment edge can be assigned to the other side of a long
    # pause. Re-recognize just those transitions with uninterrupted context.
    suspicious = []
    for section in project.sections:
        if not section.included or section.start is None:
            continue
        i = min(range(len(words)), key=lambda i: abs(words[i]["start"] - (section.start + .1)))
        if i + 1 < len(words) and words[i + 1]["start"] - words[i]["end"] > 1.5:
            suspicious.append(words[i]["start"])
    for t in suspicious:
        progress(94, "Double-checking a chapter transition with a long pause…")
        if recognizer is None:
            from faster_whisper import WhisperModel
            recognizer = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=min(4, os.cpu_count() or 2),
                                      download_root=str(cache_dir() / "models"))
        offset = max(0, t - 7)
        segments, _ = recognizer.transcribe(decode(project.audio, offset, 28), language="en", word_timestamps=True,
                                            vad_filter=False, beam_size=5, condition_on_previous_text=False)
        replacement = []
        lo, hi = max(0, t - 2), min(project.duration, t + 12)
        for segment in segments:
            for w in segment.words or []:
                if lo <= offset + w.start < hi:
                    replacement.append({"word": w.word, "start": offset + w.start, "end": offset + w.end})
        if replacement:
            words = sorted([w for w in words if not lo <= w["start"] < hi] + replacement, key=lambda w: w["start"])
    if suspicious:
        match_sections(project.sections, words, project.duration, gaps)
        temp = cached.with_suffix(".tmp")
        temp.write_text(json.dumps({"words": words, "waveform": project.waveform, "silence": gaps}))
        temp.replace(cached)
    progress(100, "Analysis complete. Preview the chapter boundaries before export.")
    return project


def export(project, extension, bitrate="192k"):
    sections = validate_export(project)
    if fingerprint(project.audio) != project.audio_fingerprint:
        raise ValueError("The source audio changed since analysis. Analyze it again.")
    output = Path(project.output)
    output.mkdir(parents=True, exist_ok=True)
    names = export_names(sections, extension)
    prose_names = [str(Path(name).with_suffix(".md")) for name in names]
    all_names = [name for pair in zip(names, prose_names) for name in pair]
    for name in all_names:
        if (output / name).exists():
            raise FileExistsError(f"Already exists: {name}. Choose an empty export folder.")
    # Stage in the destination filesystem. Cancellation leaves no partial chapter files.
    with tempfile.TemporaryDirectory(prefix=".podcast-splitter-", dir=output) as stage:
        for i, (section, name) in enumerate(zip(sections, names)):
            progress(round(i / len(sections) * 95), f"Exporting {name}…")
            start, end = project.cut_points(section)
            args = ["ffmpeg", "-v", "error", "-nostdin", "-ss", str(start), "-i", project.audio,
                    "-t", str(end - start), "-vn", "-map_metadata", "-1"]
            args += {"mp3": ["-c:a", "libmp3lame", "-b:a", bitrate],
                     "flac": ["-c:a", "flac"], "wav": ["-c:a", "pcm_s16le"]}[extension]
            args += ["-metadata", f"title={section.title}", "-metadata", f"track={i + 1}", str(Path(stage) / name)]
            run(args)
            prose = f"# {section.title}\n\n{section.text}\n"
            (Path(stage) / prose_names[i]).write_text(prose, encoding="utf-8")
        published = []
        try:
            for name in all_names:
                # link is atomic and refuses to replace a file created during encoding.
                os.link(Path(stage) / name, output / name)
                published.append(output / name)
        except BaseException:
            for path in published:
                path.unlink(missing_ok=True)
            raise
    progress(100, f"Exported {len(names)} chapters with matching Markdown prose to {output}")
    return names


def main():
    if hasattr(os, "setsid"):
        os.setsid()
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["analyze", "export"])
    parser.add_argument("project")
    parser.add_argument("--model", default="base.en", choices=["tiny.en", "base.en", "small.en", "medium.en"])
    parser.add_argument("--format", default="mp3", choices=["mp3", "flac", "wav"])
    args = parser.parse_args()
    try:
        for tool in ("ffmpeg", "ffprobe"):
            if not shutil.which(tool):
                raise RuntimeError(f"{tool} is missing. Install FFmpeg with the MP3 encoder enabled.")
        project = Project.load(args.project)
        if args.action == "analyze":
            analyze(project, args.model).save(args.project)
        else:
            export(project, args.format)
        emit("done", project=args.project)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        emit("error", message=str(error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
