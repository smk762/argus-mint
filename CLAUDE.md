# CLAUDE.md — argus-mint

Guidance for AI agents working in this repo. Human-facing usage lives in [README.md](README.md); this file is the orientation an agent needs to change code safely.

## What this is

Provenance tokens for game assets. A Python package plus Foundry contracts that render art from **reviewed templates** through darklight, pin it to IPFS, build canonical ERC-721 metadata, mint transferable **Card**s and soulbound **Deed**s, and anchor commit/reveal secrets and Merkle roots in a **Ledger** contract so anyone can verify a consumer's records. First consumer: atlas-realms (a browser strategy game), which writes its own adapter.

**argus-mint has no game in it.** It never knows about units, battles, hexes or coins. A consumer supplies a template's variables, a record to show, and 32-byte leaf hashes. Out of scope: game rules/stats, marketplaces, fungible tokens, staking or yield, free-text prompts, a frontend (only a JS verifier module ships).

Part of the [Argus suite](https://github.com/smk762?tab=repositories&q=argus) (sibling of argus-atlas, argus-studio).

## Hard rules (a change that breaks one is a bug)

1. **No raw prompts.** A render is a template id + variables, each picked from that template's fixed list. Free text may appear in metadata, never in a model request. `templates.fill` rejects values outside the list, missing or extra keys.
2. **Only reviewed templates render.** Status `draft | approved | retired`; only `approved` renders. Approval is a human step recorded in the template file (`approved_by`, `approved_at`).
3. **Retry, don't loosen.** A safety rejection is retried with a new deterministic seed up to `max_attempts`. Never change the prompt or disable the check. Every attempt's seed + outcome is recorded.
4. **Tokens carry no power.** Metadata describes a record; it never holds a number a game reads back as a stat. Contracts have no hooks a game calls to decide outcomes.
5. **Nothing pays out.** No fungible token, revenue share or staking. Royalties, if any, are standard ERC-2981 on resale.
6. **Consumers' deterministic code never reads the chain.** The indexer copies chain state into the consumer's DB; game logic reads that copy.
7. **Reproducible.** Same template version + variables + seed → same render request. Metadata JSON is canonical (sorted keys, compact separators, UTF-8) so its hash is stable.
8. **Pure core.** `templates`, `metadata`, `merkle`, `vouchers`, `siwe` are pure (no I/O beyond reading a template file) and tested without a network. Network code (darklight, IPFS, RPC) lives in thin, injectable clients behind `Protocol`s.

## Layout

```
src/argus_mint/   templates render pin metadata merkle vouchers siwe indexer ledger cli
                  abi/   # committed contract ABIs (`make abi`), so pip users never need Foundry
contracts/        Foundry: src/{Card,Deed,Ledger}.sol, test/, script/Deploy.s.sol, lib/ (submodules)
verifier/         ES module: Merkle proofs, Ledger reads, drand quicknet signature checks
templates/        example template YAML (status draft)
scripts/          export_abi.py; fixtures/ (JS generator for @openzeppelin/merkle-tree fixtures)
tests/fixtures/   committed cross-implementation fixtures (Merkle, vouchers)
```

Extras: base install = pure Python (no web3); `chain` = web3 for the indexer and ledger client; `cli` = Typer; `dev` = tests.

## Settled decisions (beyond the spec)

- The ERC-721 contract is **`Card`** (not `UnitCard`): no game words in argus-mint. EIP-712 domain name `"argus-mint Card"`, version `"1"`. Token name/symbol are constructor args.
- The voucher's **`contentHash` = keccak256(canonical({"image": …, "provenance": …}))** — only the immutable part of the metadata, so it stays checkable after live attribute refreshes. `metadata.content_hash()` is the one place this is computed.
- Template hash = keccak256 of the canonical JSON of the template **minus `status`**, so retiring a template doesn't change the hash cards already carry. `approved_by`/`approved_at` are included.
- Ledger commitments are **sha256** (atlas-realms already publishes `sha256(secret)`); Merkle leaves are OpenZeppelin `StandardMerkleTree` `["bytes32"]` (double keccak).
- Contracts are not upgradeable; a new version is a new deployment.
- **OpenZeppelin is pinned to v5.4.0 and `evm_version = "cancun"`** on purpose: OZ ≥ 5.5 emits the `clz` opcode, which needs an Osaka-compatible chain. Bump both together only once the target L2 is confirmed to support Osaka.
- `Deed`: each `recordId` mints once, ever (a burned deed's record stays used), so a retried mint can't duplicate. The consumer makes per-player records with e.g. `keccak256(feat, player)`.
- `Ledger.reveal` works once per epoch and is open to anyone; `Card.refresh`/`freeze` revert after freeze; `setBaseURI` emits `BatchMetadataUpdate` over all minted ids.

## Commands

```bash
make install      # git submodules + uv sync --locked --all-extras
make test         # pytest (no network: clients are stubbed)
make lint         # ruff 0.15.16 check + format --check, forge fmt --check
make forge-build  # / forge-test
make abi          # forge build, then copy ABIs into src/argus_mint/abi (commit them)
make fixtures     # regenerate Merkle fixtures with @openzeppelin/merkle-tree (Node)
make check        # lint test forge-test — every milestone ends green on this
```

Foundry lives in `~/.foundry/bin` (the Makefile finds it there); Node in `~/.local/bin`.

## Conventions & gotchas

- **Versioning is git-tag-derived** (`hatch-vcs`); `_version.py` is generated. Each milestone ends with a pre-release tag (`v0.0.1a1`, `v0.1.0a1`, …) and a CHANGELOG heading.
- **ABIs are committed and checked in CI** — after a contract change run `make abi` and commit the result in the same commit.
- **Fixtures:** `tests/fixtures/merkle.json` comes from the reference JS library (`@openzeppelin/merkle-tree`) and must never be regenerated to make a failing test pass. `vouchers.json` and `proofs.json` are Python-built (`scripts/py_fixtures.py`) for the forge tests; `tests/test_fixtures.py` fails if they're stale.
- **Forge gotcha:** `sha256(...)` / `ecrecover` are precompile *calls*, so they consume a pending `vm.prank` / `vm.expectRevert`. Compute hashes before the cheatcode.
- Tests marked `anvil` start a local node and deploy from `contracts/out`; they skip without Foundry unless `ARGUS_MINT_REQUIRE_ANVIL=1` (CI's contracts job sets it).
- `uv.lock` is committed; a dependency change needs `make lock` in the same commit.
- Template origin: `argus-pkg-template` via copier. The template's Dockerfile and GHCR image jobs were removed on purpose (library, no service); CI is standalone because the shared argus-ci workflow has no Foundry hook. Re-delete those if `copier update` brings them back.
- Branches: `dev` is the default and takes feature PRs; `main` gets `dev` → `main` release PRs (same as argus-atlas).
