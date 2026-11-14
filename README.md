# Prose to Video

Desktop prose-to-stills-to-video editor. The active application has seven tabs:
**Podcast Splitter → Start → Place → Character → Scenes → Stills → Video Production**.
Project and settings commands are available from the application menu. Historical storyboard, generic entity,
animation, production queue, QC and approval interfaces are inactive.

## Fedora KDE setup

Install Python 3.12 or 3.13 and Fedora's FFmpeg tools. The base application also runs on newer Python versions, but WhisperX 3.8.6 currently requires Python earlier than 3.14:

```bash
sudo dnf install python3.12 ffmpeg-free
```

`ffmpeg-free` supplies `ffmpeg` and `ffprobe`. Fedora's build supports a limited set of codecs; Qt playback also depends on the multimedia codecs available on the machine. An audio file can have valid FFprobe metadata while Qt cannot play its codec. The application reports playback errors in that case. Audio alignment needs no GPU or external service; AI scene detection uses an OpenAI API key. The package names are listed by [Fedora Packages: Python 3.12](https://packages.fedoraproject.org/pkgs/python3.12/python3.12/) and [Fedora Packages: ffmpeg-free](https://packages.fedoraproject.org/pkgs/ffmpeg/ffmpeg-free/).

From the repository root, create a virtual environment and install the application:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
prose-to-video
```

The application uses `PySide6-Addons`, Pydantic, and the official OpenAI Python client. `pytest` is the only test dependency. Audio metadata uses FFprobe. The integrated Podcast Splitter uses faster-whisper for local CPU speech recognition and FFmpeg for previews and chapter exports (MP3 requires libmp3lame).

To run the tests:

```bash
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
```

The GUI smoke test uses Qt's `offscreen` platform, so it does not require a visible desktop session. For interactive use, launch `prose-to-video` from a Fedora KDE session. `python -m prose_to_video.main` is an equivalent launch command.

## Podcast Splitter (first tab)

Create or open a project with **File → New Project / Open Project**, then choose
Markdown prose and narration in **Podcast Splitter**. Both tabs use the same copies
in the project's `source/` folder; importing or replacing a source in either tab
updates the other. Splitter Markdown supports `.md`, `.markdown`, and `.txt` with
ATX headings; prose without headings remains usable by the video workflow.

The original splitter interface and engine are included in `src/podcast_splitter/`
under their MIT license. Select headings, choose a local speech model, analyze,
preview and edit IN/OUT cuts, approve flagged chapters, widen all parts, and export
MP3, FLAC, or WAV with matching Markdown files. Existing exports are never
overwritten. The default export folder is `output/chapters/` inside the project;
you can still choose another destination. First analysis downloads the selected
speech model; models and transcript caches use the user's normal cache directory.

**File → Save Project** saves both workflows. Splitter edits also save automatically
while idle and when switching projects or closing the app. `podcast-splitter.json`
stores chapter selections, cut points, approvals, waveform, widening, model, and
export format alongside `project.sqlite` and `stills-workflow.json`. Source and
in-project export paths are relative, so the entire project folder can be moved.
Cancel a running audio task before saving, switching projects, or closing.

## Workflow

1. In **Start**, create/open a project, import UTF-8 prose, optionally link narration,
   configure AI/image/export settings, and save a project visual style and image density.
   Imports preserve the original files. Re-import the active prose file after editing it to reload its changed text; other duplicate source filenames are rejected.
   To replace edited narration, use **Link / Replace Narration Audio** and select
   the edited file, including one with the same filename. Then rebuild the timeline
   in Video Production and review any manual cuts. Scenes and still images are kept.

2. **Analyze Story** first reads the whole story in bounded requests to establish
   character/place identities, then plans visual beats using validated sentence IDs,
   and writes staging, a camera movement, and a self-contained TTI prompt for each scene. It does not
   generate images. Cancel safely; Analyze Story resumes saved checkpoints.
   After prose changes, Analyze Story clears the previous analysis, scenes, prompts,
   still selections and timeline, then rebuilds through image prompts. Reference
   images are preserved and reattached to matching character/place names or aliases;
   references for absent identities remain saved for future analysis.
3. **Place** and **Character** contain canonical descriptions, stable IDs and optional
   reference images. Description edits save automatically. Character lists only people participating in more than one
   included scene, resolving aliases and unambiguous pronouns to the same person.
   One-off subjects remain available for scene staging and prompts. Select the references to condition
   generation and save. Absence of references never blocks text-only generation.
4. **Scenes** is the authoritative editor for each still's original prose passage,
   identity links, description, staging, camera, lighting and final prompt. Select a
   scene to highlight its exact passage. List fields use one entry per line. Manual
   edits save automatically; prompt regeneration is explicit. Select prose to split at its start,
   or merge with the next scene. Existing images remain available after restructuring.
   Technical explanation can use a concept map (systems/flows), blueprint (sections/construction),
   CAD visualization (geometry), or product showcase (device appearance). Action involving technology
   can remain cinematic. **Explanatory focus** records the source-supported idea the image should
   communicate. Edit it and **Render type**, then use **Regenerate Prompt** to apply them; regeneration
   preserves these choices and the project's artistic style without inventing technical specifications.
   Press **Ctrl+Z** (or choose **Edit → Undo**) to undo edits and project actions;
   the restored state saves automatically. Undo history lasts until the project is closed
   or source files are imported. Consecutive typing is grouped into one undo step.
5. **Stills** generates selected/missing images, imports images, and selects candidates.
   The first successful result becomes usable immediately. Failed regeneration keeps
   the current selection. Metadata retains exact source ranges, prompt/request snapshot,
   provider/job, identities and references. Unsupported reference limits produce an
   actionable error. Negative prompts on providers without that feature are visibly
   appended as Avoid instructions and recorded in the request snapshot.
6. In **Video Production**, click **Build Timeline from Stills + Audio** to assemble
   every included scene's selected still with the full narration in one step. It reuses
   current word alignment or aligns narration automatically with the default local
   speech model (first use may download model weights). **Align Narration…** remains
   available for custom alignment settings. Timing comes from aligned source words,
   never AI timestamps. The first image covers leading audio;
   images change at the next scene's speech start and hold through pauses; the last
   covers trailing audio. Speech spans and display intervals are separate fields.
   Use the audio player's seek/play controls to preview the selected stills against
   narration. Export MP4 through FFmpeg; Preview Export opens a rendered video.

**Camera Movement** appears after Camera Position in Scenes. In Video Production,
each included scene shows its selected still on the left and a movement dropdown on
the right. Changes save automatically, support Undo, and do not require regenerating
the image. Choose from zooms, directional and diagonal pans, combined pans and zooms,
anchored zooms, slow drift, breathing, or a static hold. Focal-point movements expose
X/Y coordinates from 0 to 1, measured from the original image's top-left corner.
Moving shots fill and crop to the video aspect ratio with bounded viewports so no
image edge exposes a gap. Static holds retain the configured image fit policy.
Existing scenes default to static holds. The audio timeline previews still selection;
use Preview Export to watch rendered camera motion.

The timeline table edits display start/end times in seconds. Adjust both sides of a shared boundary,
then Save Timeline Edits. Manual intervals survive rebuilds; conflicting rebuilds fail
without replacing the saved timeline.
Closing the app also saves pending valid timeline edits. Invalid edits or save errors
keep the window open so changes can be corrected or saving retried.

**Nearest fit** is the default timing option in Video Production. Build Timeline uses
verified word matches where possible, interpolates missing scene boundaries between
nearby matches, and falls back to prose-length proportions when no matches are available.
Narration discrepancies do not block this mode. Approximate rows are labeled; preview
and adjust their display cuts, then Save Timeline Edits or export. The full audio and
all included stills remain on the timeline, and alignment flags remain available for review.
Choose **Strict** to require verified narration throughout.

For optional transcript correction and precise timing, open **Review Narration…**:

1. Matching automatically reconciles hyphens, joined words, accents, dotted acronyms,
   common title abbreviations, cardinal numbers and spoken years. Original prose and
   character positions stay intact. Joined phrases use their verified audio range;
   the app never invents individual word timings.
2. Select a flagged passage, play it, and compare the read-only manuscript context
   with the editable transcript. Correct recognition errors to what you hear. Keep
   real extra or repeated speech in the transcript. Uncheck **Show discrepancies only**
   to inspect any other passage.
3. Mark each edited passage as listened to and checked. **Save Draft** and switching
   passages save corrections locally; closing and reopening review preserves them.
4. Choose **Re-align and Build Timeline**. The app runs the corrected transcript through
   the local audio aligner without running speech recognition again, recalculates all
   discrepancies, and builds only when the included prose has verified timing and
   no unmatched transcript speech remains. If actual omissions or repetitions remain,
   review reports them; it does not silently waive them. Fix the recording or manuscript
   production selection as appropriate. Changing manuscript text still requires resolving
   scene anchors (see Current limits).

Drafts live in `alignment/transcript-review.json` and belong to a specific source/alignment
version. Cancellation or alignment failure preserves the last alignment, saved timeline,
images, and correction draft. Existing saved transcripts gain the improved matching on
build or review without another recognition pass.

Audio alignment requires the optional WhisperX dependency:
`python -m pip install -e '.[audio]'`. Model weights may need downloading on first use.

If alignment still reports missing dependencies after installation, check the Python
path in the error. Install and launch using the same virtual environment. In particular,
an older `.venv` made with Python 3.14 cannot use the audio packages installed in a
separate Python 3.12 environment such as `.venv312`. Launch that environment explicitly:

```bash
.venv312/bin/python -m prose_to_video.main
```

Update any desktop shortcut's `Exec` path to that environment's `bin/prose-to-video`
and restart the app. Reinstalling in `.venv312` does not change an already running app
or a shortcut pointing at `.venv`.

## Persistence and compatibility

`project.sqlite` and source media remain intact. The active workflow is saved atomically
in `stills-workflow.json`; images and reference files remain project-local. Back up or
move the entire project folder. Scenes store original UTF-8 text hashes and Python
character offsets; CRLF/CR/LF are preserved. Qt display normalization is translated back
to source positions. Summaries and rewritten prose are never used as anchors.

On first open, compatible character/place records, references, scenes, and complete
shot-level source partitions migrate. All shot images are preserved as candidates,
with historical selections preferred. Valid legacy timing boundaries become manual
intervals. Ambiguous records remain in the historical database with a migration message.
Historical approval flags do not gate the active workflow.

## Current limits

- Prose replacement preserves existing work and reports stale anchors; automatic
  re-anchoring is not implemented. Use a new project for a revised manuscript.
- Split/merge is blocked for scenes already placed on the timeline to preserve edits.
  Removed scenes' images remain in metadata, but are not reassigned across passages.
- Image generation is sequential, one candidate per request. Uncertain submissions
  are logged and never automatically resubmitted; provider-side job recovery after
  app restart is not implemented.
- Very large continuity catalogs can exceed the configured AI context budget;
  the provider rejects oversized requests explicitly rather than truncating identities.
- Catalog aliases and names are established during analysis; the UI edits canonical
  descriptions and references. Image/export settings currently use JSON editors.

Tests use mocked providers and synthetic media, including real FFmpeg exports when the
binary is installed. Verification never requires paid generation.
