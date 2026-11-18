# Teaser projects

A teaser is an image with a quote on it, made to show an upcoming story before the story is out. Like a card, it is built only from reviewed parts, and its image is reviewed by the moderator and the owner before it goes anywhere. Posting it, minting it, both or neither come later and are optional.

A teaser is drawn in layers: a **background** from a scene, then the **identity** painted into it (or, for a viewpoint identity, its frame over the top), then the **quote**.

> Training identities, checking a teaser's identity, and posting or minting teasers are on the way ([epic #47](https://github.com/smk762/argus-mint/issues/47)). This page covers the project's files, the checks on them, their review, backgrounds and teaser drafts, which are here now.

## Files

One directory per project, under `ARGUS_MINT_PROJECTS_DIR` (default `projects`). A real project usually lives outside any repository. [projects/example-teasers](https://github.com/smk762/argus-mint/tree/dev/projects/example-teasers) is a neutral example.

| File | Holds |
|---|---|
| `project.yaml` | `title` (the working title), `attribution` (a fixed line under each quote, or `null` for none), `generated_label`, the `frames` teasers may use, the SingGuard `policy` (default `default`, darkroom's SFW one), and the checkpoint `family` (default `sdxl`; painting a figure in is SDXL-only for now) |
| `identities/*.yaml` | Who a teaser speaks for. Its `voice`, and its `presence`: **`embodied`** (painted in from `prompt`, and kept consistent by a `lora` once a training run passes argus-proof, recorded as `proof_run`) or **`viewpoint`** (nobody painted in: the image is what they see, and their `frame`, such as a security-feed overlay, says so). It also lists `references`, each with its sha256 and provenance (`generated`, `licensed` or `personal`). A `personal` photo of a real person needs a `consent` record id |
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

## Backgrounds

A scene's backgrounds are rendered by a job: `POST /projects/{id}/scenes/{scene}/{version}/backgrounds` (moderator). Both the scene and the project must be approved, and no spoiler rule may match the scene. The job renders, through darkroom at `batch` priority (behind a person waiting on a draft), each of the scene's `backgrounds` that has no image yet:

- The prompt, negative prompt, checkpoint, sampling and size are the scene's own. No LoRA is used.
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

## Capabilities

`capabilities.backgrounds` is true when the moderator token and a darkroom are set and the server isn't read-only. `capabilities.teasers` is true when the moderator token is set and the server isn't read-only (an embodied teaser also needs darkroom).

`capabilities.author_catalogue` is true when the moderator token is set, the projects directory is writable, and the server isn't read-only.
