# Teaser projects

A teaser is an image with a quote on it, made to show an upcoming story before the story is out. Like a card, it is built only from reviewed parts, and its image is reviewed by the moderator and the owner before it goes anywhere. Posting it, minting it, both or neither come later and are optional.

A teaser is drawn in layers: a **background** from a scene, then the **identity** painted into it (or, for a viewpoint identity, its frame over the top), then the **quote**.

> Checking a teaser's identity, and posting or minting teasers, are on the way ([epic #47](https://github.com/smk762/argus-mint/issues/47)). This page covers the project's files, the checks on them, their review, backgrounds, teaser drafts and training an identity's LoRA, which are here now.

## Files

One directory per project, under `ARGUS_MINT_PROJECTS_DIR` (default `projects`). A real project usually lives outside any repository. [projects/example-teasers](https://github.com/smk762/argus-mint/tree/dev/projects/example-teasers) is a neutral example.

| File | Holds |
|---|---|
| `project.yaml` | `title` (the working title), `attribution` (a fixed line under each quote, or `null` for none), `generated_label`, the `frames` teasers may use, the SingGuard `policy` (default `default`, darkroom's SFW one), and the checkpoint `family` (default `sdxl`; painting a figure in is SDXL-only for now) |
| `identities/*.yaml` | Who a teaser speaks for. Its `voice`, and its `presence`: **`embodied`** (painted in from `prompt`, and kept consistent by a `lora` once a training run passes argus-proof, pinned with its `proof_run` and `proof_export`; see [Training an identity](#training-an-identity)) or **`viewpoint`** (nobody painted in: the image is what they see, and their `frame`, such as a security-feed overlay, says so). It also lists `references`, each with its sha256 and provenance (`generated`, `licensed` or `personal`). A `personal` photo of a real person needs a `consent` record id |
| `scenes/*.yaml` | A setting: checkpoint, sampling, size, a fixed `prompt`, how many `backgrounds` to render for review, and the `character_area` where a figure may be painted in (left out when nobody is) |
| `lines/*.yaml` | One quote each. `text` is verbatim from its `source` and never edited. `published` is what goes on the image (typos fixed, nothing else), and defaults to `text`. No model writes or reads a line |
| `teasers/<project>-v<n>.json` | The teaser links, one file per version, with the project's id. The teasers by number (`T1`, `T2`…): for each, its `line`, `identity`, `scene` and `frame` (by id and version), its `alt` text, and the chosen `background` once there is one |

Every file, the teaser links included, has an `id`, a `version` and a `status`, and is reviewed like a template: `draft`, then `approved` (with `approved_by` and `approved_at`), then `retired`. A change is a new version. Only approved files make teasers; teasers are made from the newest approved version of the links. Every image they lead to is reviewed again, as a draft.

A scene or identity prompt is fixed text written by the author. A teaser never takes a prompt.

## Checks

Each save (and `GET /projects/{id}`) reports:

- **Problems**: schema errors, and what one file says about another: a teaser naming a file or frame that doesn't exist, or a frame that isn't one of the project's; a line whose voice isn't the identity's; an embodied identity in a scene with no `character_area`; a viewpoint teaser not using its identity's frame; a scene outside the project's checkpoint family; a file belonging to another project.
- **Spoilers**: rule numbers from the private spoiler list that match any text in the file, negatives and sources included.
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
2. **The frame**, with the line's published text in its `quote` slot, the project's `attribution` in `attribution` when it is set, and "generated image" in `label` when `generated_label` is on. A frame without a slot the teaser needs is refused. [frames/example-teaser-v1.yaml](https://github.com/smk762/argus-mint/tree/dev/frames/example-teaser-v1.yaml) is a full-bleed example: the window is the whole canvas.
3. **The PNG** is baked opaque, with only its critical chunks (`IHDR`, `IDAT`, `IEND`): no text, time, colour-profile or size metadata. Stripping metadata (argus-herald does) changes no byte, so its sha256 stays the one reviewed.

The draft has `kind: "teaser"`, no `asset_id`, `consumer` or bio, and a `teaser` record: the project, the teaser links and the number, each linked file by id, version and hash, `presence`, the background's id and sha256, and the alt text. The frame is the draft's `frame`, and the quote, attribution and label are its `texts`. It is reviewed like a card ([review.md](review.md)): the moderator (or auto-moderation), then the owner by signing in with the wallet in `to`. A teaser's text isn't edited: it is the reviewed line's. Publishing a teaser as a card is refused; posting and minting teasers come later.

## The tracker

`GET /projects/{id}/tracker` reports, for each teaser of the newest approved links, the stages argus-mint works out, so a UI shows them rather than deciding them:

| Stage | Reached when |
|---|---|
| `quote_final` | the teaser's line is approved |
| `concept_chosen` | its identity and its scene are approved |
| `image_made` | a teaser draft made from the current line, identity and scene is approved (or published) |
| `posted`, `minted` | always `false` for now: posting and minting teasers come later |

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
| `train` | argus-forge | Only a forge whose `/health` says `training: enabled` (the trusted GPU host). `POST /config` (kohya, the trigger, `collect_captions: false`, `dry_run: false`), then `POST /run`, polled through `GET /run/{id}`. The LoRA (`<export>/forge/kohya/output/<name>.safetensors`) is hashed and copied into the LoRA directory as `mint/<name>.safetensors`. A run an earlier try started is followed, not started again |
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

## Capabilities

`capabilities.backgrounds` is true when the moderator token and a darkroom are set and the server isn't read-only. `capabilities.teasers` is true when the moderator token is set and the server isn't read-only (an embodied teaser also needs darkroom).

`capabilities.author_catalogue` is true when the moderator token is set, the projects directory is writable, and the server isn't read-only.

`capabilities.identity_training`: see [Training an identity](#training-an-identity).
