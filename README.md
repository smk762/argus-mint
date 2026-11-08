# argus-mint

Provenance tokens for game assets. argus-mint renders art from reviewed templates, pins it to IPFS, builds canonical ERC-721 metadata, mints transferable cards and soulbound deeds, and anchors commit/reveal secrets and Merkle roots on-chain so anyone can check a game's records.

It contains no game logic. A consumer supplies a template's variables, a record to show, and 32-byte leaf hashes; argus-mint does the rest. Tokens carry no in-game power, and nothing pays out.

Part of the [Argus suite](https://github.com/smk762?tab=repositories&q=argus).

> Status: pre-alpha, under construction (milestone M0).

## What's in the box

| Part | What it does |
| --- | --- |
| `argus_mint` (Python) | template registry, darklight render client with retries, IPFS pinning, canonical metadata, OpenZeppelin-compatible Merkle trees, EIP-712 mint vouchers, SIWE, a reorg-safe event indexer |
| `contracts/` (Foundry) | `Card` (ERC-721 + ERC-4906 + ERC-2981, voucher mint), `Deed` (ERC-721 + ERC-5192 soulbound), `Ledger` (commit/reveal/anchor notary) |
| `verifier/` (ES module) | verify Merkle proofs against a Ledger root, revealed secrets against commitments, and drand quicknet beacons |

## Install

```bash
uv pip install argus-mint            # pure Python core
uv pip install "argus-mint[chain]"   # + web3 for the indexer and ledger client
uv pip install "argus-mint[cli]"     # + the argus-mint command
```

## Develop

Needs [uv](https://docs.astral.sh/uv/), [Foundry](https://book.getfoundry.sh/) and Node (for fixtures and the verifier).

```bash
git clone --recurse-submodules https://github.com/smk762/argus-mint
make install       # submodules + venv with all extras
make check         # lint + pytest + forge test
make help          # everything else
```

Versions are derived from git tags via `hatch-vcs`. Tagging `vX.Y.Z` publishes to PyPI.

## License

MIT
