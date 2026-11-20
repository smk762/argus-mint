# Teaser projects

A teaser is an image with a quote on it, made to show an upcoming story before the story is out. Like a card, it is built only from reviewed parts, and its image is reviewed by the moderator and the owner before it goes anywhere. Once approved it can be posted (through argus-herald), minted, both or neither: each is optional.

A teaser is drawn in layers: a **background** from a scene, then the **identity** painted into it (or, for a viewpoint identity, its frame over the top), then the **quote**.

> Checking a teaser's identity is on the way ([epic #47](https://github.com/smk762/argus-mint/issues/47)). This page covers the project's files, the checks on them, their review, backgrounds, teaser drafts, training an identity's LoRA, posting, anchoring, minting and the export, which are here now.

## Files

One directory per project, under `ARGUS_MINT_PROJECTS_DIR` (default `projects`). A real project usually lives outside any repository. [projects/example-teasers](https://github.com/smk762/argus-mint/tree/dev/projects/example-teasers) is a neutral example.

| File | Holds |
|---|---|
| `project.yaml` | `title` (the working title), `attribution` (a fixed line under each quote, or `null` for none), `generated_label`, the `frames` teasers may use, the SingGuard `policy` (default `default`, darkroom's SFW one), and the checkpoint `family` (default `sdxl`; painting a figure in is SDXL-only for now) |
| `identities/*.yaml` | Who a teaser speaks for. Its `voice`, and its `presence`: **`embodied`** (painted in from `prompt`, and kept consistent by a `lora` once a training run passes argus-proof, pinned with its `proof_run` and `proof_export`; see [Training an identity](#training-an-identity)) or **`viewpoint`** (nobody painted in: the image is what they see, and their `frame`, such as a security-feed overlay, says so). It also lists `references`, each with its `file`, `sha256` and `provenance`: an argus-cortex `Provenance` record (see [References](#references)) |
| `scenes/*.yaml` | A setting: checkpoint, sampling, size, a fixed `prompt`, how many `backgrounds` to render for review, and the `character_area` where a figure may be painted in (left out when nobody is) |
| `lines/*.yaml` | One quote each. `text` is verbatim from its `source` and never edited. `published` is what goes on the image (typos fixed, nothing else), and defaults to `text`. No model writes or reads a line |
| `frames/*.yaml` | Optional: the project's own [frames](catalogue.md#a-projects-own-frames), named `<project>/<name>` (such as a viewpoint identity's camera overlay), each with its overlay PNG and fonts beside it. Reviewed and hashed like any frame, through the frame routes. Their sources (HTML, CSS, a capture spec for [`scripts/capture_frame.py`](catalogue.md#building-an-overlay-from-html)) can sit in `frames/src/` |
| `teasers/<project>-v<n>.json` | The teaser links, one file per version, with the project's id. The teasers by number (`T1`, `T2`…): for each, its `line`, `identity`, `scene` and `frame` (by id and version), its `alt` text, and the chosen `background` once there is one |

Every file, the teaser links included, has an `id`, a `version` and a `status`, and is reviewed like a template: `draft`, then `approved` (with `approved_by` and `approved_at`), then `retired`. A change is a new version. Only approved files make teasers; teasers are made from the newest approved version of the links. Every image they lead to is reviewed again, as a draft.

A scene or identity prompt is fixed text written by the author. A teaser never takes a prompt.

## Checks

Each save (and `GET /projects/{id}`) reports:

- **Problems**: schema errors, and what one file says about another: a teaser naming a file or frame that doesn't exist, or a frame that isn't one of the project's; a frame of another project (a project uses global frames and its own); a line whose voice isn't the identity's; an embodied identity in a scene with no `character_area`; a viewpoint teaser not using its identity's frame; a scene outside the project's checkpoint family; a file belonging to another project.
- **Spoilers**: rule numbers from the private spoiler list that match any text in the file, negatives and sources included. `GET /projects/{id}` also reads the project's own frame files as raw text: every string in the YAML **and its comments** (a capture's comments name its sources). Approving such a frame through the frame review route is refused (409, `held by spoiler rule N`). Text drawn into an overlay PNG can't be read: the reviewer checks the overlay by eye.
- **SingGuard**: darkroom's text pre-flight, under the project's `policy`, on the title and attribution, each identity and scene prompt, each line's text and published text, and each teaser's alt text. Negative prompts are left out, since they name what to avoid.

### SingGuard's verdicts are kept in the file

Each file holds SingGuard's verdicts on its own texts, in a `guard` block written by the server:

```yaml
guard:
- field: text            # the field judged ("T1.alt" in teaser links)
  sha256: 3f1c…          # of the text judged
  policy: default
  passed: true
  detail: ''             # the rule or reason when it didn't pass
  checked_at: '2026-11-18T06:31:47Z'
```

A verdict counts while the field's text and the project's policy are the ones it judged: a restart keeps it, and an edit or a change of policy drops it. Like `status`, `guard` is left out of the file's hash, and a client can't send one: a save carrying `guard` is refused.

### References

Each reference's `provenance` is an argus-cortex `Provenance` record, checked with cortex's own models on every save and again at approval:

```yaml
references:
  - file: refs/keeper-01.png
    sha256: 3f1c…
    provenance: {kind: synthetic, source: argus-mint render, license: own,
                 generator: {model: sdxl/juggernaut-xl.safetensors, seed: 7, prompt: "an old lighthouse keeper…"}}
  - file: refs/jo-02.jpg
    sha256: 9a0e…
    provenance: {kind: consented, source: studio shoot, license: likeness release,
                 subject: <as the release names them>, release_sha256: 5b7d…}
```

- Only **cleared** kinds can be approved: `stock`, `synthetic` (an image mint rendered itself is `synthetic`, with its `generator`) and `consented`. Teasers are public, and a `personal` or `curated` source isn't cleared to be shown.
- A `consented` reference shows a real person. Its consent record, `<release_sha256>.json` in `ARGUS_MINT_CONSENT_DIR`, must be the one its provenance names (same release and subject), must bind this image (its `sha256` is in the record's `reference_images`), and must cover `training` of the subject's face on the day it is checked: not revoked, not expired.
- Records are read fresh each time, so a revocation counts at once. They name a person, so the directory stays private: nothing serves, logs or copies a record, and a problem names the reference's file and cortex's refusal code, never the subject.

An identity with a reference problem is reported in `checks.problems` and can't be approved (409). Consent can be revoked or expire after approval, so [training](#training-an-identity) checks again whenever it uses the references: `select` (before the anchors go to curator and the held-out ones into proof's export) and `evaluate` (before proof scores against them) stop with the reason if any reference can't be used that day.

### The spoiler list

A text file outside every repository, at `ARGUS_MINT_SPOILERS` (keep it `chmod 600`). One rule per line, with `#` comments. A plain line matches as a whole phrase, ignoring case and spacing. A `re:` line is a regular expression, also ignoring case. A match is reported only by the rule's number, counting from 1, so no draft, response, log or fixture repeats the spoiler. The server refuses to start when the file is set but can't be read.

## Review

Approving a project file is refused (409) while a spoiler rule matches it, or while any of its texts lacks a passing verdict. A text with no verdict that still holds is judged at approval, and the verdict is written into the file whether it passes or not. With no darkroom configured, a missing verdict can't be made, so such a file isn't approved; a file whose stored verdicts all hold and passed still can be.

Teaser links are approved only when nothing in them is wrong (the problems above) and every line, identity and scene they name is approved.

An approved or retired file never changes: the author saves a new version.

## HTTP

| Endpoint | Auth | |
|---|---|---|
| `GET /projects` | | `{projects: [{id, title, status}]}` |
| `GET /projects/{id}` | | `{id, files, checks}`. Each file is `{kind, id, version, status, hash, file, doc}`; the teaser links are kind `teasers`. `checks.guard` holds the stored verdicts that still hold |
| `POST /projects/{id}/files/{kind}/{item}/{version}` | moderator | `{doc}` writes a draft `project`, `identity`, `scene`, `line` or `teasers` (the links; `item` is the project's id). Leave out `status`, `approved_by`, `approved_at` and `guard`. SingGuard judges the texts first and its verdicts are saved with the file. Returns `{file, checks}`. A new project starts with `files/project/{id}/1` |
| `POST /projects/{id}/files/{kind}/{item}/{version}/review` | moderator | `{decision: "approve" \| "retire", by}` |
| `POST /projects/{id}/scenes/{scene}/{version}/backgrounds` | moderator | start a background job (202): the scene's set, with its `job` |
| `GET /projects/{id}/backgrounds` | | `{sets}`, one per scene version |
| `GET /projects/{id}/scenes/{scene}/{version}/backgrounds` | | one set |
| `GET /projects/{id}/backgrounds/{bg}/image` | | a background's PNG |
| `POST /projects/{id}/backgrounds/{bg}/review` | moderator | `{decision: "approve" \| "reject", by, note}` |
| `POST /projects/{id}/teasers/{n}/draft` | moderator | `{to}`: a teaser draft (201) |
| `GET /projects/{id}/tracker` | | `{project, links_version, teasers}`: each teaser's stages and drafts |
| `GET /projects/{id}/identities/{identity}/{version}/training` | | the training record and each step's status |
| `POST /projects/{id}/identities/{identity}/{version}/training/{step}` | moderator | start one training step (202) |
| `GET /projects/{id}/identities/{identity}/{version}/training/candidates/{n}` | | a candidate's PNG |
| `GET /posts/ready` | | approved teaser drafts to post ([below](#posting)) |
| `POST /drafts/{id}/posted` | poster or moderator | `{where, url, at, sha256}`: record a post |

## Backgrounds

A scene's backgrounds are rendered by a job: `POST /projects/{id}/scenes/{scene}/{version}/backgrounds` (moderator). Both the scene and the project must be approved, and no spoiler rule may match the scene. The job renders, through darkroom at `batch` priority (behind a person waiting on a draft), each of the scene's `backgrounds` that has no image yet:

- The prompt, negative prompt, checkpoint, sampling and size are the scene's own. The one LoRA is the project's `style_lora`, when it has one, and each background's `request` records it. With a style LoRA, darkroom must have a workflow for that checkpoint with one LoRA (`<family>+lora1.json`): the job is refused (409) before anything renders when its `/models` says it hasn't.
- Background *n*'s seed is derived from the project, scene id, version and *n* (`backgrounds.background_seed`), so the same scene always asks for the same renders.
- A safety rejection is retried with the next derived seed, up to the scene's `max_attempts`. Every attempt is recorded.
- A background the safety check refused every time is recorded in `failed` with `final: true`, and no later job renders it again. One stopped by an error (`final: false`) is tried again by the next job.

They are stored outside the repository, under `ARGUS_MINT_BACKGROUNDS_DIR` (default `backgrounds`): `<project>/<scene>-v<version>/manifest.json`, plus one PNG per background, exactly as darkroom returned it. Each background has an id (`<scene>-v<version>-<nn>`, such as `lamp-room-v1-02`), its `sha256`, the `request` and `attempts`, `made_with`, and a `status`: `pending`, then `approved` or `rejected`. Only one job runs at a time; a job cut off by a restart reads as `failed`.

The moderator looks at each one (`GET /projects/{id}/backgrounds/{bg}/image`) and approves or rejects it (`POST /projects/{id}/backgrounds/{bg}/review`, `{decision, by, note}`). The decision is bound to the image's sha256 and made once. Only an approved background goes into a teaser. Choose it as the teaser's `background` in a new version of the teaser links, and approve that version.

## Teaser drafts

`POST /projects/{id}/teasers/{n}/draft` (moderator, `{to}`) makes the draft for teaser `T<n>` of the newest approved teaser links. It is refused (409) unless:

- the project has no problems, and no spoiler rule matches any linked text (the project, line, identity, scene and the teaser's entry);
- the teaser links, project, line, identity and scene are approved, and so is the frame;
- every text of those files has a stored SingGuard verdict that still holds (same text, the project's policy) and passed;
- the teaser has a `background`, rendered from its scene, approved, and unchanged on disk.

Then the layers:

1. **The art.** For an **embodied** identity, its `prompt` (and `negative`) is painted into the scene's `character_area` on the background, through darkroom's inpainting: the background as the input image, a mask that is white over the area, `denoise` 1.0. The checkpoint, sampling and size are the scene's. The LoRAs are the identity's `lora` (once pinned) and then the project's `style_lora`, never more than two. Inpainting needs SDXL, and darkroom must have an inpainting workflow for that many LoRAs: its example workflows stop at one (`sdxl+lora1+inpaint.json`), so a teaser with both LoRAs needs a `sdxl+lora2+inpaint.json` in its workflow directory. The draft is refused (409) before rendering when darkroom's `/models` says it can't. The seed is derived from the project, the teaser number and the background's sha256. A safety rejection is retried with the next derived seed, and every attempt is recorded. Every pixel outside the mask stays the background's. For a **viewpoint** identity the background itself is the art, and nothing is rendered.
2. **The frame**, with the line's published text in its `quote` slot, the project's `attribution` in `attribution` when it is set, and "generated image" in `label` when `generated_label` is on. A frame without a slot the teaser needs is refused. [frames/example-teaser-v1.yaml](https://github.com/smk762/argus-mint/tree/dev/frames/example-teaser-v1.yaml) is a full-bleed example: the window is the whole canvas. The example project's viewpoint identity, `lamp`, uses the project's own [frames/harbour-feed-v1.yaml](https://github.com/smk762/argus-mint/tree/dev/projects/example-teasers/frames/harbour-feed-v1.yaml) (`example-teasers/harbour-feed`), a harbour-camera overlay, also full bleed, with mild [effects](catalogue.md#effects) on the background (lens bulge, colour fringing, scanlines, noise, vignette).
3. **The PNG** is baked opaque, with only its critical chunks (`IHDR`, `IDAT`, `IEND`): no text, time, colour-profile or size metadata. Stripping metadata (argus-herald does) changes no byte, so its sha256 stays the one reviewed.

The draft has `kind: "teaser"`, no `asset_id`, `consumer` or bio, and a `teaser` record: the project, the teaser links and the number, each linked file by id, version and hash, `presence`, the background's id and sha256, and the alt text. The frame is the draft's `frame`, and the quote, attribution and label are its `texts`. It is reviewed like a card ([review.md](review.md)): the moderator (or auto-moderation), then the owner by signing in with the wallet in `to`. A teaser's text isn't edited: it is the reviewed line's. Once both approve, it can be [posted](#posting), [minted](#minting), both or neither.

Before it is saved, it gets its [identity check](#the-identity-check) and auto-moderation's checks, which are recorded in its `auto_check` whether auto-moderation is on or not.

## The identity check

Does the figure painted in look like the identity it is meant to be? For a teaser of an **embodied** identity, argus-mint asks argus-proof (`POST /score`, `ARGUS_MINT_PROOF_URL`) to score it:

- **the image:** the draft's unframed art (`art.png`), before the frame and text go on;
- **the crop:** the scene's `character_area`, so the background isn't scored;
- **the references:** the identity's `proof_export` (the held-out references its LoRA passed proof against);
- **the scorer:** its `check_metric` (`identity`: ArcFace, a face; `likeness`: DINOv2, anything else), with proof's `target_category` `identity`.

`character_area` is in the scene's render pixels, and that is the art's own pixel space: the background is rendered at the scene's `width` x `height`, the inpainting mask is drawn at that size over that area, and darkroom answers at its input's size. mint checks this rather than assuming it: art of another size, or with an EXIF orientation (proof crops after applying one), isn't sent, and proof's echo of the crop must be the crop sent.

The result is `auto_check.identity`:

| Field | |
|---|---|
| `outcome` | `passed` (`score` ≥ `min_score`), `below`, `uncalibrated` (scored; the identity has no `min_score` yet), `skipped`, `unavailable` or `error` |
| `reason` | the outcome in words, e.g. `likeness 0.412 is under identity keeper v2's min_score 0.6` |
| `presence`, `metric`, `score`, `min_score`, `export`, `crop` | what was asked and what came back |
| `proof_version`, `scorer`, `scorer_version`, `model_file` | proof's provenance: its wire version, and the scorer's name, version and weights (`{name, sha256}`) |
| `at`, `art_sha256` | when, and the art that was scored |

What each outcome does:

- **`passed`** is one of auto-moderation's passing checks.
- **`below`** holds the draft, with the reason in `auto_check.held`. The moderator still decides.
- **`uncalibrated`** records the score and says so, and holds nothing on it: the pilot sets `min_score` from these scores. It can't stand in for the moderator either, so with auto-moderation on the draft waits for a person.
- **`skipped`**: a viewpoint identity has nobody painted in, so there's nothing to score, and that passes. An embodied identity with no `proof_export` yet (no LoRA has passed proof) has no references to score against, so it is held.
- **`unavailable`**: proof didn't answer, answered 503 (its scorer isn't installed, or the GPU lease was refused), or isn't configured. **`error`**: proof refused the request (an unknown export, references that overlap the training images, a bad crop), or its answer can't be read. Both hold the draft: a teaser is never let through unchecked.

When proof was down, the moderator runs the check again with `POST /drafts/{id}/identity-check` (moderator token; a pending teaser only, 409 otherwise). It scores the same art against the same identity and scene files the draft was made from (409 if they've changed), and records auto-moderation's checks again; with auto-moderation on and every check passing, that records the moderator's approval. The route is a sub-resource of the draft like `/review` and `/edit`, named for the record it rewrites and for the `identity_check` capability. It reruns the check rather than being a special retry, so it works whatever the last outcome was.

The check never approves on its own. Auto-moderation records the moderator's approval only when it is on and every check passed, the identity check's included, and the owner always reviews.

## The tracker

`GET /projects/{id}/tracker` reports, for each teaser of the newest approved links, the stages argus-mint works out, so a UI shows them rather than deciding them:

| Stage | Reached when |
|---|---|
| `quote_final` | the teaser's line is approved |
| `concept_chosen` | its identity and its scene are approved |
| `image_made` | a teaser draft made from the current line, identity and scene is approved (or published) |
| `posted` | a draft of the teaser (current or not) has a post recorded |
| `minted` | a draft of the teaser (current or not) is published: pinned, with metadata and a signed voucher. Whether the voucher was then sent on-chain is the Card contract's to say |

Each teaser also lists its chosen `background` and its drafts, newest first: `{id, status, awaiting, created_at, links_version, background, current}`. `current` is false for a draft made from another line, identity or scene than the current links name. With no approved links, `links_version` is `null` and `teasers` is empty.

## Training an identity

An embodied identity keeps its look from one teaser to the next through its own LoRA. argus-mint runs the training through the suite and keeps a record of every step; the moderator advances it one step at a time, and approves the result like any other file.

### What the identity file says

The identity names how it is trained, and is reviewed with it:

```yaml
check_metric: identity      # proof's metric: identity (ArcFace) for a face, likeness (DINOv2) otherwise
min_score: null             # its threshold; unset until the pilot sets one
training:
  model: sdxl/base.safetensors   # the checkpoint, as darkroom and proof name it
  base_model: null               # the same checkpoint as argus-forge names it (null: forge's default, SDXL base 1.0)
  sampling: {sampler: dpmpp_2m, scheduler: karras, steps: 28, cfg: 6}
  width: 1024
  height: 1024
  candidates: 60                 # renders curator chooses from
  train: 30                      # training images
  held_out: 8                    # references, never trained on
  trigger: kpr_keeper            # first in every caption; the LoRA's trigger word
  caption_style: photo           # lens's target_style (photo or anime)
  max_attempts: 4                # per candidate, for safety rejections
  proof_seeds: 8                 # images proof renders with the LoRA
```

`check_metric`, `min_score` and `training` are left out of the identity's hash while unset, so identities written before them hash as they did. Once a run passes, `lora`, `proof_run` and `proof_export` are pinned, always all three: the LoRA, proof's run, and proof's export holding the held-out references (a directory name under proof's `ARGUS_PROOF_EXPORTS_DIR`). The per-teaser identity check (#51) scores against that export on `check_metric`, with `min_score`.

### The steps

`POST /projects/{id}/identities/{identity}/{version}/training/{step}` (moderator) starts one step as a job. The step is in the path, so each request names the one step meant: no step starts the next, and nothing runs unattended. A step is refused (409) unless the step before it is `done`, nothing of this training is queued or running, and the step itself isn't done (a finished step never runs again; a failed one may, and resumes what it can). Every step also needs the project and identity approved, an embodied identity with `training` and `check_metric` and no LoRA yet, no spoiler rule matching the identity, and a fresh passing SingGuard verdict on its prompt.

| Step | Who | What |
|---|---|---|
| `candidates` | darkroom | `training.candidates` renders of the identity's `prompt` and `negative` on `training.model`, no LoRA, at `batch` priority. Candidate *n*'s seed is derived from the project, identity, version and *n*. A safety rejection is retried with the next derived seed, up to `max_attempts`, and every attempt is recorded; one refused every time is final. Refused (409) up front when darkroom's `/models` has no LoRA-free workflow for the checkpoint. A failed step keeps what rendered: running it again renders only the rest |
| `select` | argus-curator | The candidates (and the identity's references not marked `held_out`, as anchors) are uploaded (`POST /upload`) and scanned with faces and MiVOLO on, anchored on those references (`POST /scan/folder`, `faces.reference_paths`). Curator's `face_source` filter (`POST /scan/{id}/select`) keeps one adult identity, one face per image, within 0.4 of it; pose and expression checks are off, since a training set wants variety. mint ranks the kept images by their distance, takes the closest `train + held_out`, and spreads `held_out` of them evenly through that ranking as references; the rest train. Curator exports both (`POST /export`, by `selection`) into the shared volume, and mint copies the training manifest into the proof export so proof's held-out check sees the training set. The identity's `held_out` references join proof's references. A sha256 check refuses any reference that is a training image |
| `caption` | argus-lens | `POST /caption/batch` with the training images (never the references), `target_style` from `caption_style`, `target_backend` the project's family. mint takes each image's `training` caption (lens's sidecars hold its `identity` caption, the wrong one for a character LoRA), puts the trigger word first, and writes `<image>.txt` beside it |
| `train` | argus-forge | Only a forge whose `/health` says `training: enabled` (the trusted GPU host). `POST /config` (kohya, the trigger, `collect_captions: false`, `dry_run: false`), then `POST /run`, polled through `GET /run/{id}`. The LoRA is the one forge reports on the succeeded run (`lora`: its `export_path` in the export and its `sha256`, read from the config the trainer ran); mint refuses it when forge reports none or the file hashes differently, then copies it into the LoRA directory as `mint/<name>.safetensors`. A forge from before that field (no `lora` key) falls back to `<export>/forge/kohya/output/<name>.safetensors`. A run an earlier try started is followed, not started again |
| `evaluate` | argus-proof | `POST /run/stream`: the LoRA on `training.model`, the trigger and the identity's prompt, `proof_seeds` seeds, against the proof export. Then `GET /report/{run}`. It **passes** when proof's verdict passed and isn't pending a person's ratings, `check_metric`'s mean was scored, and it is at least `min_score` when one is set. `likeness` carries no weight in proof's verdict, so a likeness identity needs a `min_score` to pass |
| `pin` | | The report is read again, so ratings given in proof's review since count. Refused unless it passes, and unless `GET /report/{run}/manifest` names exactly the LoRA forge trained (name and sha256). Then a **draft** identity version (the next number) is saved with `lora` (and the trigger word), `proof_run` and `proof_export`, through the same checks as any save. The approved file trained from never changes |

The pinned version is reviewed like any project file. Approving an identity with a `lora` is refused (409) unless a training record shows a passing run pinned exactly that LoRA, run and export into that version, so a LoRA can't be approved by writing its name into a file.

**A face, for now.** Curator measures consistency only between faces (`face_source`). A `likeness` identity (no humanlike face) is refused at `select`, and the record says why; the pilot's identity is a face.

### The record

`GET /projects/{id}/identities/{identity}/{version}/training` returns `{project, identity, version, steps, next, record}`. `steps` is each step's status as mint works it out (`not_started`, `queued`, `running`, `done`, `failed`; a job a restart cut off reads as failed) and `next` is the one step the moderator may start now. The record keeps every run of every step (`runs`: its inputs, the sibling's scan or run id, its outputs and error), the candidates (seed, request, attempts, sha256), the selection (curator's scan id, the training and held-out files and their sha256), the captions, the LoRA (name, sha256, forge's run), proof's evaluation (run, score, verdict, reasons) and what was pinned. Records live under `ARGUS_MINT_TRAINING_DIR` (default `training`): `<project>/<identity>-v<version>/record.json` and `candidates/`.

### Deployment: the shared volumes

Curator, forge and proof read and write files, not uploads (lens takes uploads), so the training needs two volumes they share with argus-mint:

- **The exports volume.** Curator's export root (`ARGUS_CURATOR_EXPORT_ROOT`), forge's export root (`ARGUS_FORGE_EXPORT_ROOT`) and proof's exports directory (`ARGUS_PROOF_EXPORTS_DIR`) are the same directory, mounted at the same path in all three (curator's manifest records absolute paths), and mounted in argus-mint at `ARGUS_MINT_TRAINING_EXPORTS_DIR`. It holds `mint-<project>-<identity>-v<version>` (the training images, captions, curator's manifest and forge's output) and `…-proof` (`references/` and a copy of the training manifest).
- **The LoRA directory.** The `loras/` directory darkroom's ComfyUI and proof (`PROOF_MODELS_DIR/loras`) load from, mounted in argus-mint at `ARGUS_MINT_LORAS_DIR`.

Curator takes the candidates by `POST /upload` into `mint/<project>/<identity>-v<version>` under its source root. Forge trains only with `ARGUS_FORGE_READONLY=0`, its train image and a GPU: the trusted host.

`capabilities.identity_training` is true when darkroom, `ARGUS_MINT_CURATOR_URL`, `ARGUS_MINT_LENS_URL`, `ARGUS_MINT_FORGE_URL`, `ARGUS_MINT_PROOF_URL`, both directories and the moderator token are set, and the server isn't read-only. When it is false, an embodied identity is painted in from its prompt alone: its file has no `lora`, and its teaser drafts record that.
## Posting

argus-herald posts approved teasers; argus-mint never pushes to it. Herald, on a laptop, polls `GET /posts/ready`, fetches each image from its `image_url`, posts it, and reports the post with `POST /drafts/{id}/posted`.

### `GET /posts/ready`

No auth (a read). Approved teaser drafts (`approved` or `published`), newest first by `created_at`, paged with `?limit=` (1–200, default 50) and `?offset=`; `?project=<id>` keeps one project's. A draft that already has posts stays listed, with `posted: true`, so a poster can skip it.

```json
{
  "items": [
    {
      "id": "3f9c0e1a2b4d5e6f",
      "project": "example-teasers",
      "title": "Lantern Coast",
      "number": "T1",
      "status": "approved",
      "current": true,
      "created_at": "2026-11-18T09:00:00Z",
      "approved_at": "2026-11-18T09:20:00Z",
      "image_url": "/drafts/3f9c0e1a2b4d5e6f/image",
      "image_sha256": "9b1f…",
      "alt": "An old lighthouse keeper in an oilskin coat stands beside the great brass lens on a rainy night.",
      "quote": "Forty years I've kept it lit, and not once has the sea said thank you.",
      "attribution": null,
      "label": "generated image",
      "posted": false,
      "posts": []
    }
  ],
  "total": 1
}
```

- `image_url` is relative to argus-mint's base URL. Its bytes hash to `image_sha256`, the image both reviewers approved. The PNG has no metadata chunks, so stripping metadata leaves the hash as it is.
- `alt` is the teaser's alt text from the links; `quote` the line's published text as drawn; `attribution` and `label` what the project draws (`null` when it doesn't).
- `current` is false for a draft made from another line, identity or scene than the current links name for that number (a newer draft supersedes it). Such a draft is still approved and still listed.
- `title` is the project's working title, as the draft was named with it (`<title> T<n>`).
- `approved_at` is the last approval of the current image.

### `POST /drafts/{id}/posted`

Auth: `Authorization: Bearer <ARGUS_MINT_POSTER_TOKEN>` (or the moderator token). The poster token is accepted here and nowhere else.

```json
{"where": "bluesky", "url": "https://bsky.app/profile/…/post/…", "at": "2026-11-18T09:30:00Z", "sha256": "9b1f…"}
```

- `where`: the platform, lower case (`^[a-z0-9][a-z0-9._-]{0,31}$`).
- `url`: the post, `http(s)`, or `null` when there is none.
- `at`: when it was posted, with a time zone.
- `sha256`: of the bytes posted. It must equal the draft's `image_sha256`.

A draft can have several posts (one per platform, or reposts). Each is kept in the draft's directory, in `posts.json`, with `recorded_at` and `by` (`poster` or `moderator`). The answer is `{id, image_sha256, posts}`: 201 for a new post, 200 when the same post (`where`, `url`, `at`, `sha256`) was already recorded, so a retry records nothing twice. `GET /drafts/{id}` also lists the `posts`.

| Code | When |
|---|---|
| 401 | no token, or not the poster's or the moderator's |
| 403 | the server has neither token set |
| 404 / 400 | no such draft / a malformed id |
| 409 | `sha256` isn't the draft's image; the draft isn't a teaser; it isn't approved (pending or rejected); its image on disk differs from the one reviewed |
| 422 | a malformed body: `where`, `url`, `at` without a time zone, `sha256` |

## Anchoring

`argus-mint teasers anchor <project> --ledger 0x… --epoch N [--tree FILE]` anchors a Merkle root of the project's posted teasers in the Ledger, the way `argus-mint anchor` does for any leaves (the same `merkle` and Ledger code, the same pre-check that the epoch is free, the tree written before the transaction is sent, the account from `--account` or `$ARGUS_MINT_SIGNER_KEY`, never a key on the command line).

- **Leaves:** each posted, approved teaser of the project whose image no earlier anchor holds. A leaf's value is the 32-byte sha256 of the posted PNG (the reviewed image), and its hash is OpenZeppelin's `StandardMerkleTree` `["bytes32"]` leaf, `keccak256(keccak256(sha256))`. Anyone holding the image can rebuild the leaf and check its proof against the anchored root (`Ledger.verify`). An image posted from two drafts is one leaf.
- **Batches:** each run anchors what's new since the last, on a new epoch. With nothing new it refuses.
- **Kept with the project, outside the repo:** under `ARGUS_MINT_ANCHORS_DIR` (default `anchors`), `<project>/anchors.json` lists every anchor (`epoch`, `root`, `count`, `leaves` with each draft and number, `tree`, `network`, `chain_id`, `ledger`, `tx`, `block`, `sender`) and `<project>/epoch-<N>-tree.json` is the tree (or `--tree`). It reads the drafts from `ARGUS_MINT_DRAFTS_DIR`, so run it where the server's drafts are.

## Minting

Publishing is optional for any approved draft, a card or a teaser: `POST /drafts/{id}/publish` or `argus-mint publish`, as for a card ([drafts.md](drafts.md#publishing)). For a teaser:

- **Asset id:** `keccak256(utf8("argus-mint/teaser/<project>/<T n>"))` (`metadata.teaser_asset_id`). Every draft of a teaser has the same one, so a teaser mints once on a Card contract.
- **Metadata:** the same document as a card's (`name`, `description`, `image`, `attributes: []`, `provenance`), with `description` the alt text and a teaser's `provenance`:

```json
{
  "kind": "teaser",
  "project": {"id": "example-teasers", "version": 1, "hash": "0x…"},
  "links": {"id": "example-teasers", "version": 1, "hash": "0x…"},
  "number": "T1",
  "line": {"id": "keep-it-lit", "version": 1, "hash": "0x…"},
  "identity": {"id": "keeper", "version": 1, "hash": "0x…"},
  "scene": {"id": "lamp-room", "version": 1, "hash": "0x…"},
  "presence": "embodied",
  "background": {"id": "lamp-room-v1-01", "sha256": "…"},
  "frame": {"id": "example-teaser", "version": 1, "hash": "0x…"},
  "art": "ipfs://<the unframed art>",
  "asset_id": "0x…",
  "inpaint": {"seed": 123, "attempts": 1, "model": "sdxl/…", "loras": [{"name": "…", "weight": 0.8}],
              "image_sha256": "<the background's>", "mask_sha256": "…", "denoise": 1}
}
```

`inpaint` is there for an embodied identity only (`loras` left out when there are none); a viewpoint teaser's art is the background itself. The voucher's `contentHash` is `metadata.content_hash()`, as for a card. A card's metadata is unchanged by any of this.

## Export

`argus-mint export <project> --out DIR` writes, for each teaser number with an approved draft, `T<n>.png` (byte for byte the reviewed image) and `T<n>.json`, its record: the draft id and status, each linked file by id, version and hash, the background (with its seeds, checkpoint and LoRAs when `--backgrounds` or `ARGUS_MINT_BACKGROUNDS_DIR` is given), the frame and texts, the render (template, final seed, every attempt's seed, checkpoint, LoRAs, the inpainting input), `made_with`, the reviews, the posts and each anchor holding the image, with its Merkle proof. For a number with several approved drafts, the newest posted one is written, else the newest.

## Capabilities

`capabilities.post` is true when a poster or moderator token is set and the server isn't read-only.

`capabilities.backgrounds` is true when the moderator token and a darkroom are set and the server isn't read-only. `capabilities.teasers` is true when the moderator token is set and the server isn't read-only (an embodied teaser also needs darkroom).

`capabilities.author_catalogue` is true when the moderator token is set, the projects directory is writable, and the server isn't read-only.

`capabilities.identity_training`: see [Training an identity](#training-an-identity).

`capabilities.identity_check` is true when `ARGUS_MINT_PROOF_URL` is set and the server isn't read-only: an embodied identity's teasers are scored ([The identity check](#the-identity-check)). Without it they are held as unchecked.
