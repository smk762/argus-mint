# CLAUDE.md — argus-mint

Guidance for AI agents working in this repo. Human-facing usage lives in [README.md](README.md); this file is the orientation an agent needs to change code safely.

## What this is

Provenance tokens for game assets. A Python package (and an HTTP service, `argus-mint serve`) plus Foundry contracts that render art from **reviewed templates** through darklight, pin it to IPFS, build canonical ERC-721 metadata, mint transferable **Card**s and soulbound **Deed**s, and anchor commit/reveal secrets and Merkle roots in a **Ledger** contract so anyone can verify a consumer's records. First consumer: atlas-realms (a browser strategy game), which writes its own adapter.

**argus-mint has no game in it.** It never knows about units, battles, hexes or coins. A consumer supplies a template's variables, a record to show, and 32-byte leaf hashes. Out of scope: game rules/stats, marketplaces, fungible tokens, staking or yield, free-text prompts, a frontend (only a JS verifier module ships; the UIs are argus-studio's pages and argus-assay, clients of `argus-mint serve`).

## Argus suite
<!-- argus-suite: keep this block; argus-ci checks for it -->
This repo is part of the Argus suite. The suite map below lists every sibling and what it owns; check it before
building something a sibling may already do. **On every release, update this repo's row and capabilities in
argus-cortex's SUITE.md** (a PR to argus-cortex in the same release).

@../argus-core/SUITE.md

Not checked out? Read https://github.com/smk762/argus-cortex/blob/main/SUITE.md

## Hard rules (a change that breaks one is a bug)

