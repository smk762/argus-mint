# The catalogue: templates, frames and bio templates

Cards are made only from reviewed parts. The catalogue holds three kinds, each a YAML file with an `id` and a `version`:

| Kind | Decides | Example |
|---|---|---|
| **template** | what the art may show: the prompt, its fixed variable lists, checkpoints, LoRAs, sampling | [templates/example-portrait-v3.yaml](https://github.com/smk762/argus-mint/blob/dev/templates/example-portrait-v3.yaml) |
| **frame** | the card's border: an overlay PNG, the art window, text slots and fonts | [frames/card-green-v1.yaml](https://github.com/smk762/argus-mint/blob/dev/frames/card-green-v1.yaml) |
| **bio template** | how a language model writes the card's flavour text | [bios/example-bio-v1.yaml](https://github.com/smk762/argus-mint/blob/dev/bios/example-bio-v1.yaml) |

A change is a new version: a new file. Cards already made keep pointing at the version, and hash, they were made with.

## Status and review

Each file has a `status`:

| Status | Meaning |
|---|---|
| `draft` | being written or reviewed. It can make **samples** for its reviewer (local files, never pinned or minted), not cards |
| `approved` | a person approved it. Only approved templates render cards, only approved frames go on them, only approved bio templates write their bios |
| `retired` | no longer used for new cards. Cards already made with it are unaffected |

Approval is a human step recorded in the file: `status: approved` plus `approved_by` and `approved_at`. Only two moves exist: `draft → approved` and `approved → retired`. Retiring keeps the approval lines.

**Hashes.** Each file has a hash that cards carry in their provenance. It covers the whole file **except `status`**, so retiring a file doesn't change the hash on cards already minted. `approved_by` and `approved_at` are included. A frame's hash also covers its overlay PNG and fonts.

**Reviewing through the API.** `POST /{templates,frames,bios}/{id}/{version}/review` (a project's own frame too, by its `<project>/<name>` id) with `{"decision": "approve" | "retire", "by": "<reviewer>"}` (moderator token) writes the decision into the YAML, keeping its comments and layout, re-validates it, and restores the original if it no longer loads. The served directories must be writable (`capabilities.review_catalogue`), and the change is a file edit to commit to the consumer's repository.

**Samples before approving.** `POST /samples` (or `argus-mint sample`) renders a few cards from a draft template, frame and bio template, so the reviewer sees what they're approving. Samples run as background jobs, go through the same safety checks and retries as cards, and are never pinned, signed or minted. `POST /frames/{id}/{version}/preview` shows any frame on a draft's or sample's existing art.

## Templates

A template is a prompt with `{placeholders}`, and for each placeholder a fixed list of allowed values. A render is the template id plus one value from each list. **There is no free-text prompt**: a value outside a list, a missing variable or an extra one is refused. `GET /templates` reports `combinations`, the number of prompts a reviewer is approving.

| Field | |
|---|---|
| `prompt`, `variables` | the prompt and each placeholder's allowed values |
| `negative` | the negative prompt (no placeholders) |
| `model` | the default checkpoint |
| `models` | other checkpoints a card may choose (reviewed with the template). Empty: only `model` |
| `width`, `height` | render size. Match it to the frame's art window: the art is scaled to cover the window and the overflow is cropped |
| `sampling` | sampler, scheduler, steps, cfg, clip_skip, and for Flux `guidance`. Fixed, so a render is reproducible |
| `loras` | LoRAs always applied, each `{name, weight, trigger_words}` |
| `lora_options`, `max_loras` | LoRAs a card may add, each with allowed `weights`, `trigger_words` and the checkpoints it pairs with; a card picks up to `max_loras` |
| `max_attempts` | how many seeds to try when darkroom's safety check rejects a render |

### Checkpoint families

A checkpoint's **family** is the top folder of its path in darkroom: `sdxl`, `flux`, `flux-klein`, `sd35`. Families differ in what they need, so a template can vary its settings:

- `sampling_by_family[family]` replaces `sampling` for that family.
- `sampling_by_model[checkpoint]` replaces both, for one checkpoint. Its key must be a checkpoint the template lists. Example: Flux schnell takes about 4 steps where Flux dev takes 20.
- `negative_by_family[family]` replaces `negative`. Flux's must be `""`: darkroom's Flux workflows have no slot for a negative prompt.

Fields that are empty or unset are left out of the template's hash, so templates written before they existed keep their hash.

### The darkroom check

darkroom refuses (400) a request its workflows can't take. Rather than find that at render time, argus-mint checks a template against darkroom's model list (`GET /models`):

- every checkpoint and LoRA exists, and the sampler and scheduler too;
- a workflow exists for each checkpoint with every LoRA count the template allows (its fixed LoRAs plus 0 to `max_loras` chosen ones);
- each workflow takes what the template sends: a negative prompt, `guidance`, `clip_skip` above 2;
- `guidance` is set where the workflow has a slot for it.

`GET /templates/{id}/{version}/check` returns `{ok, problems}`. With a darkroom configured, approving a template that has problems is refused (409, with the problems). `argus-mint preflight` runs the same check, then darkroom's text pre-flight on every variable combination.

## Frames

A frame is a PNG overlay, transparent inside its **art window**, described by a YAML file next to it.

| Field | |
|---|---|
| `overlay` | the PNG, relative to the YAML file |
| `window` | `{x, y, width, height}`: where the art goes. The art is scaled to **cover** the window, centred, and whatever doesn't fit is cropped |
| `texts` | named text slots |
| `style_locked` | `true` refuses owner restyling ([customise.md](customise.md)) |

**Text slots.** Each slot is a box (`x`, `y`, `width`, `height`) with a `font` (a TTF/OTF next to the frame; Pillow's built-in font if unset), `size` and `min_size`, `colour`, `align` (`left | center | right`), `valign` (`top | middle`), `wrap` and `line_spacing`. Text is drawn at `size` if it fits, else at the largest size down to `min_size` that does. If it still doesn't fit, it is clipped and ends in an ellipsis.

argus-mint fills the `name` slot with the card's name and the `bio` slot with the bio. Every other slot (`class`, `stats`…) takes the consumer's display text. That text is shown on the image only and is never a stat a game reads back.

card-green v1 has a landscape art window of about 4:3 (808 × 593), so templates for it render landscape (e.g. 1152 × 896).

### A project's own frames

A [teaser project](projects.md) may keep its own frames beside its other files, in `<project>/frames/` (a private project's then stay outside any public repository). Each is named by its project: **`<project>/<name>`**, such as `example-teasers/harbour-feed`. That id is written in the frame's YAML and is how the frame is referenced (`project.yaml`'s `frames`, a viewpoint identity's `frame`, the teaser links), served, reviewed and recorded in a draft's or token's provenance.

- **No collisions.** A global frame's id has no `/`, so a project's frame never shadows one, nor another project's. The id is part of the frame's hash, so the hash says whose frame it is.
- **Where the file lives decides.** A frame in `<project>/frames/` (beside that project's `project.yaml`) must be named `<project>/…`; a frame anywhere else must not have a `/` in its id. Only `<project>/frames/*.yaml` is read, not subdirectories, so its sources can sit in `frames/src/`.
- **Everything else is a frame's.** Same YAML, same review (`draft → approved → retired`, `approved_by`, `approved_at`), same hash (the YAML minus `status`, plus the overlay and fonts). `GET /frames` lists them with `project` set and `file` relative to the projects directory (`<project>/frames/<file>`); the frame routes take the two-part id, with the `/` as it is or encoded (`/frames/example-teasers/harbour-feed/1/overlay`, `/frames/example-teasers%2Fharbour-feed/1/review`).
- **Teasers only.** A project uses global frames and its own, never another project's (a problem in its checks), and a card draft refuses a project's frame (422).

### Building an overlay from HTML

`scripts/capture_frame.py` renders a local HTML page to a transparent PNG with Playwright's Chromium, and writes the frame's YAML from a small JSON spec (its window and text slots):

```sh
uv run --with playwright playwright install chromium-headless-shell   # once
uv run --with playwright python scripts/capture_frame.py <project>/frames/src/<name>/capture.json
```

```json
{
  "html": "overlay.html", "png": "../../<name>-v1.png", "yaml": "../../<name>-v1.yaml",
  "width": 1024, "height": 1024,
  "kit": {"name": "monohud", "commit": "<full sha>", "license": "MIT"},
  "frame": {"id": "<project>/<name>", "version": 1, "window": {"x": 0, "y": 0, "width": 1024, "height": 1024},
            "style_locked": true, "texts": {"quote": {"x": 72, "y": 700, "width": 880, "height": 220, "wrap": true}}}
}
```

- **No network.** The page is opened from `file://` and every other request is refused, so a page that loads a CDN stylesheet or web font fails to capture. Vendor what it needs (fonts too, with their licences).
- **What's recorded.** The YAML starts with comments: the command that rebuilds it, the sha256 of the HTML and of every local file the page loaded, the kit's name and commit (or `none`), and the Playwright and Chromium versions. Comments aren't hashed, so they change no hash; the overlay's bytes are what the hash covers. The status is always `draft`, and an approved or retired frame is never overwritten: capture a new version. The YAML is validated (`load_frame`) before anything is replaced.
- **Playwright isn't a dependency** of argus-mint, not even for its tests: they pass a stub renderer. Without it the script says how to run it.
- Chromium's text rendering can differ by version, so a rebuild is pixel-for-pixel only with the recorded versions. The committed PNG is what's reviewed and hashed, either way.

The example is [projects/example-teasers/frames](https://github.com/smk762/argus-mint/tree/dev/projects/example-teasers/frames): a harbour-camera look (timestamp, reticle, status, signal) in a few lines of its own CSS, captured to `harbour-feed-v1.png`, full bleed so the background shows through.

**Using a component kit (e.g. monohud).** [monohud](https://github.com/jshguo-labs/monohud) (MIT) has framework-free `packages/css` (plain CSS; `tokens.generated.css` holds its tokens) and `packages/fx` (TypeScript, so it needs a build before a page can load it). It is young, so pin a commit and review it. Vendor it into the frame's source directory, never into argus-mint:

1. `git clone https://github.com/jshguo-labs/monohud && git -C monohud checkout <sha>` and read what you're taking.
2. Copy `packages/css/src/*.css` and `packages/css/LICENSE` into `<project>/frames/src/<name>/vendor/monohud/` (and `packages/tokens/LICENSE` if you take its tokens), with the commit in a `COMMIT` file beside them. Check the CSS loads nothing remote (`@import url(http…)`, web fonts); the capture refuses it anyway.
3. Write `overlay.html` linking the vendored CSS and your own, with `background: transparent`. A static capture has no motion, so leave `fx`'s animations out; for signal distortion use the frame's reviewed effects.
4. Put `"kit": {"name": "monohud", "commit": "<sha>", "license": "MIT"}` in `capture.json`, capture, and look at the PNG over a background (`POST /frames/{id}/{version}/preview`) before approving it.

## Bio templates

A bio template tells a language model how to write a card's flavour text.

| Field | |
|---|---|
| `model` | the chat model (e.g. mistral-nemo) |
| `system`, `prompt` | the instructions. The prompt uses the render template's `{variables}` and a literal `{{name}}` token |
| `temperature`, `max_tokens`, `max_chars`, `max_attempts` | generation limits and retries |

The model sees only the variable values (from the fixed lists) and writes `{name}` where the name goes. **The card's real name never reaches the model**: argus-mint puts it in afterwards. darkroom's text check screens each reply with the token still in, and flagged or unusable replies are rewritten. The bio is drawn on the card, becomes its metadata `description`, and its provenance names the bio template and model. Bios aren't reproducible (there's no seed), so the text itself is kept in the draft.
