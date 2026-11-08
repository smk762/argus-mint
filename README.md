# argus-mint

Provenance tokens for game assets. argus-mint renders art from reviewed templates, pins it to IPFS, builds canonical ERC-721 metadata, mints transferable cards and soulbound deeds, and anchors commit/reveal secrets and Merkle roots on-chain so anyone can check a game's records.

It contains no game logic. A consumer supplies a template's variables, a record to show, and 32-byte leaf hashes; argus-mint does the rest. Tokens carry no in-game power, and nothing pays out.

Part of the [Argus suite](https://github.com/smk762?tab=repositories&q=argus).

> Status: pre-alpha. Milestones M0–M5 are done: pure core, contracts, clients, JS verifier, and a live demo on Base Sepolia (below). Renders go through argus-darkroom.

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

## Live demo (Base Sepolia)

Deployed 2026-11-08 from this repo (`make deploy`), source verified on Sourcify:

| Contract | Address |
|---|---|
| Card | [`0xCAa3573b5550870A5e15C47c650b8700D70a8fA7`](https://sepolia.basescan.org/address/0xCAa3573b5550870A5e15C47c650b8700D70a8fA7) |
| Deed | [`0xe44D67bdFd3416F97206fdE7aeE55F9008EB5D7c`](https://sepolia.basescan.org/address/0xe44D67bdFd3416F97206fdE7aeE55F9008EB5D7c) |
| Ledger | [`0x4b41B687C694bd8bf3778871752637445c2Aa254`](https://sepolia.basescan.org/address/0x4b41B687C694bd8bf3778871752637445c2Aa254) |

**Card #1** was made end to end. Every command's output is in [demo/base-sepolia/](demo/base-sepolia/).

1. **Render.** The approved `example-portrait` v1 template was rendered with fixed values and seed 42: `portrait of a female dwarf, bronze heraldry, mining, cave, …`. darkroom passed it on the first attempt under its SFW `default` policy (record `cb2bd951…`).
2. **Pin.** The image and canonical metadata were pinned on Kubo. The metadata's `provenance` carries the template hash, seed and attempt count.
3. **Sign.** A mint voucher was signed for the Card (EIP-712), carrying `contentHash` = keccak256 of the metadata's image and provenance.
4. **[Submit](https://sepolia.basescan.org/tx/0x194f515824b65ff610fc61010ff01370745ed7c8a134cca2385d36bc080c5324)** minted token 1.
5. **[Freeze](https://sepolia.basescan.org/tx/0x0f3140ec791db3069693ee9cb655f5ded8ca65b3bfcc33c9dfc7e4989de84bb4)** fixed `tokenURI(1)` at `ipfs://bafkreid3o6zha6adg7ndl4rpmnsv7bc22t6ak2ik4opr6oed56v74uhefu`.

**Epoch 1 anchor:** four demo records ([records.txt](demo/base-sepolia/records.txt)) were hashed with sha256 into a Merkle tree. Its root, `0x4cafc06c…1ee1f736`, was [anchored in the Ledger](https://sepolia.basescan.org/tx/0x98899e0c2d074f0b63e30af5d7d52ee1ace470024932fa64124cdcda10584695).

### Verify it yourself

Anchored records, with the JS verifier and the public RPC (needs only Node):

```bash
make verifier-install
node demo/base-sepolia/verify.mjs          # each record checks out; a changed one is rejected
```

To check the card, read `tokenURI(1)` and `contentHashOf(1)` from the Card and fetch the metadata from IPFS. Then confirm `keccak256(canonical({image, provenance}))` equals the on-chain `contentHash`:

```python
from argus_mint import chain
from argus_mint.metadata import content_hash
card = chain.contract(chain.connect("base-sepolia"), "Card", "0xCAa3573b5550870A5e15C47c650b8700D70a8fA7")
# doc = the JSON at card.functions.tokenURI(1).call()
assert content_hash(doc) == bytes(card.functions.contentHashOf(1).call())
```

## License

MIT
