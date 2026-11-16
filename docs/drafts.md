# Drafts: from render to publish

A card starts as a **draft**: local files that nobody outside the service has seen. It becomes public only when it is **published**, after both reviewers approve it ([review.md](review.md)).

```
draft  →  review (moderator + owner)  →  publish (pin, metadata, signed voucher)  →  mint (on-chain, by the owner's wallet)
```

## Making a draft

A draft is made from an **approved** template, with values picked from the template's fixed lists, plus optionally an approved frame and an approved bio template ([catalogue.md](catalogue.md)). The art comes from one of:

| Source | `renderer` | Notes |
|---|---|---|
| darkroom render | `darkroom` | the usual path: rendered on request through argus-darkroom's safety checks |
| library entry | `darkroom` | a portrait rendered earlier by `argus-mint library build`; no GPU needed. The draft records the entry in `library`. It's allowed only if the approved template renders exactly the request the entry was made from. |
| supplied image | `supplied` | CLI only (`draft --image`); auto-moderation always holds it |
| placeholder | `placeholder` | CLI dry runs only; can never be published |

Then, if a bio template was named, a language model writes the bio, and if a frame was named, the art is composited into it with the card's name, bio and display text.

Over HTTP: `POST /drafts` (moderator token). From the CLI: `argus-mint draft`. Both make the same record.

Each draft also records what made it, in `made_with`: `{"argus-mint": <version>}`, plus `"darkroom": <version>` when darkroom rendered the art for this draft (the version from darkroom's record of that render). Library and supplied drafts leave darkroom out. Drafts made before argus-mint recorded this have an empty `made_with`. A UI reads the "made with" line from here, not from `/health`, which reports the server running now.

## Draft status

`GET /drafts/{id}` and `GET /drafts` report a `status`, worked out from the reviews of the **current** image:

| Status | Meaning |
|---|---|
| `pending` | at least one reviewer hasn't decided on the current image, and nobody rejected it |
| `approved` | the moderator and the owner both approved the current image; ready to publish |
| `rejected` | a reviewer rejected the current image. It can't be reviewed or published; the owner may edit the card, which makes a new image and a new `pending` review |
| `published` | pinned and signed; final |

`awaiting` lists the roles that haven't reviewed the current image yet: `["moderator", "owner"]`, `["owner"]`, `["moderator"]` or `[]`. A UI uses it to show who is next. With auto-moderation on, a draft that passes the platform's checks starts with only `["owner"]` awaiting.

`GET /drafts?status=pending` filters by status, newest first, paged with `limit` (up to 200) and `offset`.

## What a draft records

`GET /drafts/{id}` returns `{id, status, awaiting, draft, published}`. The `draft` record holds:

| Field | What it is |
|---|---|
| `request` | the exact render request: template id, version and hash, prompt, negative prompt, checkpoint, LoRAs, sampling, size and the **final** seed |
| `attempts` | every render attempt, in order (below) |
| `renderer` | `darkroom`, `supplied` or `placeholder` |
| `library` | the library entry id, when the art came from one |
| `asset_id`, `to` | the consumer's 32-byte asset id, and the wallet the card is minted to (the owner) |
| `name`, `description`, `external_url`, `consumer` | metadata text. `description` defaults to the bio |
| `frame` | the frame's id, version and hash, when framed |
| `bio` | the bio template (id, version, hash, model), the text, every writing attempt, and `edited` if a person changed it |
| `texts` | the consumer's display text by frame slot (class, stats…) |
| `style` | the owner's styling of the frame ([customise.md](customise.md)) |
| `art_sha256`, `image_sha256` | the unframed render and the finished card image (equal when unframed) |
| `reviews`, `edits`, `auto_check` | the review history ([review.md](review.md)) |
| `created_at` | when the draft was made |

`GET /drafts/{id}/image` is the card image the reviewers approve. `GET /drafts/{id}/art` is the unframed render.

## Attempts

darkroom checks every render. When its safety check rejects one, argus-mint **retries with a new seed**, derived deterministically from the first, up to the template's `max_attempts`. The prompt never changes between attempts, and the check is never turned off. Each attempt is recorded:

| Field | |
|---|---|
| `seed` | the seed of this attempt |
| `outcome` | `ok`, `safety_rejected` or `error` (`supplied` for a supplied image) |
| `ref` | darkroom's record id for the attempt |
| `detail` | darkroom's reason, for a rejection or error |
| `workflow` | darkroom's workflow `{name, sha256}`, when it said |

A darkroom error (it refused the template text, or failed) stops at once. If every attempt is rejected, no draft is made, and `POST /drafts` answers 422 with the attempts.

A bio is written the same way: a reply that darkroom's text check flags, or that is unusable, is rewritten, up to the bio template's `max_attempts`, and every attempt is kept in `bio.attempts`.

## Publishing

`POST /drafts/{id}/publish` (moderator token), or `argus-mint publish`, takes an **approved** draft and:

1. checks the art and card image still hash to what was reviewed, and that both reviewers approved;
2. pins the unframed art (framed cards only) and the card image to IPFS;
3. builds the canonical ERC-721 metadata, whose `provenance` names the template, seed, attempt count, variables, checkpoint, LoRAs, frame, bio template and style;
4. signs an EIP-712 mint voucher for the owner's address, with the metadata's content hash, a price (default 0) and a deadline (default: now plus `ARGUS_MINT_VOUCHER_TTL`, 3600 s).

The result goes in `published` on the draft: the signer, the render summary, the reviews, the metadata and its CID, the voucher, the signature and the typed data. A draft publishes **once**. Placeholder art is refused.

Nothing reaches IPFS or a voucher before both approvals. Minting itself happens on-chain, sent by the owner's wallet (or `argus-mint submit`).
