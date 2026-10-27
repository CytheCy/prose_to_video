# Prose to Video

Prose to Video is a Fedora KDE desktop application for organizing written stories and their narrated audio. Stage Seven adds AI image generation, candidate review, and explicit shot image approval. Video assembly and rendering remain future work.

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

The database stores project and scene records. Exported scene breakdowns can be saved in `scenes/`. The `references/` folder stores imported reference images and cached thumbnails; `images/` stores generated originals and separate thumbnails. `video_clips/`, `previews/`, and `output/` reserve space for later stages. Source paths are relative to the project folder, so moving the whole folder keeps references valid. A missing source file appears as missing in the project panel and can be replaced with a new import.

The SQLite schema uses `PRAGMA user_version`. Version 1 contains the Stage One `project` table. Version 2 adds `scene_analysis` and `scene` tables. Version 3 adds alignment runs, indexed words, scene timings, and transcript checkpoints. Version 4 adds project storyboard settings, storyboard runs, active scene plans, and indexed shot records. Version 5 adds visual entities, aliases, evidence, variants, reference image metadata, scene and shot relationships, style profiles, and an audit trail. Version 6 adds project prompt settings, versioned image prompts, active and approved version pointers, and reference relationships. Version 7 adds image generation settings, persistent jobs, generated candidates, and approved image selections. Candidate metadata includes source fingerprints, parent images, provider usage, and costs when available. Existing older projects migrate in place when opened; unknown newer versions are refused. Scene, alignment, storyboard, and prompt-version writes are transactional, so failed writes preserve preceding results.

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

Use **Export…** in the Storyboard tab for complete structured JSON or a scene-ordered Markdown breakdown with source passages, timing, visual descriptions, and camera details. Unresolved timing is labeled explicitly. Exports contain no credentials. Stage Four creates no image assets, animation, motion effects, or rendered clips. Stage Five adds stable entity references and approved images for later image generation.

Known limitations: live AI output depends on provider access, response quality, and context limits; the automated workflow uses a deterministic fake provider and has not verified a live generation request. Very long individual source tokens cannot be split safely into planning chunks. Continuity still needs human review. Shot timing remains draft where Stage Three alignment is uncertain. The timeline is a planning view, not video playback.

## Visual continuity and reference assets (Stage Five)

Open the **Visual Library** tab after importing prose and detecting scenes. The left side lists entities. Search by name or alias and filter by category. Use **Add Entity** to create one manually, or **Extract Selected Scene**, **Extract Multiple Scenes…**, or **Extract All Scenes** to analyze existing scene source ranges with the configured AI provider. Extraction runs in a worker thread and processes long scenes in bounded chunks. **Cancel Extraction** stops at a request boundary. The provider must be configured under **File → AI Scene Detection Settings**. No key is needed for manual library work.

Extraction proposes visual identities and records appearance details as separate evidence. An exact quote in the manuscript is labeled **explicit**; inference and AI proposals remain distinct. An AI detail with no matching quote cannot become an explicit fact. Select an entity to review its description and evidence. Double-click an evidence row to mark it Approved, Reviewed, Needs Review, or Rejected. The main entity fields can be edited independently; unknown features can stay blank. Saved manual appearance edits are labeled as approved user design choices in the evidence record. Approving an AI detail makes it available to future reference packages, but extraction never overwrites an approved entity or image. Reanalysis is safe for previously reviewed work. Check suggestions for ambiguous identities and use aliases, entity type corrections, and the review status controls as needed. AI-suggested aliases appear as evidence and become active aliases only after approval.

Use **Merge Entities** to combine duplicate identities after confirmation. Scene and shot links, evidence, variants, and image associations move to the surviving stable ID. **Separate Selected Links** moves a chosen scene, shot, image, evidence record, or variant to a new entity if two identities were conflated. Double-click a related scene or shot to remove its association. A manually removed shot link is remembered and will not be restored by automatic reconciliation. The audit log retains entity merge and separation details. Entity deletion asks for confirmation and leaves imported image files available for other associations.

