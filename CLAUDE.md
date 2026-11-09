# CLAUDE.md — argus-mint

Guidance for AI agents working in this repo. Human-facing usage lives in [README.md](README.md); this file is the orientation an agent needs to change code safely.

## What this is

Provenance tokens for game assets. A Python package plus Foundry contracts that render art from **reviewed templates** through darklight, pin it to IPFS, build canonical ERC-721 metadata, mint transferable **Card**s and soulbound **Deed**s, and anchor commit/reveal secrets and Merkle roots in a **Ledger** contract so anyone can verify a consumer's records. First consumer: atlas-realms (a browser strategy game), which writes its own adapter.

**argus-mint has no game in it.** It never knows about units, battles, hexes or coins. A consumer supplies a template's variables, a record to show, and 32-byte leaf hashes. Out of scope: game rules/stats, marketplaces, fungible tokens, staking or yield, free-text prompts, a frontend (only a JS verifier module ships).

Part of the [Argus suite](https://github.com/smk762?tab=repositories&q=argus) (sibling of argus-atlas, argus-studio).

## Hard rules (a change that breaks one is a bug)

1. **No raw prompts.** A render is a template id + variables, each picked from that template's fixed list. Free text may appear in metadata, never in a model request. `templates.fill` rejects values outside the list, missing or extra keys.
2. **Only reviewed templates and frames make cards.** Status `draft | approved | retired`; only `approved` templates render cards and only `approved` frames go on them. Approval is a human step recorded in the file (`approved_by`, `approved_at`). `argus-mint sample` / `render.render_for_review` render drafts for their reviewer, to local files only: never pinned, signed or minted.
3. **Retry, don't loosen.** A safety rejection is retried with a new deterministic seed up to `max_attempts`. Never change the prompt or disable the check. Every attempt's seed + outcome is recorded.
4. **Tokens carry no power.** Metadata describes a record; it never holds a number a game reads back as a stat. Contracts have no hooks a game calls to decide outcomes.
5. **Nothing pays out.** No fungible token, revenue share or staking. Royalties, if any, are standard ERC-2981 on resale.
6. **Consumers' deterministic code never reads the chain.** The indexer copies chain state into the consumer's DB; game logic reads that copy.
7. **Reproducible.** Same template version + variables + seed → same render request. Metadata JSON is canonical (sorted keys, compact separators, UTF-8) so its hash is stable.
8. **Pure core.** `templates`, `metadata`, `merkle`, `vouchers`, `siwe` are pure (no I/O beyond reading a template file) and tested without a network. Network code (darklight, IPFS, RPC) lives in thin, injectable clients behind `Protocol`s.
9. **Every card image is reviewed before it is published.** `draft` (render + frame, local files) → `review` (the moderator **and** the owner each approve or reject the exact image, bound to its sha256) → `publish` (pin, metadata, signed voucher). `cards.check_publishable` refuses anything else; a rejection closes the draft. Nothing reaches IPFS or a voucher before both approvals.

## Layout

```
src/argus_mint/   templates metadata merkle vouchers siwe     # pure core (_codec: keccak, canonical JSON, field types)
                  frames cards                             # frame overlays (Pillow, `frames` extra); card drafts + image review
                  render pin indexer                       # clients behind Protocols (no web3 needed)
                  chain ledger                             # web3 clients (`chain` extra)
                  keys                                     # signers: Foundry keystore (password file/prompt) or $ARGUS_MINT_SIGNER_KEY
                  cli                                      # Typer: templates, frames, preflight, sample, draft, review, publish, submit, freeze, anchor
                  abi/   # committed contract ABIs (`make abi`, abi.load(name)), so pip users never need Foundry
contracts/        Foundry: src/{Card,Deed,Ledger}.sol, test/, script/Deploy.s.sol, lib/ (submodules)
verifier/         ES module: Merkle proofs, Ledger reads, drand quicknet signature checks
templates/        example template YAML (example-portrait v1: approved; v2: draft, clean full-bleed art for framing)
frames/           card-green v1 (the card frame, draft; ~4:3 window) and example-plain v1 (placeholder, square window, drawn by scripts/make_example_frame.py)
scripts/          export_abi.py; fixtures/ (JS generator for @openzeppelin/merkle-tree fixtures)
tests/fixtures/   committed cross-implementation fixtures (Merkle, vouchers)
```

Extras: base install = pure Python (no web3); `chain` = web3 for the indexer and ledger client; `cli` = Typer; `frames` = Pillow for compositing; `dev` = tests.

## Settled decisions (beyond the spec)

- The ERC-721 contract is **`Card`** (not `UnitCard`): no game words in argus-mint. EIP-712 domain name `"argus-mint Card"`, version `"1"`. Token name/symbol are constructor args.
- The voucher's **`contentHash` = keccak256(canonical({"image": …, "provenance": …}))** — only the immutable part of the metadata, so it stays checkable after live attribute refreshes. `metadata.content_hash()` is the one place this is computed.
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
- Drafts (`cards.py`): a directory with `draft.json`, `art.png` and, when framed, `card.png`. Reviews stay in `draft.json` (not in public metadata). The template's `width`/`height` go to darkroom inside `sampling` (its workflow's `$width`/`$height`), so match the render's aspect to the frame's art window: the compositor crops whatever doesn't fit (cover).
- Signers (`keys.load_signer`): `--account` / `$ARGUS_MINT_ACCOUNT` names a Foundry keystore (`~/.foundry/keystores/<name>` or a path), password from `$ARGUS_MINT_KEYSTORE_PASSWORD_FILE` or a hidden prompt; else `$ARGUS_MINT_SIGNER_KEY`. Never a key on the command line.
- `submit` / `freeze` / `anchor` pre-check what the contract would refuse (role, already minted/frozen/anchored, expiry, chain id) before asking for a password or spending gas; a revert that still happens is decoded to the custom error's name by `chain.explain_revert`. `submit` refuses `dry_run` outputs. `anchor --tree` writes the standard-v1 dump *before* sending, so a landed root always has its tree.
- Pinning: `compose.yaml` runs a Kubo node (`make ipfs`); its RPC (5001) is admin-level and stays on 127.0.0.1, swarm 4001 is public. A Kubo `add` of a file > 256 KiB returns a dag-pb CID (`bafybei…`), not a raw one; that's expected.
- darkroom LoRAs need a workflow with `$lora`/`$lora_weight` slots; the deployed plain `sdxl.json` has none, so a template with LoRAs is refused (400) there. Model names come from darkroom's `GET /models`.

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

