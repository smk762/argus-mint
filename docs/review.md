# Review: who approves what

Every card image is looked at by people before it is published. argus-mint keeps two kinds of review apart:

- **Catalogue review** decides which templates, frames and bio templates may make cards at all. See [catalogue.md](catalogue.md).
- **Image review** decides whether one finished card image may be published. This page covers it.

## Two reviewers, both required

A card image needs two approvals before it can be published:

| Role | Who | How they sign in |
|---|---|---|
| **moderator** | the platform's reviewer | the moderator bearer token (`ARGUS_MINT_MODERATOR_TOKEN`), plus a name or id in `by` |
| **owner** | the wallet the card will be minted to (the draft's `to`) | a Sign-In with Ethereum (EIP-4361) message signed by that wallet |

Neither role can act as the other. The moderator token can't sign as the owner, and the owner's signature can't stand in for the moderator.

**Why both.** The moderator answers for the platform: is this image fit to publish under its name? The owner answers for themselves: is this the card they want, with their name on it, minted to their wallet? A card goes on-chain only when both have said yes to exactly the same pixels.

## Each decision is bound to the image

A review records the role, the decision (`approved` or `rejected`), who decided, when, an optional note, and the **sha256 of the card image** it was about.

- Each role decides **once per image**. A second review by the same role on the same image is refused.
- A **rejection closes that image**. No more reviews and no publishing for it. To go on, render a new draft, or the owner edits the card (below), which makes a new image.
- If the image changes, earlier reviews **stop counting**. They stay in the draft's history, but a new image needs both approvals again.
- Publishing checks that the art and card image on disk still hash to what was reviewed, and refuses otherwise.

Reviews live in the draft (`draft.json`). They are never written into the card's public metadata.

## The owner's sign-in message

The owner never sends a password or token. Instead:

1. The UI asks the server for a challenge: `POST /drafts/{id}/challenge` with `{"action": "review", "approve": true}` (or `false` to reject).
2. The server returns an EIP-4361 message for the draft's `to` address on the service's domain (`ARGUS_MINT_SIWE_DOMAIN`). Its statement names the decision, for example *"Approve the card image of argus-mint draft 1a2b… for minting."*, and its resources name the draft (`urn:argus-mint:draft:<id>`) and the image (`urn:sha256:<image sha256>`).
3. The owner's wallet signs it (`personal_sign`).
4. The UI sends `POST /drafts/{id}/review` with `role: "owner"`, `approve`, the `message` and the `signature`.

The server checks all of it: the message is the one it issued (same draft, decision and image), the nonce is unused and under 10 minutes old, the domain and chain id match, and the signer is the draft's `to` address. Nonces live in the server's memory and work once, so a restart or a used nonce means asking for a new challenge.

The owner can review only while the draft is `pending` and still awaits the owner.

## Owner edits restart the review

Only the owner changes a card; the moderator approves or rejects. The owner may change the name, the bio, the display text and the frame's style ([customise.md](customise.md)). An edit:

- is signed like a review: a challenge with `{"action": "edit", "edit": {…}}`, whose resources also include the sha256 of the exact edit, so the signature covers those changes and nothing else;
- redraws the card on the same art, with the same frame (checked by hash);
- makes a new image, so **both approvals are needed again**, while the old reviews stay as history;
- is recorded in the draft's `edits`: who changed which fields, and the new image's sha256;
- works on a pending or rejected draft, never on a published one, and only on a framed card.

Text a person typed (a name, an edited bio, display text) never reaches a model.

## Auto-moderation

With `ARGUS_MINT_AUTO_MODERATE=1` (off by default) the platform's checks may stand in for the moderator. **The owner always reviews in person.**

After a draft is created, and again after each edit, the server runs the checks and records the result in the draft's `auto_check` (`passed`, `checks`, `held`):

| Check | Passes when |
|---|---|
| Art | darkroom rendered it (or it came from the library) and its final attempt passed darkroom's image guards outright. Supplied or placeholder art, or an image darkroom sent to `review`, is held. |
| Bio | the model wrote it and darkroom's text check passed it. A bio a person edited goes through the word filter instead. |
| Free text | the name, an edited bio, the description and each display text pass the word filter: better-profanity's list (which catches letter swaps) plus the terms in `ARGUS_MINT_BLOCKLIST`, one per line. |

If every check passes, a moderator approval is recorded with `by: "auto: platform checks"` and the checks as its note. If anything is held, the draft waits for a human moderator, and `auto_check.held` says why. Auto-moderation never overrides a decision the moderator already made on that image.

The checks judge safety, not quality. A missing figure or garbled lettering is the owner's to catch.

## Where to see it

- `GET /drafts/{id}` returns `status` and `awaiting` (the roles that haven't reviewed the current image yet). See [drafts.md](drafts.md).
- `GET /health` returns `capabilities.moderate`, `owner_review` and `auto_moderate`, so a UI knows which controls to enable.
- From the CLI: `argus-mint review DRAFT_DIR --as moderator|owner --by NAME --approve|--reject [--note …]` ([cli.md](cli.md)).
