# Teaser projects

A teaser is an image with a quote on it, made to show an upcoming story before the story is out. Like a card, it is built only from reviewed parts, and its image is reviewed by the moderator and the owner before it goes anywhere. Posting it, minting it, both or neither come later and are optional.

A teaser is drawn in layers: a **background** from a scene, then the **identity** painted into it (or, for a viewpoint identity, its frame over the top), then the **quote**.

> Rendering backgrounds, painting identities in, training identities and posting teasers are on the way ([epic #47](https://github.com/smk762/argus-mint/issues/47)). This page covers the project's files, the checks on them and their review, which are here now.

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

`capabilities.author_catalogue` is true when the moderator token is set, the projects directory is writable, and the server isn't read-only.
