# Prose to Video

Prose to Video is a Fedora KDE desktop application for organizing written stories and their narrated audio. Stage One provides project creation, source imports, prose viewing, audio inspection and playback, and persistent video output settings. It does not produce a video yet.

## Fedora KDE setup

Install Python 3.12 or newer and Fedora's FFmpeg tools:

```bash
sudo dnf install python3.12 ffmpeg-free
```

`ffmpeg-free` supplies `ffmpeg` and `ffprobe`. Fedora's build supports a limited set of codecs; Qt playback also depends on the multimedia codecs available on the machine. An audio file can have valid FFprobe metadata while Qt cannot play its codec. The application reports playback errors in that case. No GPU or external service is required. The package names are listed by [Fedora Packages: Python 3.12](https://packages.fedoraproject.org/pkgs/python3.12/python3.12/) and [Fedora Packages: ffmpeg-free](https://packages.fedoraproject.org/pkgs/ffmpeg/ffmpeg-free/).

From the repository root, create a virtual environment and install the application:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
prose-to-video
```

The dependency is `PySide6-Addons`, which installs the PySide6 core and multimedia modules without unrelated Qt WebEngine and PDF modules. Pydantic validates project data and settings. `pytest` is the only test dependency.

To run the tests:

```bash
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
```

The GUI smoke test uses Qt's `offscreen` platform, so it does not require a visible desktop session. For interactive use, launch `prose-to-video` from a Fedora KDE session. `python -m prose_to_video.main` is an equivalent launch command.

## Using Stage One

Choose **File → New Project** and enter a project name and parent directory. The app creates a new folder named after the project. Use **Import Prose** for UTF-8 `.md` or `.txt` files and **Import Audio** for `.mp3`, `.wav`, `.flac`, or `.m4a` files. Imports copy the originals into `source/`; the originals are left unchanged. If a source filename already exists in the project, the import is rejected so no file is overwritten. Rename the source file before importing it again if you want to keep both copies.

The prose panel displays Markdown as source text and preserves line breaks. Its statistics count Unicode characters, words, and paragraphs. A word is a Unicode letter/digit/underscore sequence with optional internal apostrophes; hyphens separate words and Markdown punctuation is ignored. A paragraph is a nonempty text block separated by a blank line. The text is read on a worker thread and inserted into the viewer in chunks.

The audio panel has play, pause, stop, seek, elapsed time, duration, and volume controls. FFprobe records duration, codec, sample rate, and channel count when available. **File → Video Output Settings** changes width, height, and frame rate; defaults are 1920 × 1080 at 30 fps. **Save Project** updates the project's modified time. Imports and settings save automatically. **Close Project** clears the workspace; **Open Project** asks for an existing project folder.

Each project is self-contained:

```text
Project_Name/
├── project.sqlite
├── source/          # copied prose and narration
├── scenes/
├── alignment/
├── storyboard/
├── references/
├── images/
├── video_clips/
├── previews/
└── output/
```

Only `project.sqlite` and imported source files contain Stage One data. The other folders reserve space for later stages; they contain no generated assets. The database stores source paths relative to the project folder, so moving the whole folder and opening it in its new location keeps the references valid. A missing source file appears as missing in the project panel and can be replaced with a new import.

The SQLite schema uses `PRAGMA user_version`. Version 1 contains a single `project` table. Future schema changes should add a numbered SQL migration in `storage/database.py`, apply each version in order within a transaction, and then update `user_version`. This release refuses unknown database versions so it does not silently alter newer projects.

## Scope

Stage One includes a resizable Qt workspace, persistent projects, validated source imports, prose statistics, audio metadata and playback, output settings, and automated tests. Scene detection, transcription, alignment, storyboarding, AI generation, animation, timeline assembly, rendering, and quality control remain for later stages. Stage Two may add narrative scene detection using the existing project and storage boundaries.