- **Versioning is git-tag-derived** (`hatch-vcs`); `_version.py` is generated. Each milestone ends with a pre-release tag (`v0.0.1a1`, `v0.1.0a1`, …) and a CHANGELOG heading.
- **ABIs are committed and checked in CI** — after a contract change run `make abi` and commit the result in the same commit.
- **Fixtures:** `tests/fixtures/merkle.json` comes from the reference JS library (`@openzeppelin/merkle-tree`) and must never be regenerated to make a failing test pass. `vouchers.json` and `proofs.json` are Python-built (`scripts/py_fixtures.py`) for the forge tests; `tests/test_fixtures.py` fails if they're stale.
- **Forge gotcha:** `sha256(...)` / `ecrecover` are precompile *calls*, so they consume a pending `vm.prank` / `vm.expectRevert`. Compute hashes before the cheatcode.
- Tests marked `anvil` start a local node and deploy from `contracts/out`; they skip without Foundry unless `ARGUS_MINT_REQUIRE_ANVIL=1` (CI's contracts job sets it).
- `uv.lock` is committed; a dependency change needs `make lock` in the same commit.
- Template origin: `argus-pkg-template` via copier. The template's Dockerfile and GHCR image jobs were removed on purpose (library, no service; `compose.yaml` only runs dependencies such as Kubo); CI is standalone because the shared argus-ci workflow has no Foundry hook. Re-delete those if `copier update` brings them back.
- Branches: `dev` is the default and takes feature PRs; `main` gets `dev` → `main` release PRs (same as argus-atlas).
