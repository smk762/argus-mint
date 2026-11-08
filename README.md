# argus-mint

Provenance tokens for game assets. argus-mint renders art from reviewed templates, pins it to IPFS, builds canonical ERC-721 metadata, mints transferable cards and soulbound deeds, and anchors commit/reveal secrets and Merkle roots on-chain so anyone can check a game's records.

It contains no game logic. A consumer supplies a template's variables, a record to show, and 32-byte leaf hashes; argus-mint does the rest. Tokens carry no in-game power, and nothing pays out.

Part of the [Argus suite](https://github.com/smk762?tab=repositories&q=argus).

> Status: pre-alpha. Milestones M0–M4 are built (pure core, contracts, clients, JS verifier). Target chain: **Base**. Renders go through argus-darkroom. Next: the Base Sepolia demo (M5).

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

## Quick look

```bash
argus-mint templates templates/                 # validate templates, show status and hash
argus-mint mint --template my-approved.yaml \
  --var role=ranger --var colour=none --var pose=standing --seed 81234 \
  --asset unit-17 --to 0x7099…79C8 --name "Hana of the Strait" \
  --card 0x5FbD…0aa3 --chain-id 31337 --dry-run  # prints metadata, voucher and signature
```

```python
from argus_mint import merkle, templates, vouchers
from argus_mint.metadata import card_metadata, content_hash

tree = merkle.build(leaf_hashes)            # OpenZeppelin StandardMerkleTree-compatible
proof = tree.proof(3)                       # verifies in Ledger.verify and MerkleProof.verify
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

## Deploy (Base)

```bash
cp .env.example .env                        # RPC URLs and role addresses (no paid API keys needed)
cast wallet new deployer                    # new key, straight into Foundry's encrypted keystore; fund its address
make deploy                                 # Base Sepolia; `make deploy NETWORK=base` for mainnet
```

The script deploys `Card`, `Deed` and `Ledger` over plain JSON-RPC, grants roles, and verifies the sources on Sourcify for free (Blockscout displays them). `VERIFIER=blockscout` verifies on Blockscout directly; `VERIFIER=etherscan` uses Basescan and needs an Etherscan API key. Step by step, including wallet backup, funding and role setup: [docs/deploy.md](docs/deploy.md).

## Rendering with argus-darkroom, pinning with Kubo

```bash
make ipfs                                    # local Kubo node (compose.yaml): RPC 127.0.0.1:5001, gateway :8080
argus-mint preflight my-template.yaml --darkroom http://localhost:8106      # reviewer check, every combination
argus-mint mint --template my-template.yaml --var ... --asset unit-17 --to 0x… --name "…" --card 0x… \
  --darkroom http://localhost:8106 --ipfs-api http://127.0.0.1:5001         # live: renders, pins, signs
```

On-chain, each step signs with a Foundry keystore account (`--account` / `$ARGUS_MINT_ACCOUNT`) or `$ARGUS_MINT_SIGNER_KEY`:

```bash
argus-mint submit demo-1.json                                         # send the voucher to Card.mint
argus-mint freeze --card 0x… --token-id 1 --uri <metadata cid>        # fix tokenURI at ipfs://…
argus-mint anchor leaves.txt --ledger 0x… --epoch 1 --tree epoch-1.json   # Merkle root into the Ledger
```

The Kubo RPC port is an admin API and is published on localhost only. Swarm port 4001 is public so wallets and gateways can fetch pinned art; behind NAT, forward 4001 (tcp+udp) or add a remote pinning service so cards stay retrievable when this host is offline.

## License

MIT
