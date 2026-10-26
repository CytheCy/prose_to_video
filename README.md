# Prose to Video

Prose to Video is a Fedora KDE desktop application for organizing written stories and their narrated audio. Stage Four adds AI visual shot planning and an editable storyboard. The application does not generate images or video yet.

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

The application uses `PySide6-Addons`, Pydantic, and the official OpenAI Python client. `pytest` is the only test dependency. Audio metadata uses FFprobe.

To run the tests:

```bash
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
```

The GUI smoke test uses Qt's `offscreen` platform, so it does not require a visible desktop session. For interactive use, launch `prose-to-video` from a Fedora KDE session. `python -m prose_to_video.main` is an equivalent launch command.

## Projects, prose, and audio

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

The database stores project and scene records. Exported scene breakdowns can be saved in `scenes/`. Other folders reserve space for later stages. Source paths are relative to the project folder, so moving the whole folder keeps references valid. A missing source file appears as missing in the project panel and can be replaced with a new import.

The SQLite schema uses `PRAGMA user_version`. Version 1 contains the Stage One `project` table. Version 2 adds `scene_analysis` and `scene` tables. Version 3 adds alignment runs, indexed words, scene timings, and transcript checkpoints. Version 4 adds project storyboard settings, storyboard runs, active scene plans, and indexed shot records. Existing older projects migrate in place when opened; unknown newer versions are refused. Scene, alignment, and storyboard replacement are transactional, so failed writes preserve preceding results.

## Detecting and reviewing scenes

Set an OpenAI API key in your shell environment before launching the application. For example:

```bash
export OPENAI_API_KEY='your-key-here'
prose-to-video
```