Use **Import Image** to copy a PNG, JPEG, or WebP into `references/`. The original is not changed. Image processing runs on a worker thread. The app validates image content, records dimensions, format, SHA-256, and import time, and creates a cached 256-pixel thumbnail. Identical files reuse one asset record; distinct files with the same filename get unique internal names. Choose a role such as portrait, full-body view, establishing view, front view, or detail view, and add an optional description. Use **Approve** to preserve an image as a continuity reference and **Set Primary** to identify the main approved view. Removing an approved association asks for confirmation. Reference paths stay relative to the project, so moving the whole project folder preserves them. On open, missing or externally changed files appear as warnings; the app never silently substitutes another image. An unreferenced asset cleanup API is available for confirmed maintenance operations.

Use **Add Variant** for a named change in clothing, damage, setting, or another legitimate narrative appearance. Double-click a variant to edit its description, status, scene context, and default setting. Only approved variants are offered when linking an entity to a shot. The **Storyboard** panel displays associated entities and the count of approved images. Use **Add Visual Entity…** or **Remove Selected Entity** there to edit shot links. **Pin Approved Image…** attaches a chosen approved view directly to the shot; **Unpin Selected Image** removes that choice. Explicit name mentions in a shot can establish an initial association; pronouns and ambiguous names remain for review. Selecting an entity in the library shows its related scenes, shots, and the number of affected shots. Changes to references do not rerun scene detection, alignment, or storyboard timing.

The project visual style section stores artistic intent: aesthetic, rendering approach, palette, lighting, camera style, realism, textures, rules, and negative constraints. Use **Save Style** for the project profile, **Save Preset** and **Load Preset** for profiles within a project. **Export Style** and **Import Style** move a profile between projects. **Override Scene** and **Override Shot** apply nonempty fields from the editor at the selected scope; shot fields take precedence over scene fields, which take precedence over project fields. This is independent of any future image model settings.

Use **Inspect Continuity** to review possible duplicate identities, differing source descriptions, missing references for recurring characters, missing links and image files, and a variant assigned outside its scene context. Confirmed and possible warnings are labeled separately. Inspection does not change prose or shots. **Export JSON** writes stable IDs, aliases, evidence, variants, image metadata, relationships, and style information. **Export Markdown** writes a readable guide by category with relative image paths. **Export Style** writes the project style as its own JSON file. Exports contain no image binaries or API credentials. The provider-independent reference package API includes the effective style, approved entity descriptions and details, approved variants, approved image metadata, and source-grounded constraints for a shot.

Stage Five prepares reference assets only. It does **not** generate images, animate them, or render a final video. Known limitations: AI suggestions require human review; pronouns and uncertain identity matches are not automatically merged; the continuity inspector is conservative and may flag legitimate descriptive changes; image integrity verification can take time in projects with many large assets; live provider results depend on network access, credentials, and model behavior. The Stage Five extraction tests use a deterministic fake provider and do not make external API requests.

## Image prompt generation (Stage Six)

Open a project with narrative scenes and a storyboard, select a shot in **Storyboard**, then open **Image Prompts**. **Generate Prompt** creates one version for that shot. **Generate Scene** and **Generate All** process saved storyboard shots in order and skip shots that already have a prompt, including approved ones. **Regenerate Prompt** explicitly creates a new version. If a prompt is approved, its approved version stays active while the new version is saved in history for review. **Cancel** stops after the current AI request; completed versions remain saved. Generation runs in a worker thread. Use **Settings…** to save project-specific detail level, composition, environment and character emphasis, continuity strictness, environmental additions, negative prompts, neighboring-shot context, and whether a batch pauses after each prompt. The model field can inherit the application's **AI Scene Detection Settings** model. Credentials continue to come from that existing environment-variable setting and are never saved with the project.

