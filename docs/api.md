# HTTP API

`argus-mint serve` puts the card workflow behind HTTP for the suite's UIs (argus-studio's pages and argus-assay). It listens on `127.0.0.1:8108` by default. The server decides; a UI only shows what it says.

The request and response shapes are pinned in [schema/mint-wire.schema.json](https://github.com/smk762/argus-mint/blob/dev/schema/mint-wire.schema.json) (`argus-mint schema`). `GET /health` reports its `wire_version`; additive changes bump the minor, breaking ones the major. Recorded example responses live in [fixtures/api/](https://github.com/smk762/argus-mint/tree/dev/fixtures/api).

## Auth

| Who | How |
|---|---|
| anyone | reads, the frame layout and preview, owner challenges |
| **moderator** | `Authorization: Bearer <ARGUS_MINT_MODERATOR_TOKEN>`. 403 if the server has no token set, 401 if it's wrong |
| **owner** | a signed EIP-4361 challenge from the wallet the card is minted to (`message` + `signature` in the body). See [review.md](review.md) |

Writes from a browser origin the server wasn't told about (`ARGUS_MINT_CORS_ORIGINS`) are refused, and with `ARGUS_MINT_READ_ONLY` every write is. Server-to-server calls send no `Origin` and are allowed.

## Errors

Errors are JSON `{"detail": …}`. `detail` is usually a sentence; a failed render or bio is an object with the attempts.

| Code | Meaning |
|---|---|
| 400 | malformed id |
| 401 / 403 | missing or wrong token; bad, reused or expired challenge; signer isn't the owner |
| 404 | no such draft, template, frame, bio, sample or library entry |
| 409 | the state doesn't allow it: already reviewed, rejected, published, not approved, nothing changed, darkroom can't render the template |
| 422 | the request is invalid: a value outside a template's list, an unknown slot, a past deadline, every render attempt rejected |
| 502 | a backend (darkroom, the bio writer, IPFS) failed |
| 503 | the server isn't configured for this (no darkroom, writer, SIWE domain, signer, IPFS or Card) |

## Health

| Endpoint | Returns |
|---|---|
| `GET /health` | `version`, `wire_version`, `read_only`, `network`, `chain_id`, `card`, `consumer`, `reviewers`, `siwe_domain`, `capabilities`, and `backends` (does darkroom, the writer and IPFS answer) |

`capabilities` tells a UI which controls to enable:

| Capability | True when |
|---|---|
| `create_drafts` | moderator token set, not read-only, and darkroom configured or a library served |
| `write_bios` | as above, with darkroom and a bio writer |
| `moderate` | moderator token set, not read-only |
| `owner_review`, `edit_text` | SIWE domain set, not read-only |
| `publish` | moderator token, IPFS, a signer and the Card address set, not read-only |
| `sample` | moderator token and darkroom, not read-only |
| `review_catalogue` | moderator token, catalogue directories writable, not read-only |
| `library` | a pre-rendered library is served |
| `check_templates` | a darkroom is configured to check templates against |
| `auto_moderate` | auto-moderation is on |
| `author_catalogue` | moderator token set, projects directory writable, not read-only |

## Catalogue

See [catalogue.md](catalogue.md).

| Endpoint | Auth | |
|---|---|---|
| `GET /templates` | | every template file, any status: `{hash, file, combinations, template}` |
| `GET /templates/{id}/{version}/check` | | `{template, ok, problems}`: what would make darkroom refuse it. 503 without a darkroom |
| `GET /frames` | | every frame: `{hash, file, width, height, frame}` |
| `GET /frames/{id}/{version}/overlay` | | the overlay PNG |
| `GET /bios` | | every bio template: `{hash, file, bio}` |
| `POST /templates/{id}/{version}/review` | moderator | `{decision: "approve" \| "retire", by}`; writes it into the YAML. A template darkroom can't render isn't approved (409) |
| `POST /frames/{id}/{version}/review` | moderator | the same, for a frame |
| `POST /bios/{id}/{version}/review` | moderator | the same, for a bio template |

## Teaser projects

See [projects.md](projects.md): `GET /projects`, `GET /projects/{id}`, `POST /projects/{id}/files/{kind}/{item}/{version}` (save a draft, teaser links included), and `POST /projects/{id}/files/{kind}/{item}/{version}/review`. Writes need the moderator token.

## Library

| Endpoint | Auth | |
|---|---|---|
| `GET /library` | | `{template, models, count, items}`; each item `{id, group, model, variables, seed, sha256}`. 404 if none is served |
| `GET /library/{id}/image` | | the entry's PNG; `?thumb=<width>` (up to 1024) for a cached JPEG preview. Thumbnails never go on cards |

## Drafts

See [drafts.md](drafts.md) and [review.md](review.md).

| Endpoint | Auth | |
|---|---|---|
| `GET /drafts` | | `{items, total}`, newest first. `?status=pending\|approved\|rejected\|published`, `?limit=` (1–200, default 50), `?offset=` |
| `GET /drafts/{id}` | | `{id, status, awaiting, draft, published}` |
| `GET /drafts/{id}/image` | | the card image the reviewers approve (PNG) |
| `GET /drafts/{id}/art` | | the unframed render (PNG) |
| `POST /drafts` | moderator | make a draft (201) |
| `POST /drafts/{id}/challenge` | | the sign-in message for the owner's wallet |
| `POST /drafts/{id}/review` | moderator or owner | record a decision |
| `POST /drafts/{id}/edit` | owner | change text or style; the card is redrawn and reviewed again |
| `POST /drafts/{id}/publish` | moderator | pin and sign an approved draft |

### `POST /drafts`

```json
{
  "template": {"id": "example-portrait", "version": 1},
  "variables": {"role": "ranger", "colour": "none", "pose": "standing"},
  "seed": 81234,
  "model": null,
  "loras": [],
  "asset": "unit-17",
  "to": "0x…",
  "name": "Hana of the Strait",
  "frame": {"id": "card-green", "version": 1},
  "bio": {"id": "example-bio", "version": 1},
  "texts": {"class": "Ranger of the Strait"}
}
```

- Give exactly one of `asset` (the asset id is keccak256 of its UTF-8) or `asset_id` (`0x` + 64 hex).
- Instead of `variables`/`seed`/`model`, give `library: "<entry id>"` to use a pre-rendered portrait; its variables, seed and checkpoint apply.
- Optional: `description` (default: the bio), `external_url`, `style`.
- The template, frame and bio template are named by id and version, never by path, and must be approved.

Rendering can take a while: the request returns when the draft is made. If every attempt is rejected the answer is 422 with `{error, attempts}`; a bio that can't be written is 422 with `{error, bio_attempts}`.

### `POST /drafts/{id}/challenge`

`{"action": "review", "approve": true}` for a review, or `{"action": "edit", "edit": {name?, bio?, texts?, style?}}` for an edit. Returns `{message, nonce, expires_at}`. The message is valid for 10 minutes and works once. A review challenge needs the draft `pending` and awaiting the owner; an edit challenge needs it unpublished.

### `POST /drafts/{id}/review`

Moderator: `{"role": "moderator", "approve": true, "by": "mod-1", "note": ""}` with the bearer token. Owner: `{"role": "owner", "approve": true, "message": "…", "signature": "0x…"}`; `by` is taken from the signature. Returns the draft.

### `POST /drafts/{id}/edit`

`{name?, bio?, texts?, style?, message, signature}`: the same fields the edit challenge was issued for. Works on a pending or rejected framed draft, not a published one. Returns the draft with its new image. See [customise.md](customise.md).

### `POST /drafts/{id}/publish`

`{"price": 0, "deadline": null}`: price in wei; deadline in unix seconds (default now + `ARGUS_MINT_VOUCHER_TTL`). Returns the draft with `published`: `{signer, render, reviews, metadata, metadata_cid, voucher, signature, typed_data}`. A draft publishes once.

## Samples and frame previews

| Endpoint | Auth | |
|---|---|---|
| `POST /samples` | moderator | a sample job (202). Body: `template`, optional `frame`, `bio`, `model`, `loras`, `count` (1–12, default 4), `variables` (fixed values; the rest vary), `seed`, `name`, `texts`, `style`. Any status |
| `GET /samples` | | jobs, newest first, `{items, total}`, paged with `limit`/`offset` |
| `GET /samples/{id}` | | `{id, status: queued\|running\|done\|failed, items, …}`; each item says whether `art` and `framed` exist |
| `GET /samples/{id}/{n}/art`, `/framed` | | sample *n*'s images (PNG) |
| `POST /frames/{id}/{version}/preview` | | a frame (any status) on existing art: `{"art": {"draft": "<id>"}}` or `{"art": {"sample": "<id>", "n": 0}}`, plus optional `name`, `bio`, `texts`, `style`. Returns a PNG |
| `POST /frames/{id}/{version}/layout` | | `{"texts": {…}}` → where each line goes, for a browser preview |
| `GET /frames/{id}/{version}/fonts/<path>` | | a font the frame names |

Samples run one at a time on a background thread and are never pinned, signed or minted. A job left queued or running by a restart reads as `failed`.

## Configuration

The server is configured by `ARGUS_MINT_*` environment variables; see the README's *Configuration* and [.env.example](https://github.com/smk762/argus-mint/blob/dev/.env.example). From the command line: `argus-mint serve --host 127.0.0.1 --port 8108 --cors <origins>` ([cli.md](cli.md)).