The key is read from the environment at request time and is never saved to the project database or exports. Use **File → AI Scene Detection Settings** to choose the provider, model, credential environment variable, optional temperature, maximum output tokens, model context limit, chunk size, and overlap size. Set the context limit to the selected model's documented capacity; the application checks a conservative UTF-8 byte budget before each request. The initial provider is OpenAI; the provider interface can support others later. The default model is [`gpt-6-luna`](https://developers.openai.com/api/docs/models/gpt-6-luna) with a conservative 128,000-token configured context limit. See the [OpenAI Python client's structured output helper](https://github.com/openai/openai-python/blob/main/helpers.md) for the API method used.

Open a project with imported prose and click **Detect Scenes** in the Scenes panel. Progress shows boundary detection, reconciliation when overlapping chunks disagree, and scene descriptions. **Cancel** stops after the current API request. Processing runs in a worker thread, so the window remains usable. If an analysis fails or is canceled, previously saved scenes stay intact. Running detection again asks for confirmation because it replaces boundaries and manually edited descriptions.

The detector divides the manuscript into configurable, overlapping chunks at paragraph boundaries where practical. An oversized paragraph is divided into context pieces without making those pieces eligible scene starts. The model proposes source IDs; Python resolves them to exact character offsets and validates that the resulting scenes cover the entire original text without gaps or overlap. A scene may span many paragraphs or chunks. Chunk edges alone never create scenes. Each scene is tied to the manuscript's SHA-256 hash. If the imported prose changes, existing scene offsets are marked outdated and editing/export is disabled until reanalysis.

Select a scene to highlight its exact range in the read-only prose viewer. The right panel shows factual fields such as location, characters, point of view, summary, environment, objects, actions, dialogue participants, and atmosphere. Edit them and click **Save Scene Details**; changed fields receive **Manually edited** status. You can also mark an unchanged scene **Reviewed**. Enter list fields one item per line. **Split Scene** offers internal paragraph boundaries. **Merge With Next Scene** combines adjacent ranges. Both operations preserve all source text and mark affected descriptions for review; review their summaries after changing a boundary.

**Export Scenes** writes JSON with complete structured records, offsets, source text, source hash, and available timing, or Markdown with a readable breakdown. **Detailed JSON** also includes word, sentence, transcript, and issue records. Exports contain no API credentials. Missing timing is explicitly unresolved.

Known scene detection limitations: the first provider requires network access and valid OpenAI credentials; live API behavior depends on the selected model's availability and context limits. Chunk size is a character limit, so lower it for models with smaller token windows. AI boundary and description quality still require human review. Long scenes are described in multiple requests, and their descriptions are combined. Scene splitting currently uses paragraph starts. Cancellation cannot interrupt an API request already in flight, but the client has a 60-second request timeout.

## Aligning narrated audio (Stage Three)

The local audio pipeline uses FFmpeg, [WhisperX](https://github.com/m-bain/whisperX), PyTorch, and a language-specific speech alignment model. Stage Three requires no API key. Install the audio extras into a **Python 3.12 or 3.13** environment. To use CPU, first select the current CPU wheel instructions on [PyTorch's Linux installation page](https://pytorch.org/get-started/locally/), then install the application extras:

```bash
python3.12 -m venv .venv312
source .venv312/bin/activate
python -m pip install -e '.[audio,test]'
```

WhisperX pulls PyTorch and other dependencies; installing the PyTorch build suited to your machine first avoids an unsuitable GPU wheel. For NVIDIA CUDA, use the matching CUDA option in the PyTorch selector and check `python -c 'import torch; print(torch.cuda.is_available())'`. The app offers `auto`, `cpu`, and `cuda`: auto uses CUDA only when PyTorch reports it available, otherwise CPU. Intel integrated graphics currently use CPU. Auto precision is `int8` on CPU and `float16` on CUDA; `float32` and `int8` can be selected explicitly. The default `tiny` model and batch size 4 are conservative starting points, but CPU alignment can still be slow and memory use depends on the chosen models and recording. WhisperX reports that its much larger `large-v2` model uses under 8 GB of GPU memory in a specified configuration; that is not a minimum hardware guarantee for this app. [WhisperX setup and memory guidance](https://github.com/m-bain/whisperX#technical-details-).

Open a project with both prose and narration imported, optionally **Detect Scenes**, then click **Align Audio**. Choose the Whisper model, device, precision, batch size, chunk duration, and language. The app asks whether model downloads are allowed for this run. Choose **No** to use cached models only; a missing model produces a clear error. A first permitted run may fetch Whisper, Silero voice detection, NLTK sentence data, and a language-specific wav2vec2 alignment model. Common Hugging Face model files live under `~/.cache/huggingface/hub` (or `HF_HOME/hub`); Silero is cached under PyTorch's hub directory, usually `~/.cache/torch/hub`; NLTK uses its normal data locations. [Hugging Face cache documentation](https://huggingface.co/docs/hub/en/local-cache), [WhisperX model selection](https://github.com/m-bain/whisperX/blob/main/whisperx/alignment.py). The application never silently fetches these large models.

Alignment runs off the GUI thread. Progress reports audio preparation, transcription, forced word alignment, manuscript matching, sentence and scene timing, and saving. **Cancel Alignment** stops at a chunk or stage boundary. FFmpeg converts bounded chunks to mono 16 kHz PCM in temporary files; it never changes the imported audio, trims leading silence, or removes pauses. The original timeline remains the timestamp reference. An hour-long recording is processed in chunks rather than decoded all at once. The waveform is a cached half-second peak summary in `alignment/`, generated separately from model work.

Select a scene to see its start, end, duration, matched-word coverage, original prose, transcribed speech, unmatched manuscript words, and warnings. **View Transcript** shows the full recognized transcript, including a completed transcription checkpoint if later alignment failed. **Play Scene** seeks and plays through that scene's spoken interval. Select prose in the viewer and click **Play Selected Prose** to play its matched range. During playback and seeking, the matching sentence is highlighted using stored source offsets. Click the waveform to seek or select a timed scene. Scene boundaries appear on the time ruler. To correct a scene boundary, edit the start and end fields in seconds and click **Save Scene Times**. Corrections are marked manual, validated against audio duration, and preserved across ordinary scene detail edits. Splits and merges recalculate timing from existing words; a changed range discards its old manual correction.

Word, sentence, paragraph, and scene timing is stored in SQLite with the exact source hashes. If prose or audio changes, stale timing is hidden and cannot be used for playback or export. A completed transcript or forced alignment checkpoint is reused only for the same audio hash and model settings. A failed or canceled new run does not replace the active alignment. **Export Scenes** includes numeric seconds in JSON and readable times in Markdown; choose **Detailed JSON** for words, sentences, transcript, and issues. Unresolved passages have null timing rather than estimated timing.

If FFmpeg or FFprobe is missing, install `ffmpeg-free` and verify both commands are on `PATH`. If `torch` or `whisperx` cannot be imported, check that the app and dependencies are installed in the same Python environment; WhisperX 3.8.6 cannot be installed into Python 3.14. If CUDA is selected but unavailable, use CPU or follow the [PyTorch installation selector](https://pytorch.org/get-started/locally/) for a compatible driver and wheel. On out-of-memory errors, try `tiny` or `base`, batch size 1, a shorter chunk, or CPU. Unsupported detected languages need a compatible alignment model; Stage Three does not guess word timing without one.

Known limitations: WhisperX's recognition and language-specific alignment can miss or mishear speech, especially repeated or revised passages, numbers, unusual symbols, and overlapping voices. Those words and scenes need review. Manuscript matching is order preserving and conservatively leaves very large ambiguous passages unresolved. Transcription is saved after its full first pass, so interruption during that pass requires retranscription. A manually corrected scene boundary changes scene playback/export timing only; it does not rewrite word timestamps. Model installation and real model inference depend on the local Python, codec, and hardware combination and have not been tested on every Fedora system.

## Planning visual shots (Stage Four)

A narrative **scene** is a continuous part of the story. A **shot** is one planned view during part of that scene. One scene may have many shots: an establishing view, a character close-up, an object insert, and a view through a window can all belong to the same scene. Shots never modify narrative scene boundaries, manuscript text, or verified word alignment.

Open a project with detected scenes and choose the **Storyboard** tab beside **Scenes**. Select a scene in the Scenes tab, then choose **Generate Scene**. **Generate Selected…** opens a multiple-scene picker; **Generate All** processes every scene. Generation uses the same AI provider, model, and environment credential configured under **File → AI Scene Detection Settings**. It runs on a background thread, and **Cancel** stops at the next request boundary. Replacing an existing scene plan asks for confirmation because manual shot edits may be lost. Other scenes remain untouched when regenerating one scene. A failed or canceled generation preserves saved plans. No API key is needed to open, edit, or export an existing storyboard.

Open **File → Storyboard Settings…** or use **Settings…** in the Storyboard tab to save per-project settings. Low density targets roughly 12–20 seconds per shot, Medium 6–12 seconds, and High 3–6 seconds. Custom density uses the entered target duration. These guide the model; visible changes in the story determine breakpoints. Minimum and maximum durations limit which shots can be marked ready; enable duration exceptions only when you accept unusually short or long shots. Settings also cover visual style, camera movement intensity, establishing views, close-ups, environmental details, and narration pause assignment. The exact settings and fingerprint used in each generation stay with its storyboard.

The AI chooses visual content and labeled source sentence breakpoints. Python extracts each passage from the manuscript using exact half-open character offsets, then derives shot boundaries from existing aligned words and scene timing. The AI does not supply timestamps. For fully timed scenes, shots continuously cover the scene's assigned visual interval; pauses between shots or adjacent scenes belong to the previous or next shot according to the project setting. Scene visual coverage is stored separately on each storyboard from its constituent spoken words. Long pauses are noted in shot timing details. Missing or uncertain word anchors yield partially timed shots or an untimed draft; the application never guesses precise times. Such storyboards are marked **Draft**, and only validated complete timelines are **Ready** for later rendering. Correct the alignment or review and explicitly edit shot times before relying on synchronization.

The shot list shows numbers, titles, start and end times, durations, shot types, descriptions, and review status. Its preview area is a placeholder. Select a shot to see its exact prose passage and edit foreground, background, environment, characters, appearances, actions, props, framing, angle, position, movement, lighting, atmosphere, transition, suggested motion, and visual description. **Save Shot** persists changes and marks descriptive edits as manually edited; an unchanged shot can be marked Reviewed. Start and end fields accept seconds or can be left blank for unresolved timing. Invalid, overlapping, or out-of-scene intervals are rejected. The model may make cinematic choices about framing and lighting, but review all claimed character, prop, and event details against the source.

**Split…** offers internal sentence boundaries. When that boundary has verified word timing, both resulting shots retain continuous audio coverage; otherwise they need timing review. **Merge Next** combines adjacent source and time ranges. **Insert…** partitions a shot and creates a new editable visual description. **Move Visual Down** swaps the visual plans of adjacent timeline slots and marks both for review against their passages; source and time order remain chronological. **Delete…** asks whether the previous or next shot should cover the removed interval, so no gap is silently left. These operations preserve the exact manuscript source ranges and do not change the alignment.

The blue storyboard timeline draws timed shots in proportion to duration, with a red playback indicator. Click a region to select its shot; **Play Shot** seeks the existing narration player to the shot's start and stops at its end. Selecting a shot scrolls to and highlights its exact prose range. Selecting a passage inside one shot identifies that shot. During playback, the active shot follows the audio. Scene, shot, and sentence playback highlights use separate layers. Untimed draft shots have no synchronized timeline region. A changed manuscript, scene boundary, or audio alignment is identified as stale and blocks timed playback or export until reviewed or regenerated.

Use **Export…** in the Storyboard tab for complete structured JSON or a scene-ordered Markdown breakdown with source passages, timing, visual descriptions, and camera details. Unresolved timing is labeled explicitly. Exports contain no credentials. Stage Four creates no image assets, animation, motion effects, or rendered clips. A later stage can use the structured shot fields and stable entity references for image generation; image prompts, reference images, and asset approvals remain future work.

Known limitations: live AI output depends on provider access, response quality, and context limits; the automated workflow uses a deterministic fake provider and has not verified a live generation request. Very long individual source tokens cannot be split safely into planning chunks. The continuity references here are textual and still need human review; there is no visual reference-image system. Shot timing remains draft where Stage Three alignment is uncertain. The timeline is a planning view, not video playback.