The language-model request contains the selected shot's exact source passage and offsets, preceding prose in the same scene, scene and storyboard details, limited neighboring-shot continuity context, approved Visual Library written design details, selected approved appearance variant, effective project/scene/shot style, and output aspect ratio derived from video dimensions. Later prose is excluded from the surrounding passage to avoid premature reveals. Reference-image identifiers and metadata remain in the local prompt package; image binaries are not sent to the language model. The generated positive prompt is assembled deterministically from editable narrative, composition, camera, foreground, middle ground, background, environment, appearance, action, object, lighting, material, and style fields. Negative text is kept separate for future image providers that support it.

Edit the prompt fields and choose **Save Changes** to create another version. Use the version selector to view earlier versions, then **Restore Version** to make one active. **Revert Unsaved** reloads the active version. **Approve Prompt** explicitly approves the active version after deterministic validation. Approved versions are retained separately from drafts. **Copy Prompt** copies the positive text for use in an external image tool. **Export…** writes the selected prompt, current scene, or whole project as structured JSON or readable Markdown. Exports include references by identifier and relative path, not image data or API keys. Reference thumbnails in the editor show approved images; missing files are labeled.

Prompt fingerprints track the selected shot, scene description, associated approved visual entities and variants, reference metadata, effective visual style, project dimensions, relevant neighbor context, and generation settings. The editor shows **Outdated** when these inputs change. Updating one entity only affects prompts linked to it. Outdated versions are kept for review; regeneration never deletes them. Approval checks missing prompt text, shot and source association, entity links, missing reference files, stale fingerprints, required named subjects, and conflicting aspect ratio instructions. A stage-seven generation package can be assembled locally from an approved current prompt, its style, ratio, entity details, reference IDs and paths, and dependency fingerprint. It makes no image-generation request.

Known limitations: AI prompt quality and narrative accuracy need human review; a text prompt alone cannot guarantee consistent character appearance. The deterministic validator catches structural problems but cannot prove semantic consistency. Missing approved images are reported while approved written descriptions remain usable. The automated tests use a deterministic fake AI provider; no live API request is claimed. Stage Six does not generate images, animation, or video.

## Generating and reviewing images (Stage Seven)

