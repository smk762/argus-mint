# Changelog

All notable changes to this project are documented here. Versions come from git tags (`hatch-vcs`).

## Unreleased

## 0.1.0 (2026-11-08)

First public release, covering milestones M0–M5. Pre-alpha: the Python API may still change, and the contracts have not had an external review. The Base mainnet deployment is a demo, not a production deployment.

### Added

- **Image review before publishing.** `argus-mint mint` is replaced by `draft` (render and frame into a local draft), `review` (the moderator and the owner each approve or reject the exact image, bound to its sha256) and `publish` (pin, metadata, signed voucher). `cards.check_publishable` refuses a draft that lacks either approval, was rejected, or whose files changed. Nothing reaches IPFS or a voucher before both approvals.
- **Frames.** `frames` module and `[frames]` extra (Pillow): a reviewed PNG overlay with an art window and an optional name slot, composited onto the render. Provenance gains optional `frame` (id, version, hash) and `art` (the unframed render's ipfs:// URI). Both are omitted when unset, so existing cards' `contentHash` is unchanged. `argus-mint frames` validates a directory of frames; there's a placeholder `frames/example-plain-v1`.
- **`argus-mint sample`** renders a few combinations of a draft template (optionally framed) to local files for its reviewer, through `render.render_for_review`. It never pins, signs or mints.
- `templates/example-portrait-v2.yaml` (draft): clean full-bleed portraits for framing, without the card frames, lettering and badges v1's "card art" produced.
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
