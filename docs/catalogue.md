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

**Reviewing through the API.** `POST /{templates,frames,bios}/{id}/{version}/review` with `{"decision": "approve" | "retire", "by": "<reviewer>"}` (moderator token) writes the decision into the YAML, keeping its comments and layout, re-validates it, and restores the original if it no longer loads. The served directories must be writable (`capabilities.review_catalogue`), and the change is a file edit to commit to the consumer's repository.

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

## Bio templates

A bio template tells a language model how to write a card's flavour text.

| Field | |
|---|---|
| `model` | the chat model (e.g. mistral-nemo) |
| `system`, `prompt` | the instructions. The prompt uses the render template's `{variables}` and a literal `{{name}}` token |
| `temperature`, `max_tokens`, `max_chars`, `max_attempts` | generation limits and retries |

The model sees only the variable values (from the fixed lists) and writes `{name}` where the name goes. **The card's real name never reaches the model**: argus-mint puts it in afterwards. darkroom's text check screens each reply with the token still in, and flagged or unusable replies are rewritten. The bio is drawn on the card, becomes its metadata `description`, and its provenance names the bio template and model. Bios aren't reproducible (there's no seed), so the text itself is kept in the draft.
