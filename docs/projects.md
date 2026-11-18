# Teaser projects

A teaser is an image with a quote on it, made to show an upcoming story before the story is out. Like a card, it is built only from reviewed parts, and its image is reviewed by the moderator and the owner before it goes anywhere. Posting it, minting it, both or neither come later and are optional.

A teaser is drawn in layers: a **background** from a scene, then the **identity** painted into it (or, for a viewpoint identity, its frame over the top), then the **quote**.

> Rendering backgrounds, painting identities in, training identities and posting teasers are on the way ([epic #47](https://github.com/smk762/argus-mint/issues/47)). This page covers the project's files, the checks on them and their review, which are here now.

## Files

One directory per project, under `ARGUS_MINT_PROJECTS_DIR` (default `projects`). A real project usually lives outside any repository. [projects/example-teasers](https://github.com/smk762/argus-mint/tree/dev/projects/example-teasers) is a neutral example.

| File | Holds |
|---|---|
| `project.yaml` | `title` (the working title), `attribution` (a fixed line under each quote, or `null` for none), `generated_label`, the `frames` teasers may use, the SingGuard `policy` (default `safe`), and the checkpoint `family` (default `sdxl`; painting a figure in is SDXL-only for now) |
| `identities/*.yaml` | Who a teaser speaks for. Its `voice`, and its `presence`: **`embodied`** (painted in from `prompt`, and kept consistent by a `lora` once a training run passes argus-proof, recorded as `proof_run`) or **`viewpoint`** (nobody painted in: the image is what they see, and their `frame`, such as a security-feed overlay, says so). It also lists `references`, each with its sha256 and provenance (`generated`, `licensed` or `personal`). A `personal` photo of a real person needs a `consent` record id |
| `scenes/*.yaml` | A setting: checkpoint, sampling, size, a fixed `prompt`, how many `backgrounds` to render for review, and the `character_area` where a figure may be painted in (left out when nobody is) |
| `lines/*.yaml` | One quote each. `text` is verbatim from its `source` and never edited. `published` is what goes on the image (typos fixed, nothing else), and defaults to `text`. No model writes or reads a line |
| `teasers.json` | The teasers by number (`T1`, `T2`…): for each, its `line`, `identity`, `scene` and `frame` (by id and version), its `alt` text, and the chosen `background` once there is one |

Every YAML file has an `id`, a `version` and a `status`, and is reviewed like a template: `draft`, then `approved` (with `approved_by` and `approved_at`), then `retired`. A change is a new version. Only approved files make teasers. `teasers.json` isn't reviewed itself: it only links reviewed files, and every image it leads to is reviewed as a draft.

A scene or identity prompt is fixed text written by the author. A teaser never takes a prompt.

## Checks

Each save (and `GET /projects/{id}`) reports:

- **Problems**: schema errors, and what one file says about another: a teaser naming a file or frame that doesn't exist, or a frame that isn't one of the project's; a line whose voice isn't the identity's; an embodied identity in a scene with no `character_area`; a viewpoint teaser not using its identity's frame; a scene outside the project's checkpoint family; a file belonging to another project.
- **Spoilers**: rule numbers from the private spoiler list that match any text in the file, negatives and sources included.
- **SingGuard**: darkroom's text pre-flight, under the project's `policy`, on the title and attribution, each identity and scene prompt, each line's text and published text, and each teaser's alt text. Negative prompts are left out, since they name what to avoid.

### The spoiler list

A text file outside every repository, at `ARGUS_MINT_SPOILERS` (keep it `chmod 600`). One rule per line, with `#` comments. A plain line matches as a whole phrase, ignoring case and spacing. A `re:` line is a regular expression, also ignoring case. A match is reported only by the rule's number, counting from 1, so no draft, response, log or fixture repeats the spoiler. The server refuses to start when the file is set but can't be read.

## Review

Approving a project file is refused (409) while a spoiler rule matches it, or while SingGuard hasn't passed its text. With no darkroom configured there is no SingGuard, so nothing is approved. An approved or retired file never changes: the author saves a new version.

## HTTP

| Endpoint | Auth | |
|---|---|---|
| `GET /projects` | | `{projects: [{id, title, status}]}` |
| `GET /projects/{id}` | | `{id, files, teasers, checks}`. Each file is `{kind, id, version, status, hash, file, doc}`. `checks.guard` holds the verdicts already made |
| `POST /projects/{id}/files/{kind}/{item}/{version}` | moderator | `{doc}` writes a draft `project`, `identity`, `scene` or `line`. Leave out `status`, `approved_by` and `approved_at`. Returns `{file, checks}`. A new project starts with `files/project/{id}/1` |
| `POST /projects/{id}/teasers` | moderator | `{teasers}` replaces `teasers.json`. Returns `{file: null, checks}` |
| `POST /projects/{id}/files/{kind}/{item}/{version}/review` | moderator | `{decision: "approve" \| "retire", by}` |

`capabilities.author_catalogue` is true when the moderator token is set, the projects directory is writable, and the server isn't read-only.
