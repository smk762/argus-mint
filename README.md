# argus-mint

Provenance tokens for game assets. argus-mint renders art from reviewed templates, pins it to IPFS, builds canonical ERC-721 metadata, mints transferable cards and soulbound deeds, and anchors commit/reveal secrets and Merkle roots on-chain so anyone can check a game's records.

It contains no game logic. A consumer supplies a template's variables, a record to show, and 32-byte leaf hashes; argus-mint does the rest. Tokens carry no in-game power, and nothing pays out.

Part of the [Argus suite](https://github.com/smk762?tab=repositories&q=argus).

> Status: pre-alpha. Milestones M0–M5 are done: pure core, contracts, clients, JS verifier, and a live demo on Base Sepolia, plus a pre-review demo on Base mainnet (below). Renders go through argus-darkroom.

## What's in the box

| Part | What it does |
| --- | --- |
| `argus_mint` (Python) | template registry, darklight render client with retries, card frames, a draft → review → publish flow, IPFS pinning, canonical metadata, OpenZeppelin-compatible Merkle trees, EIP-712 mint vouchers, SIWE, a reorg-safe event indexer |
| `contracts/` (Foundry) | `Card` (ERC-721 + ERC-4906 + ERC-2981, voucher mint), `Deed` (ERC-721 + ERC-5192 soulbound), `Ledger` (commit/reveal/anchor notary) |
| `argus-mint serve` (HTTP) | the draft → review → publish flow for argus-studio and argus-assay, with a pinned wire schema and a Docker image |
| `verifier/` (ES module) | verify Merkle proofs against a Ledger root, revealed secrets against commitments, and drand quicknet beacons |

## Install

```bash
uv pip install argus-mint            # pure Python core
uv pip install "argus-mint[chain]"   # + web3 for the indexer and ledger client
uv pip install "argus-mint[cli]"     # + the argus-mint command
uv pip install "argus-mint[frames]"  # + Pillow, to composite frames onto renders
uv pip install "argus-mint[server]"  # + the HTTP service (argus-mint serve)
```

## Quick look

```bash
argus-mint templates templates/                 # validate templates, show status and hash
argus-mint draft --template my-approved.yaml --out drafts/unit-17 --placeholder \
  --var role=ranger --var colour=none --var pose=standing --seed 81234 \
  --asset unit-17 --to 0x7099…79C8 --name "Hana of the Strait"
argus-mint review drafts/unit-17 --as moderator --by mod-1 --approve
argus-mint review drafts/unit-17 --as owner --by 0x7099…79C8 --approve
argus-mint publish drafts/unit-17 --card 0x5FbD…0aa3 --chain-id 31337 --dry-run   # metadata, voucher, signature
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

## Making a card: draft, review, publish

Every card image is seen by people before it is published. Rendering makes a local **draft**. The moderator and the card's owner each **review** that exact image (its sha256), and only when both approve does **publish** pin it and sign the mint voucher. A rejection closes the draft; render a new one.

```bash
make ipfs                                    # local Kubo node (compose.yaml): RPC 127.0.0.1:5001, gateway :8080

# reviewing a template or frame, before approving it
argus-mint preflight my-template.yaml --darkroom http://localhost:8106      # text check, every combination
argus-mint sample my-template.yaml --out samples/ --count 6 --frame frames/my-frame.yaml \
  --darkroom http://localhost:8106                                          # sample images, local files only

# a card
argus-mint draft --template my-template.yaml --frame frames/my-frame.yaml --out drafts/unit-17 \
  --var ... --asset unit-17 --to 0x… --name "…" --darkroom http://localhost:8106 \
  --bio bios/my-bio.yaml --writer http://<chat api> --text "class=Ranger of the Strait" --text "stats=No. 17"
argus-mint review drafts/unit-17 --as moderator --by <name> --approve       # or --reject --note "why"
argus-mint review drafts/unit-17 --as owner --by <name> --approve
argus-mint publish drafts/unit-17 --card 0x… --ipfs-api http://127.0.0.1:5001 > unit-17.json   # pins, signs
```

**Frames.** Renders are clean, full-bleed art. A frame is a reviewed PNG overlay, transparent inside its art window, with an optional slot for the card's name. It's described in a YAML file next to it, which goes through the same `draft | approved | retired` review as templates ([frames/card-green-v1.yaml](frames/card-green-v1.yaml)). Render at the art window's aspect ratio (the template's `width`/`height`): the art is scaled to cover the window and the overflow is cropped. A framed card's provenance names the frame and its hash, and points to the unframed art on IPFS.

**Card text.** A frame has text slots, each with a font, size and box; text shrinks to fit and is clipped with an ellipsis if it still doesn't. argus-mint fills `name` and `bio`. The consumer fills the rest (`--text class=…`, `--text stats=…`) with display text from its own records. That text is shown on the card, never written into the metadata as stats.

**Bios.** A language model writes each card's flavour text from a reviewed bio template ([bios/example-bio-v1.yaml](bios/example-bio-v1.yaml): Mistral NeMo, fruity). It sees only the template's fixed-list values and writes a `{name}` token; argus-mint puts the real name in afterwards, so a player's name never reaches a model. darkroom's text check screens every bio, and flagged ones are rewritten. The moderator and the owner then read it on the card during review. The bio becomes the card's `description`.

On-chain, each step signs with a Foundry keystore account (`--account` / `$ARGUS_MINT_ACCOUNT`) or `$ARGUS_MINT_SIGNER_KEY`:

```bash
argus-mint submit unit-17.json                                        # send the voucher to Card.mint
argus-mint freeze --card 0x… --token-id 1 --uri <metadata cid>        # fix tokenURI at ipfs://…
argus-mint anchor leaves.txt --ledger 0x… --epoch 1 --tree epoch-1.json   # Merkle root into the Ledger
```

The Kubo RPC port is an admin API and is published on localhost only. Swarm port 4001 is public so wallets and gateways can fetch pinned art; behind NAT, pin on a VPS (native Kubo, reached through an SSH tunnel) ([docs/deploy.md §6](docs/deploy.md#6-pin-on-a-vps-optional)) so cards stay retrievable when this host is offline.

## Running the service

`argus-mint serve` puts the card workflow behind HTTP for the suite's UIs: argus-studio's pages and argus-assay, the standalone review UI. The server decides; a UI only shows what it says.

```bash
make serve                                  # local, 127.0.0.1:8108, settings from .env
make serve-up                               # or in docker with Kubo (compose profile `server`)
```

| Endpoint | |
|---|---|
| `GET /health` | version, wire version, network, Card, what it can do (`capabilities`), which backends answer |
| `GET /templates` `/frames` `/bios` | the reviewed files, every status, with hashes; `GET /frames/{id}/{version}/overlay` |
| `GET /drafts` | newest first, `?status=pending\|approved\|rejected\|published`, paged |
| `GET /drafts/{id}` | the draft and its reviews; the pins and signed voucher once published |
| `GET /drafts/{id}/image` `/art` | the card image reviewers approve, and the unframed render |
| `POST /drafts` | render, write the bio, frame: a new pending draft (moderator token) |
| `POST /drafts/{id}/challenge` | the sign-in message the owner's wallet signs to approve or reject |
| `POST /drafts/{id}/review` | moderator by token, owner by signed challenge |
| `POST /drafts/{id}/publish` | pin and sign an approved draft (moderator token) |
| `POST /drafts/{id}/edit` | the owner changes the card's text or style, with a wallet signature covering exactly that change. The card is redrawn on the same art and needs both approvals again. The moderator only approves or rejects |
| `POST /frames/{id}/{version}/layout`, `GET /frames/{id}/{version}/fonts/…` | where each line of text goes, and the frame's fonts, so a browser can preview a restyled card as SVG before it is baked |
| `POST /samples`, `GET /samples[/{id}]` | sample cards of a template, frame and bio (any status) for their reviewer, as background jobs; local only (moderator token) |
| `POST /frames/{id}/{version}/preview` | a frame (any status) on a draft's or sample's art, as a PNG |
| `POST /{templates,frames,bios}/{id}/{version}/review` | approve a draft or retire an approved file. This writes `status`, `approved_by` and `approved_at` into the YAML, keeping its comments (moderator token) |

**Who can do what.** The moderator token (`ARGUS_MINT_MODERATOR_TOKEN`) creates drafts, renders samples, approves or retires templates, frames and bio templates, reviews as the moderator, and publishes. The owner reviews by signing an EIP-4361 (Sign-In with Ethereum) message with the wallet the card will be minted to. The message names the draft, the decision and the image's sha256, and works once. Neither role can act as the other.

**Auto-moderation** (`ARGUS_MINT_AUTO_MODERATE=1`, off by default): the platform gates on safety, the owner on quality.
- **When it approves:** if darkroom's image guards passed the art, its text check passed the bio, and the name and display text pass a word filter, the moderator's approval is recorded as `auto: platform checks`.
- **When it holds:** anything else waits for a human moderator, with the reason in `auto_check.held`.
- **The word filter:** better-profanity's list, which catches letter swaps, plus your own terms in `ARGUS_MINT_BLOCKLIST`, one per line.
- **The owner** always reviews.

**Editing and styling are the owner's.** The owner can change the name, the bio and the display text. They can also restyle the frame:
- **Border:** a tint colour and strength.
- **Each text slot:** colour, an effect (shadow, outline or glow) and a background (colour, opacity, rounded corners).

Any colours are allowed; if the owner wants black on black, so be it. A frame can set `style_locked: true` to refuse restyling.

The browser previews a restyle as SVG from `/layout` and the fonts, using the same tint matrix as the server. argus-mint bakes the PNG when the owner confirms, and that PNG is what gets reviewed and published. Provenance records the style, so the card can be redrawn exactly. Edited text never reaches a model, so an edited bio goes through the word filter rather than darkroom.

**Configuration** is `ARGUS_MINT_*` (see [.env.example](.env.example)):
- **Files:** `DRAFTS_DIR`, `TEMPLATES_DIR`, `FRAMES_DIR`, `BIOS_DIR`.
- **Backends:** `DARKROOM_URL`, `WRITER_URL`, `IPFS_API`.
- **Signing:** `ACCOUNT` plus `KEYSTORE_PASSWORD_FILE`, or `SIGNER_KEY`.
- **Chain:** `NETWORK`, `CARD`.
- **Access:** `MODERATOR_TOKEN`, `SIWE_DOMAIN` (the UI's host, as wallets show it), `CORS_ORIGINS`, `READ_ONLY`.

Like the other suite services, it binds to localhost and refuses writes from browser origins it wasn't told about.

**For UI authors:**
- The wire contract is [schema/mint-wire.schema.json](schema/mint-wire.schema.json) (`argus-mint schema`), pinned by release tag and sha256 the way argus-studio pins the other services.
- [fixtures/api/](fixtures/api/) holds recorded responses with a draft in each state, for demo mode.
- The image is `ghcr.io/smk762/argus-mint`.

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

## Demo on Base mainnet (pre-review)

The same script was deployed to Base (8453) on 2026-11-08. It was the deployer's first transaction there, so the addresses equal the testnet ones, and all three contracts are verified on Sourcify. This is a demo: the contracts and deployment get a review before anything goes live, and the deployer still holds every role.

Six cards were rendered fresh from the approved `example-portrait` v1 template, one darkroom attempt each. They were pinned on a VPS Kubo node, minted and frozen by [demo/base/mint-batch.sh](demo/base/mint-batch.sh), and every step's output is in [demo/base/](demo/base/). Each card's on-chain `contentHash` matches its pinned metadata. Both demos predate the review step: they used the one-shot `mint` command that `draft` / `review` / `publish` replaced, and their template drew its own card frames, which led to frames becoming a separate step.

| Token | Name | Mint | Freeze |
|---|---|---|---|
| [1](https://basescan.org/nft/0xCAa3573b5550870A5e15C47c650b8700D70a8fA7/1) | Azure Knight | [tx](https://basescan.org/tx/0x595626d4645f5e067b8e1bd7c8b67cc2ea12c8051a5fb1846784659110496c30) | [tx](https://basescan.org/tx/0x39089c0f17410e044a407f0b9cf7d9125d5ecde71400200702e25df0669f6e50) |
| [2](https://basescan.org/nft/0xCAa3573b5550870A5e15C47c650b8700D70a8fA7/2) | Emerald Wizard | [tx](https://basescan.org/tx/0x15a60342852c7f40f7ea283a55637195f102b69899bb82a2ff638f1130808bc8) | [tx](https://basescan.org/tx/0x52ead3396a533c8232ecab5540d61f9c3ef3d0bcbbf7b816aad1790088683b28) |
| [3](https://basescan.org/nft/0xCAa3573b5550870A5e15C47c650b8700D70a8fA7/3) | Steel Cyborg | [tx](https://basescan.org/tx/0xfed072d1830d14c14bf4c60b912c079873e36cdfafc5bdb6cc6640e5b91d2a3e) | [tx](https://basescan.org/tx/0xa783b4aa35d4f382f1d19b21cb46377b70c83cb5a3a6c31f240d991e39935991) |
| [4](https://basescan.org/nft/0xCAa3573b5550870A5e15C47c650b8700D70a8fA7/4) | Silver Huntress | [tx](https://basescan.org/tx/0xef0aecf8187f30aaa919f3fc48db4dfdd8a34aeb64485851c4ffff09d79a94ae) | [tx](https://basescan.org/tx/0x69af3f6c0ff486aae3f3aa2e63602e22e2c268382becec430a7eecaf7ea6c155) |
| [5](https://basescan.org/nft/0xCAa3573b5550870A5e15C47c650b8700D70a8fA7/5) | Crimson Orc | [tx](https://basescan.org/tx/0xd3e4dde31bdec93d97d8ed469028eaddd1c98d8e1a33f40b6517cc3e04170858) | [tx](https://basescan.org/tx/0xc8c1b77f81b5b9338a37836d6905e60bb665b06b6d61c61bf4f75237d761ab16) |
| [6](https://basescan.org/nft/0xCAa3573b5550870A5e15C47c650b8700D70a8fA7/6) | Golden Priest | [tx](https://basescan.org/tx/0x9c7d71c1e30d34eef8ba3e149800d70c7d44f5705127e69e5e81c6ea33d6b192) | [tx](https://basescan.org/tx/0x437f03b5ad49bab8b6505a02179277bce97b395c959518b1c801c923238c0008) |

The collection is also on [OpenSea](https://opensea.io/collection/argus-mint-card). The same `contentHash` check as above works with `chain.connect("base")`.

## License

MIT