Open a project with storyboard shots and **approved Image Prompts**. Select a shot, then open **Image Gallery**. Choose **Settings…** to select OpenAI or ComfyUI, model, dimensions, quality, candidate count, output format, reference use, spending cap, and queue settings. The initial OpenAI model is `gpt-image-2.5-flare`; check [current OpenAI image model and size support](https://developers.openai.com/api/reference/resources/images/methods/generate) before changing it. The default 1536 × 864 frame has a 16:9 aspect ratio. A model must support the requested dimensions; the app does not resize originals. The gallery checks the selected provider's capabilities before submission. An approved prompt whose dependencies changed requires an explicit one-shot override for shot generation. The prompt itself and its outdated status remain unchanged.

For OpenAI, set `OPENAI_API_KEY` in the shell that launches the app. The official OpenAI Python SDK calls `images.generate` for text-to-image and `images.edit` for image references or refinement. Keys are read from the environment and never stored in SQLite. OpenAI has no negative-prompt field; the approved negative prompt is appended as an “Avoid” instruction in the submitted text, and the transformation is recorded with the job. Seeds are not offered for OpenAI. The app shows the actual returned model when available. OpenAI's image endpoints, supported model choices, sizes, and quality options can change; see the [Images API reference](https://developers.openai.com/api/reference/resources/images/methods/generate) and [image edits reference](https://developers.openai.com/api/reference/resources/images/methods/edit).

For ComfyUI, start a compatible server yourself and enter its URL (default `http://127.0.0.1:8188`). Use **Import workflow JSON…** to choose an **API-format** workflow. Saving the settings copies that JSON into `storyboard/workflows/` so the project can move with its workflow definition. Map application fields to your workflow's actual node IDs and input names in the JSON mapping editor. A minimal example is:

```json
{
  "positive_prompt": {"node": "6", "input": "text"},
  "negative_prompt": {"node": "7", "input": "text"},
  "width": {"node": "5", "input": "width"},
  "height": {"node": "5", "input": "height"},
  "seed": {"node": "3", "input": "seed"},
  "candidates": {"node": "5", "input": "batch_size"},
  "references": [{"node": "12", "input": "image"}],
  "outputs": ["9"]
}
```

Adapt every node ID to the imported workflow. A field is available only when mapped; reference generation requires mapped image inputs. The adapter validates workflow structure, mapped inputs, and output nodes before submission. It uploads selected references to the server, submits through `/prompt`, checks `/queue` and `/history/{prompt_id}`, and copies completed `/view` images into the project. It does not install ComfyUI or download models. See the [ComfyUI server routes](https://github.com/Comfy-Org/docs/blob/main/development/comfyui-server/comms_routes.mdx).

**Generate Image** produces a candidate for the selected shot using its approved prompt. **Generate Variations** requests at least two candidates. **Generate Scene** and **Generate Project** queue eligible shots from the current scene or whole project. A preflight dialog lists selected, approved, missing, outdated, and skipped shots, plus provider, candidate count, and any available cost estimate. Approved shots are skipped by default in scene and project batches. For a deliberate new candidate on an approved shot, select it and use **Regenerate**. The old approved image remains selected until you explicitly approve another. **Refine Selected** uses a chosen candidate as a source image when the provider supports edits; it creates a new candidate and records the parent.

The gallery shows thumbnails, a larger selected preview, status, provider, model, dimensions, creation time, prompt version ID, and approval state. **Compare Candidates** displays up to four thumbnails together. **Approve Image** selects a candidate for the shot; **Reject Image** marks a candidate rejected. **Lock Approved** protects the selected asset from replacement until **Unlock Approved**. Candidate files are preserved when another is approved. The storyboard shows each shot's image state and approved image preview. A technically valid image still needs human review for story accuracy and character continuity.

The app uses approved Visual Library images from the prompt's reference package, with pinned or primary references first, then character, robot, and vehicle references, then locations and other roles. When a provider limits image inputs, the confirmation dialog lists excluded reference IDs; job details retain included and excluded IDs. Reference content hashes are checked before submission. Reference-conditioned output can improve continuity but cannot guarantee it.

Jobs are saved in SQLite before a provider request. Completed images and thumbnails live under `images/scene_<id>/shot_<id>/` with UUID filenames. Paths in SQLite are relative to the project root, so moving the entire project preserves links. The original provider bytes are retained; thumbnails are separate PNGs. If an image is missing after moving a project, the gallery and manifest identify the missing file rather than substituting another. **Generation Details** shows the saved request settings, reference IDs, transformations, provider job ID, usage, and actual or estimated cost where known. Cost estimates are user-configured and may differ from billing. A spending cap blocks new submissions when no safe estimate is available or the threshold would be exceeded.

On reopening a project, unfinished submitted jobs become **Needs Reconciliation**. ComfyUI jobs with known IDs are checked against server history without resubmitting. OpenAI requests have no job lookup in this adapter; uncertain requests remain flagged and require manual review before any new paid request. **Resume / Reconcile Jobs** can submit pending, never-submitted jobs after confirmation and check known server job IDs. **Cancel Pending** stops the current batch after its active request and marks remaining queued items canceled. A cloud request already submitted may still complete and incur a charge. ComfyUI queued jobs can be removed when the server confirms they are pending.

Use **Export…** in Image Gallery for generation JSON, Markdown, a provider-independent visual timeline JSON, or **Approved images and manifest**. The last option copies approved files to a selected folder with ordered stable filenames and `approved_images.json`; it refuses existing output filenames. The timeline manifest includes each shot's audio range, duration, approved image and dimensions, planned camera movement and transition, and readiness. It does not call a provider.

Known limitations: Live paid OpenAI generation was not exercised by the automated suite. ComfyUI needs a compatible running server, configured workflow, and available model or backing API. The gallery's compare view uses thumbnails and does not offer masks or semantic image scoring. Job progress shows known queue states; OpenAI does not report a percentage. The app does not perform AI animation, automatic camera movement, audio-video assembly, or final rendering. Those are Stage Eight and later work.
