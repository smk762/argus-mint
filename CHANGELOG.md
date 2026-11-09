# Changelog

All notable changes to this project are documented here. Versions come from git tags (`hatch-vcs`).

## 0.2.1 (2026-11-09)

### Added

- **The portrait library in the API fixture** (#14), for argus-assay's and argus-studio's demo mode. `fixtures/api/library.json` is `GET /library` for six whole groups of the pre-rendered library (all seven SDXL checkpoints each, ids and group numbers as in the full library), with a 640 px JPEG per entry in `fixtures/api/library/`. One recorded draft is made from a library entry.
- `scripts/api_fixture.py` takes its art and library subset from a built library (`--library`, `--groups`); `ART.png` arguments are optional. The fixture's copy of template v2 also offers a Flux checkpoint and LoRA options, for the pickers.
- `tests/test_api_fixture.py` fails when the fixture is stale: wire version, response shapes, missing images, groups missing a checkpoint.

### Changed

- **Template v2** drops `sdxl/makeArtNotGoon_v8` (weaker output); it lists seven SDXL checkpoints.
- The fixture is re-recorded at wire 1.5 (it was 1.3).

## 0.2.0 (2026-11-09)

### Changed (breaking)

- `argus-mint mint` is gone. A card is now `draft` → `review` (the moderator and the owner each approve the exact image) → `publish`, and nothing is pinned or signed before both approvals. Scripts that called `mint` need the three steps; `submit` takes `publish`'s output as it took `mint`'s.

### Added

- **Image review before publishing.** `argus-mint mint` is replaced by `draft` (render and frame into a local draft), `review` (the moderator and the owner each approve or reject the exact image, bound to its sha256) and `publish` (pin, metadata, signed voucher). `cards.check_publishable` refuses a draft that lacks either approval, was rejected, or whose files changed. Nothing reaches IPFS or a voucher before both approvals.
- **Frames.** `frames` module and `[frames]` extra (Pillow): a reviewed PNG overlay with an art window and an optional name slot, composited onto the render. Provenance gains optional `frame` (id, version, hash) and `art` (the unframed render's ipfs:// URI). Both are omitted when unset, so existing cards' `contentHash` is unchanged. `argus-mint frames` validates a directory of frames; there's a placeholder `frames/example-plain-v1`.
- **Card text and bios.** Frames have named text slots (`texts:`) with fonts, wrapping, shrink-to-fit and ellipsis clipping. `bios` module: a reviewed bio template prompts a language model (`ChatWriter`, OpenAI-style `/chat`; the example uses Mistral NeMo). The model sees only fixed-list values and a `{name}` token, and the real name goes in afterwards. darkroom's text pre-flight screens each bio (`DarkroomClient.check_text`), retrying flagged replies. `draft --bio --writer --text slot=value`. Provenance gains optional `bio` (template id, version, hash, model); the bio becomes the metadata `description`. The card-green frame ships with Cinzel and Crimson Text (SIL OFL).
- **`argus-mint sample`** renders a few combinations of a draft template (optionally framed) to local files for its reviewer, through `render.render_for_review`. It never pins, signs or mints.
- **`argus-mint serve`**: the card workflow over HTTP for argus-studio and argus-assay (`server` extra, port 8108). It lists templates, frames and bios; creates, lists and shows drafts and their images; takes reviews; and publishes. The moderator authenticates with a bearer token. The owner signs an EIP-4361 challenge naming the draft, the decision and the image hash, so neither can act as the other. It uses cortex's write guard and CORS, a read-only mode, and `/health` capabilities. The wire contract `schema/mint-wire.schema.json` comes from `argus-mint schema`, and demo-mode fixtures live in `fixtures/api/`. A Docker image (`ghcr.io/smk762/argus-mint`, published on tags) and a compose profile run it. The CLI and the server share `workflow.create_draft`/`publish`.
- **Catalogue review over HTTP (wire 1.1).**
  - **Samples:** `POST /samples` runs sample cards of a draft template, frame or bio template as a background job, with `GET /samples` to fetch them.
  - **Frame previews:** `POST /frames/{id}/{v}/preview` shows a frame on a draft's or sample's art.
  - **Decisions:** `POST /{templates,frames,bios}/{id}/{v}/review` approves or retires a file. `catalogue.record_decision` writes the decision into the YAML, keeping its comments, and restores the file if it no longer validates.
  - **Listings:** they gain `file` and, for templates, `combinations`; `/health` gains the `sample` and `review_catalogue` capabilities.
- **LoRA choices and sampling per checkpoint family (wire 1.5).**
  - **LoRAs:** templates offer `lora_options` (reviewed: allowed weights, trigger words, compatible checkpoints) and `max_loras`. Renders choose from them with `--lora NAME[@WEIGHT]` or `loras` on `POST /drafts` and `/samples`.
  - **Sampling per family:** `sampling_by_family` sets sampling per checkpoint family, and `Sampling.guidance` covers Flux.
  - **Negative per family:** `negative_by_family` sets the negative prompt per checkpoint family (`""` for Flux, which has no slot for one).
  - **Hashes:** templates that use none of this keep their hash.
  - **darkroom:** LoRAs and families other than SDXL need argus-darkroom 0.1.3 or later with a workflow directory (its Docker image's default), which picks a workflow per family and LoRA count.
- **Selectable checkpoints and a pre-rendered library (wire 1.4).**
  - **Checkpoints:** templates gain `models`, a reviewed list of checkpoints a card may use. Choose one with `draft --model`, `sample --model`, or `model` on `POST /drafts` and `/samples`.
  - **Building:** `argus-mint library build` renders diverse combinations on each checkpoint, with the same variables and seeds, safety-checked and resumable.
  - **Serving:** `/library` and `/library/{id}/image` serve the library, and `POST /drafts` with `library` (or `draft --library --entry`) makes cards from it without a GPU, if the approved template renders exactly that request.
  - **Template v2** lists the eight SDXL checkpoints.
- **Owner edits and styling (wire 1.3).**
  - **Endpoint:** `POST /drafts/{id}/edit` lets the card's owner change its name, bio, display text and style, by signing a challenge that covers exactly that change. The moderator only approves or rejects.
  - **Styles** (`metadata.CardStyle`): a border tint (`frames.tint_matrix`, the same numbers Pillow and an SVG `feColorMatrix` use), plus per-slot text colour, an effect (shadow, outline or glow) and a background box. Any colours; `style_locked` frames refuse them.
  - **Browser preview:** `POST /frames/{id}/{v}/layout` and `GET /frames/{id}/{v}/fonts/…` let a browser draw the card as SVG while it is styled. The baked PNG is what's reviewed.
  - **Provenance** gains optional `style`. Create, samples and frame previews accept a style too.
  - **Redraw:** the card is redrawn on the same art, and both approvals are needed again.
  - **History:** `CardDraft.edits` records each change, and `BioRecord.edited` marks a bio a person changed.
- **Auto-moderation** (`ARGUS_MINT_AUTO_MODERATE=1`): the platform gates on safety, the owner on quality.
  - **When it approves:** when darkroom's image and text checks passed and the name and display text pass the word filter, the moderator's approval is recorded automatically.
  - **When it holds:** otherwise the draft waits for a human moderator, with the reasons in `CardDraft.auto_check`.
  - **The word filter:** `moderation.WordFilter` is better-profanity (new in the `server` extra) plus `ARGUS_MINT_BLOCKLIST`.
- `templates/example-portrait-v2.yaml` (draft): clean full-bleed portraits for framing, without the card frames, lettering and badges v1's "card art" produced.

## 0.1.0 (2026-11-08)

First public release, covering milestones M0–M5. Pre-alpha: the Python API may still change, and the contracts have not had an external review. The Base mainnet deployment is a demo, not a production deployment.

### Added

- Pre-review demo on Base mainnet: same contract addresses as testnet (deployer's first transaction), Sourcify-verified, six cards rendered fresh, pinned on a VPS node, minted and frozen by `demo/base/mint-batch.sh`.
- `scripts/kubo-vps.sh`: native Kubo as a systemd service for a public pinning node. Docker port publishing (rootless especially) left the node unable to accept incoming connections, so gateways couldn't fetch new images.
- M5 live demo on Base Sepolia: contracts deployed and Sourcify-verified, card #1 rendered through darkroom, pinned, minted and frozen, epoch 1 Merkle root anchored. Outputs and a JS verification script are in `demo/base-sepolia/`, and the README has the walkthrough.
- On-chain CLI: `submit` (voucher → `Card.mint`), `freeze` (fix tokenURI at ipfs://), `anchor` (Merkle root → Ledger, with the tree dump). Each pre-checks what the contract would refuse and decodes custom-error reverts. `keys` module: Foundry keystore signers (password file for unattended use, or a hidden prompt) alongside `$ARGUS_MINT_SIGNER_KEY`; `mint --account`. `chain.send(value=)`, `chain.explain_revert`.
- `compose.yaml` with a pinned Kubo node (`make ipfs` / `make ipfs-down`); the example template now names real darkroom models. Verified end to end: darkroom render → Kubo pin → metadata → signed voucher.
- darkroom render client (`darkroom.DarkroomClient`): guarded generation over SSE with record and image-hash checks, outcomes mapped onto the retry protocol; `argus-mint preflight` checks every template combination before approval; live `mint` (`--darkroom`, `--ipfs-api`). Templates gain `sampling` and weighted LoRAs with trigger words.
- Keyless RPC: public Base endpoints with `chain.connect()` failover; contract verification defaults to Sourcify (free), with Blockscout or Basescan as options.
- Base as the target chain: `networks` module (Base 8453, Base Sepolia 84532), `mint --network`, Foundry RPC and Basescan verification config, `make deploy`, `.env.example`, and `Web3Rpc(head="safe"|"finalized")` for following OP Stack safe/finalized heads.
- M4 verifier: ES module under `verifier/` (Merkle proofs, Ledger root/commitment/reveal reads over JSON-RPC, on-chain `verify` cross-check, drand quicknet BLS beacon verification), tested against OpenZeppelin and Python proof fixtures, real quicknet rounds, and JSON-RPC responses recorded from anvil.
- M3 clients: `render` (darklight `Protocol`, safety rejections retried with sha256-derived seeds up to `max_attempts`, every attempt recorded; non-safety errors stop at once), `pin` (`Pinner` protocol, `KuboPinner` for a self-hosted node, `LocalPinner` for tests/dry runs), `ledger` (`LedgerClient`), `indexer` (stateless confirmed-blocks follower with deep-reorg detection, `Web3Rpc` adapter), `chain` helpers, and the CLI (`templates`, `mint --dry-run`).
- M2 contracts: `Card` (ERC-721 + ERC-4906 + ERC-2981, EIP-712 voucher mint, live base URI then per-token freeze to `ipfs://`), `Deed` (ERC-721 + ERC-5192 soulbound, one deed per record id, holder may burn), `Ledger` (sha256 commit/reveal and Merkle-root anchors per epoch, write-once), `Deploy.s.sol`. Forge tests cover every revert, fuzz voucher fields and proofs, and consume Python-built fixtures; anvil-backed pytest mints a Python-signed voucher and verifies Python-built proofs on a live node.
- Committed contract ABIs in `src/argus_mint/abi/` (`make abi`), checked against the build in CI.
- M1 pure core: `templates` (registry, validation, `fill`; template hash excludes `status`), `metadata` (canonical ERC-721 JSON, `content_hash` over image + provenance), `merkle` (OpenZeppelin `StandardMerkleTree` `["bytes32"]` port with `standard-v1` dump/load), `vouchers` (EIP-712 `MintVoucher` for the `Card` contract), `siwe` (EIP-4361 build/parse/verify).
- Merkle fixtures generated by `@openzeppelin/merkle-tree@1.0.8` (`make fixtures`) for 1, 2, 3, 7 and 1,000 leaves.
- M0 scaffold: Python package from `argus-pkg-template`, Foundry project under `contracts/` with OpenZeppelin Contracts v5.4.0 and forge-std v1.17.0 as submodules, Makefile targets for both toolchains, standalone CI (Python + contracts).