1. **No raw prompts.** A render is a template id + variables, each picked from that template's fixed list. Free text (the card name, consumer display text) may appear in metadata and on the card image, never in a model request, image or text. `templates.fill` rejects values outside the list, missing or extra keys. The bio writer sees only the variables and a literal `{name}` token; the name goes in afterwards (`bios.write_bio`).
2. **Only reviewed templates, frames and bio templates make cards.** Status `draft | approved | retired`; only `approved` templates render cards, only `approved` frames go on them, and only `approved` bio templates write their bios. Approval is a human step recorded in the file (`approved_by`, `approved_at`). `argus-mint sample` / `render.render_for_review` render drafts for their reviewer, to local files only: never pinned, signed or minted.
3. **Retry, don't loosen.** A safety rejection is retried with a new deterministic seed up to `max_attempts`. Never change the prompt or disable the check. Every attempt's seed + outcome is recorded.
4. **Tokens carry no power.** Metadata describes a record; it never holds a number a game reads back as a stat. Card text (class, stats) is display text the consumer supplies, drawn on the image only; the bio writer is told to write no numbers or stats. Contracts have no hooks a game calls to decide outcomes.
5. **Nothing pays out.** No fungible token, revenue share or staking. Royalties, if any, are standard ERC-2981 on resale.
6. **Consumers' deterministic code never reads the chain.** The indexer copies chain state into the consumer's DB; game logic reads that copy.
7. **Reproducible.** Same template version + variables + seed → same render request. Metadata JSON is canonical (sorted keys, compact separators, UTF-8) so its hash is stable.
8. **Pure core.** `templates`, `metadata`, `merkle`, `vouchers`, `siwe` are pure (no I/O beyond reading a template file) and tested without a network. Network code (darklight, IPFS, RPC) lives in thin, injectable clients behind `Protocol`s.
9. **Every card image is reviewed before it is published.** (With `ARGUS_MINT_AUTO_MODERATE=1` the platform's checks may stand in for the moderator; the owner always reviews.) `draft` (render + frame, local files) → `review` (the moderator **and** the owner each approve or reject the exact image, bound to its sha256) → `publish` (pin, metadata, signed voucher). `cards.check_publishable` refuses anything else; a rejection closes the draft. Nothing reaches IPFS or a voucher before both approvals.

## Layout

```
src/argus_mint/   templates metadata merkle vouchers siwe     # pure core (_codec: keccak, canonical JSON, field types)
                  library                                  # pre-rendered portraits (library build; drafts from entries without a GPU)
                  frames cards bios                        # frame overlays + text slots (Pillow, `frames` extra); card drafts + image review; LLM bios
                  render pin indexer                       # clients behind Protocols (no web3 needed)
                  chain ledger                             # web3 clients (`chain` extra)
                  keys                                     # signers: Foundry keystore (password file/prompt) or $ARGUS_MINT_SIGNER_KEY
                  workflow                                 # create_draft / publish, shared by the CLI and the server
                  server wire                              # `argus-mint serve` (FastAPI, `server` extra) and its wire models
                  cli                                      # Typer: templates, frames, preflight, sample, draft, review, publish, submit, freeze, anchor, serve, schema
                  abi/   # committed contract ABIs (`make abi`, abi.load(name)), so pip users never need Foundry
contracts/        Foundry: src/{Card,Deed,Ledger}.sol, test/, script/Deploy.s.sol, lib/ (submodules)
verifier/         ES module: Merkle proofs, Ledger reads, drand quicknet signature checks
templates/        example template YAML (example-portrait v1: approved; v2: draft, clean full-bleed art for framing, seven SDXL checkpoints + Flux dev; v3: draft, v2 reworded for clothing, described roles and no lettering, plus Flux schnell, FLUX.2 Klein 9B and SD3.5 Large)
bios/             example bio template (draft; mistral-nemo, fruity flavour text)
frames/           card-green v1 (the card frame, draft; ~4:3 window) and example-plain v1 (placeholder, square window, drawn by scripts/make_example_frame.py)
schema/           mint-wire.schema.json: the HTTP wire contract (`argus-mint schema`; a test fails if stale)
fixtures/api/     recorded API responses for the UIs' demo mode, incl. a library subset (scripts/api_fixture.py; needs ./library)
scripts/          export_abi.py; fixtures/ (JS generator for @openzeppelin/merkle-tree fixtures)
tests/fixtures/   committed cross-implementation fixtures (Merkle, vouchers)
```

Extras: base install = pure Python (no web3); `chain` = web3 for the indexer and ledger client; `cli` = Typer; `frames` = Pillow for compositing; `server` = FastAPI + uvicorn + argus-cortex (implies cli, frames); `dev` = tests.

## Settled decisions (beyond the spec)

- The ERC-721 contract is **`Card`** (not `UnitCard`): no game words in argus-mint. EIP-712 domain name `"argus-mint Card"`, version `"1"`. Token name/symbol are constructor args.
- The voucher's **`contentHash` = keccak256(canonical({"image": …, "provenance": …}))** — only the immutable part of the metadata, so it stays checkable after live attribute refreshes. `metadata.content_hash()` is the one place this is computed. Provenance records the render's choices (`variables`, `model`, `loras`; `Provenance.for_render(req, …)` is the one builder) so the request can be rebuilt from public metadata; unset fields are left out, so cards minted without them (before 0.2.1) hash as before.
- Template hash = keccak256 of the canonical JSON of the template **minus `status`**, so retiring a template doesn't change the hash cards already carry. `approved_by`/`approved_at` are included.
- Ledger commitments are **sha256** (atlas-realms already publishes `sha256(secret)`); Merkle leaves are OpenZeppelin `StandardMerkleTree` `["bytes32"]` (double keccak).
- **Target chain: Base** (OP Stack; mainnet 8453, testnet Base Sepolia 84532), chosen 2026-11-08 for consumer onboarding and ecosystem exposure. `networks.py` holds the constants; `foundry.toml` has `base` / `base_sepolia` RPC and Basescan entries; `make deploy [NETWORK=base]` deploys and verifies (settings in `.env`, deployer from a Foundry keystore, never a raw key in a file).
- Indexing on Base: follow `Web3Rpc(w3, head="safe")` with `confirmations=0` rather than counting blocks on the sequencer's unsafe head (`safe` trails `latest` by about 2 minutes on mainnet.base.org).
- **No explorer API is needed to run anything**: deploy, mint, reads and indexing are plain JSON-RPC. `networks.*.public_rpcs` lists keyless endpoints (checked 2026-11-08) and `chain.connect()` fails over between them, skipping any on the wrong chain id. Public endpoints cap `eth_getLogs` ranges, so use `max_blocks≈500` on them. Contract verification defaults to Sourcify (free); `VERIFIER=blockscout|etherscan` are the alternatives, and only etherscan needs a key.
- Contracts are not upgradeable; a new version is a new deployment.
- **OpenZeppelin is pinned to v5.4.0 and `evm_version = "cancun"`** on purpose: OZ ≥ 5.5 emits the `clz` opcode, which needs Osaka. Base's Osaka support was unconfirmed when chosen; bump both together only after verifying it (e.g. deploy a CLZ probe to Base Sepolia).
- `Deed`: each `recordId` mints once, ever (a burned deed's record stays used), so a retried mint can't duplicate. The consumer makes per-player records with e.g. `keccak256(feat, player)`.
- `Ledger.reveal` works once per epoch and is open to anyone; `Card.refresh`/`freeze` revert after freeze; `setBaseURI` emits `BatchMetadataUpdate` over all minted ids.

## Client behaviour worth knowing

- **darklight = argus-darkroom** (guarded generation, `:8106`; argus-safelight is its UI). `darkroom.DarkroomClient` implements the `Darklight` protocol over `POST /generate/stream` (SSE) and `GET /image/{id}`. It never sets `expand` (Ollama would replace the template prompt), always names the enforcing policy (default `default`, the SFW one; the server's own default is NSFW), and checks the returned record: `final_prompt` must equal `darkroom.final_prompt(req)` (prompt + trigger words, darkroom's `assemble_prompt`), the seed must match, and the image bytes must hash to `record.image.sha256`. Outcomes: `passed` → ok; `review` → safety_rejected unless `accept_review`; `deleted`/`omitted` → safety_rejected (retried); `refused` → error (pre-flight judged the template text, so another seed can't help: the template needs review); `failed` → error. darkroom serves one generation at a time; a 409 is waited out (`busy_wait`, `busy_timeout`). Server-to-server calls send no `Origin`, which darkroom's write guard allows.
- Templates carry a `sampling` block (sampler, scheduler, steps, cfg, clip_skip) and LoRAs as `{name, weight, trigger_words}` (a bare string still works); darkroom requires sampling, pre-flight doesn't. `fill_for_review` + `combinations` let `argus-mint preflight` check every combination of a draft before approval; rendering still goes through `fill`, which refuses non-approved templates.

- `render.render` retries **only** safety rejections, with `derive_seed(seed, n)` (attempt 0 = the given seed). A darklight `error` stops at once with the attempt log; transport retries belong inside the client. The prompt is identical across attempts.
- `indexer.follow` is one stateless pass over blocks `[cursor.next_block, head - confirmations]` (paged by `max_blocks`). The returned `Cursor` carries the hash of the last block read; if that block is later replaced the next pass raises `ReorgTooDeep` rather than continuing on a stale view.
- `pin.LocalPinner`/`raw_cid` produce real CIDv1 raw ids, equal to IPFS's only for single-chunk (≤ 256 KiB) files; treat them as local ids.
- The CLI logs to stderr; every command's result is JSON on stdout (password prompts go to the tty via `getpass`). `draft` needs `--darkroom`, `--image` or `--placeholder`; a live `publish` needs `--ipfs-api` and a signer, and refuses placeholder art.
- Frames (`frames.py`): a YAML file + PNG overlay (transparent inside `window`) + optional TTF; the render is scaled to cover the window, the overlay goes on top, the card name is drawn in the `name` slot (shrinking to fit). A framed card's provenance gains `frame` {id, version, hash} and `art` (ipfs:// of the unframed render); both are left out when unset, so unframed cards hash exactly as before. Frame hash = keccak256(canonical(frame minus `status`, overlay sha256, font sha256)). Compositing is deterministic for a given Pillow version.
- Frame text slots (`texts:` in the frame YAML): each has a box, font, size/min_size, colour, align/valign, `wrap`. Text shrinks to fit, then is clipped with an ellipsis at `min_size` (`frames.fit_text`). argus-mint fills `name` and `bio`; other slots (class, stats...) come from `draft --text slot=value`. card-green ships Cinzel and Crimson Text (OFL) in `frames/fonts/`.
- Bios (`bios.py`): a bio template (YAML: model, system, prompt with the render template's `{variables}` and a `{{name}}` token, temperature, max_chars, max_attempts) is filled and sent to a `Writer` (`ChatWriter`: OpenAI-style `POST /chat`, e.g. the suite's chat API at `$ARGUS_MINT_WRITER_URL` with mistral-nemo; bearer from `$ARGUS_MINT_WRITER_KEY`). The reply is tidied, any `{Name}` variant normalised, then screened by a `TextGuard` (`DarkroomClient.check_text`, darkroom's `/preflight`) *with the token still in*; flagged or unusable replies are retried, every attempt recorded. Then the name goes in. Bios aren't reproducible (no seed): the text is kept in the draft, drawn on the card and used as the metadata `description`; provenance gets `bio` {id, version, hash, model}.
- LoRAs and families: `Template.lora_options` (reviewed: name, allowed weights, trigger words, compatible models) and `max_loras`; a render's `LoraChoice`s become `RenderRequest.loras` after the template's fixed `loras` (`Template.resolve_loras`). `sampling_by_model[model]` (keys must be allowed checkpoints; e.g. Flux schnell's ~4 steps) overrides `sampling_by_family[family(model)]`, which overrides `sampling` and `negative_by_family` overrides `negative` (`family` = top-level folder of the normalised checkpoint path, as darkroom reads it). `Sampling.guidance` is for Flux, and `darkroom.request_body` drops it when unset. Flux's negative must be `""`: darkroom's Flux workflows have no `$negative` slot, and it refuses (400) a negative, `guidance` or `clip_skip > 2` that the chosen workflow can't take. Like `models`, these are left out of the hash while unused, and so is an unset `guidance`. darkroom ≥ 0.1.3 picks a workflow per family and LoRA count from a workflow directory. `darkroom.check_template(t, models)` (pure, over `GET /models`) finds what it would refuse: missing checkpoint/LoRA/sampler, a LoRA count with no workflow (`lora_counts(t, model)` = fixed + 0..max_loras chosen), a negative/guidance/clip_skip the workflow can't take, or guidance missing where it has the slot. `preflight` runs it before the text check; the server serves it at `GET /templates/{id}/{v}/check` and refuses (409) approving a template with problems when a darkroom is configured. `Attempt.workflow` keeps darkroom's `{name, sha256}`; `Attempt.to_json()` is the one serialisation.
- Checkpoints: `Template.models` is a reviewed list of alternatives to `model` (the default). `fill`/`render` take `model=` and refuse others. The hash leaves `models` out when it is empty, so older templates (v1, in minted cards) keep their hash.
- Library (`library.py`): `build` renders `pick_variable_sets` (every value of every variable before any repeats; deterministic) on each model, one model at a time, resumable, into `manifest.json` + `images/`. Group *n* uses seed `seed*1000+n` on every model. `check_entry` allows an entry for a draft only if `fill(approved template, entry vars, entry final seed, entry model)` equals the entry's request, template hash aside (approving a draft template changes the hash). Such drafts have `renderer: darkroom`, the entry's attempts, and `library: <entry id>`. The server reloads the manifest when it changes, and `create_drafts` no longer needs darkroom when the library has entries. Thumbnails are cached JPEGs and are never used for cards.
- Drafts (`cards.py`): a directory with `draft.json`, `art.png` and, when framed, `card.png`. Reviews stay in `draft.json` (not in public metadata). The template's `width`/`height` go to darkroom inside `sampling` (its workflow's `$width`/`$height`), so match the render's aspect to the frame's art window: the compositor crops whatever doesn't fit (cover).
- Signers (`keys.load_signer`): `--account` / `$ARGUS_MINT_ACCOUNT` names a Foundry keystore (`~/.foundry/keystores/<name>` or a path), password from `$ARGUS_MINT_KEYSTORE_PASSWORD_FILE` or a hidden prompt; else `$ARGUS_MINT_SIGNER_KEY`. Never a key on the command line.
- `submit` / `freeze` / `anchor` pre-check what the contract would refuse (role, already minted/frozen/anchored, expiry, chain id) before asking for a password or spending gas; a revert that still happens is decoded to the custom error's name by `chain.explain_revert`. `submit` refuses `dry_run` outputs. `anchor --tree` writes the standard-v1 dump *before* sending, so a landed root always has its tree.
- Pinning: `compose.yaml` runs a Kubo node (`make ipfs`); its RPC (5001) is admin-level and stays on 127.0.0.1, swarm 4001 is public. A Kubo `add` of a file > 256 KiB returns a dag-pb CID (`bafybei…`), not a raw one; that's expected.
- darkroom LoRAs need a workflow with `$lora`/`$lora_weight` slots (`sdxl+lora1.json`...); darkroom < 0.1.3, or one given a single workflow file, refuses (400) a request with LoRAs. Model names come from darkroom's `GET /models`.

- **The service** (`server.py`, `argus-mint serve`, port 8108; the UI argus-assay is on 3108). Settings are `ARGUS_MINT_*` (`server.Settings.from_env`). Auth: the moderator bearer token (`MODERATOR_TOKEN`) creates drafts, reviews as moderator and publishes. The owner reviews by SIWE: `POST /drafts/{id}/challenge` issues an EIP-4361 message for the draft's `to` address on `SIWE_DOMAIN`, whose statement names the decision and whose resources name the draft and the image sha256. The nonce lives in memory, is single use and expires in 10 minutes. The review checks all of it and that the signer is `to`. Writes go through cortex's `WriteGuard` (`cross_site_refuse`, plus `constant_refuse` when `READ_ONLY`). `/health.capabilities` tells a UI what to enable. Drafts live in `DraftStore` (`DRAFTS_DIR/<16-hex id>/`, ids validated before any path is built); a published draft also has `publish.json`. One lock serialises writes. Rendering and publishing run in the threadpool. Owner edits: `POST /drafts/{id}/edit` (`workflow.edit_text`), **owner only**; the moderator just approves or rejects. It changes the text and/or `style` (`metadata.CardStyle`: a tint plus per-slot colour, effect and background; any colours) and redraws a framed draft on the same art with the exact frame it was made with (by hash). `frames.tint_matrix` is the single definition of the tint, a luma-based colourise matrix: Pillow applies it with `convert('RGB', matrix)`, and the browser uses the same numbers in an sRGB `feColorMatrix`. `frames.layout` gives the browser the exact line positions `compose` uses, and `/frames/{id}/{v}/fonts/<path>` serves the fonts. The style goes into provenance; a frame with `style_locked` refuses styles. The new image needs both approvals again; old reviews stay as history, and `edits` records who changed which fields. The owner edits by signing an `action: edit` challenge whose resources include the sha256 of the canonical edit, so the signature covers exactly those changes. Auto-moderation (`moderation.py`, `AUTO_MODERATE`): after create and after each edit, `auto_review` records `auto_check` and, if every check passed, a moderator approval by `auto: platform checks`. Free text (name, an edited bio, description, display text) never reaches a model; it goes through `WordFilter` (better-profanity plus `BLOCKLIST`). Catalogue review: `POST /{templates,frames,bios}/{id}/{v}/review` calls `catalogue.record_decision`. It edits the YAML as text (draft → approved writes `approved_by`/`approved_at`; approved → retired keeps them, so the hash cards carry is unchanged), re-validates, and restores the original on failure. The served directories must be writable for `review_catalogue`, and approving through the API leaves a file change to commit. Samples (`POST /samples`) are jobs on one background thread, stored in `SAMPLES_DIR/<id>/` (`job.json`, `<n>-art.png`, `<n>-framed.png`) and never pinned. A job left queued or running by a restart reads as failed. `POST /frames/{id}/{v}/preview` composites any-status frames on a draft's or sample's art. Wire 1.1 added these; additive, so the major is still 1.

## Commands

```bash
make install      # git submodules + uv sync --locked --all-extras
make test         # pytest (no network: clients are stubbed)
make lint         # ruff 0.15.16 check + format --check, forge fmt --check
make forge-build  # / forge-test
make abi          # forge build, then copy ABIs into src/argus_mint/abi (commit them)
make fixtures     # regenerate OZ merkle-tree fixtures (Node) + Python-built forge fixtures
make fixtures-rpc # record Ledger JSON-RPC responses from anvil for the verifier tests
make verifier-install verifier-test   # JS verifier (node --test)
make check        # lint test forge-test verifier-test — every milestone ends green on this
make ipfs         # start the local Kubo node (docker compose)
make deploy       # Base Sepolia (NETWORK=base for mainnet); needs .env from .env.example
```

Foundry lives in `~/.foundry/bin` (the Makefile finds it there); Node in `~/.local/bin`.

## Conventions & gotchas

- **Versioning is git-tag-derived** (`hatch-vcs`); `_version.py` is generated. Each milestone ends with a pre-release tag (`v0.0.1a1`, `v0.1.0a1`, …) and a CHANGELOG heading. Headings are Keep a Changelog style (`## [Unreleased]`, `## [0.2.1] - 2026-11-14`): the release workflow takes the tag's section as the GitHub Release notes and fails before publishing anything if the tag has no section (pre-releases included). The Release is created last, once PyPI has the version, with the wheel and sdist attached.
- **ABIs are committed and checked in CI** — after a contract change run `make abi` and commit the result in the same commit.
- **Fixtures:** `tests/fixtures/merkle.json` comes from the reference JS library (`@openzeppelin/merkle-tree`) and must never be regenerated to make a failing test pass. `vouchers.json` and `proofs.json` are Python-built (`scripts/py_fixtures.py`) for the forge tests; `tests/test_fixtures.py` fails if they're stale.
- **Forge gotcha:** `sha256(...)` / `ecrecover` are precompile *calls*, so they consume a pending `vm.prank` / `vm.expectRevert`. Compute hashes before the cheatcode.
- Tests marked `anvil` start a local node and deploy from `contracts/out`; they skip without Foundry unless `ARGUS_MINT_REQUIRE_ANVIL=1` (CI's contracts job sets it).
- `uv.lock` is committed; a dependency change needs `make lock` in the same commit.
- Template origin: `argus-pkg-template` via copier. The Dockerfile and GHCR image job are back (for `argus-mint serve`), adapted: the image's context has no `.git`, so the release job passes `VERSION` as a build arg (`SETUPTOOLS_SCM_PRETEND_VERSION`). CI is standalone because the shared argus-ci workflow has no Foundry hook.
- **Wire schema:** a change to `wire.py` (or a model it embeds: `Template`, `Frame`, `BioTemplate`, `CardDraft`) changes `schema/mint-wire.schema.json`; run `make schema` and commit it. Bump `WIRE_VERSION`'s major for breaking changes. Regenerate `fixtures/api/` when responses change shape.

## Branches

`dev` is the default branch and the base for every PR. Cut feature branches from `origin/dev` and bring them up to date by merging `origin/dev`, never `main`. Never open a PR against `main` or push work to it directly: `main` must never be ahead of `dev`. A release fast-forwards `main` to a `dev` commit that passed CI (`git push origin origin/dev:main`), then tags it. If `main` is protected and needs a PR, merge the release PR, then immediately merge `main` back into `dev` so the merge commit is not left on `main` alone.
