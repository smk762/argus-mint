# Deploying argus-mint to Base

How to set up a deployer wallet, fund it, and deploy `Card`, `Deed` and `Ledger`. Start on **Base Sepolia** (testnet, chain 84532); mainnet (`base`, chain 8453) works the same way with real ETH.

Nothing here needs a paid API key. Deploying talks to a public JSON-RPC endpoint, and the contract source is verified on Sourcify, which is free.

## 1. Create the deployer wallet

The deployer is an ordinary Ethereum account: a private key and its address. It pays gas for the deployment, and unless you set the roles in step 3 it also gets every role on the new contracts.

Create it straight into Foundry's encrypted keystore, so the private key is never written to disk unencrypted:

```bash
cast wallet new deployer        # prompts for a password; prints the address
cast wallet list                # shows: deployer (Local)
cast wallet address --account deployer
```

The key is stored, encrypted with that password, in `~/.foundry/keystores/deployer`.

**Back it up.** If this machine dies, the keystore file goes with it. Pick one:
- Copy `~/.foundry/keystores/deployer` somewhere safe and remember the password. The file is useless without the password.
- Or put the raw key in a password manager: `cast wallet decrypt-keystore deployer` prints it once.

Don't store the key in a plaintext file, a `.env`, shell history or a chat. `.env` holds only the *account name* (`DEPLOYER_ACCOUNT=deployer`), never a key.

**Already have a key?** `cast wallet import deployer --interactive` asks for it (input is hidden) and stores it the same way.

### Why not `cast wallet new` and save the key?

Plain `cast wallet new` prints the private key to the terminal. That works: fund the address, then `cast wallet import` the key. But the key then sits in your scrollback, and possibly a terminal log, and wherever you pasted it. `cast wallet new deployer` gives you the same account without the key ever appearing on screen.

## 2. Fund it

Send Base Sepolia ETH to the address from step 1. About **0.01 ETH** covers deploying all three contracts with plenty to spare; Base gas is cheap.

**Recommended: Google's faucet, then bridge.** [Google Cloud's Web3 faucet](https://cloud.google.com/application/web3/faucet/ethereum/sepolia) needs only a Google sign-in. It pays out *Ethereum* Sepolia ETH, not Base Sepolia, so bridge it across afterwards:

1. Request Sepolia ETH from the faucet, sent to the deployer's address.
2. Send it to Base Sepolia's bridge on Ethereum Sepolia: `0xfd0Bf71F60660E2f608ed56e1659C450eB113120` (the `L1StandardBridge`). ETH sent to this contract from an ordinary wallet arrives at **the same address** on Base Sepolia, usually within a few minutes:
   ```bash
   cast send 0xfd0Bf71F60660E2f608ed56e1659C450eB113120 --value 0.04ether \
     --account deployer --rpc-url https://ethereum-sepolia-rpc.publicnode.com
   ```
   Send less than the faucet gave you: keep about 0.005 ETH back for this transaction's Ethereum Sepolia gas. 0.01 ETH on Base Sepolia is enough to deploy.

Only send to this address from a wallet account, not from a contract or a multisig: anything else can't receive the deposit at the same address. Checked 2026-11-08: the contract reports Base Sepolia (chain 84532) as its destination and is not paused.

**Alternatives** (most need an account): the Coinbase Developer Platform faucet, and Alchemy's or QuickNode's faucets, pay out Base Sepolia ETH directly.

Check the balance on both chains:

```bash
ADDR=$(cast wallet address --account deployer)
cast balance --ether --rpc-url https://ethereum-sepolia-rpc.publicnode.com $ADDR   # Ethereum Sepolia (faucet)
cast balance --ether --rpc-url https://sepolia.base.org $ADDR                      # Base Sepolia (after the bridge)
```

For mainnet, send real ETH on Base to the same address. Bridged from Ethereum, or withdrawn from an exchange to the **Base** network, not Ethereum mainnet.

## 3. Configure

```bash
cp .env.example .env
```

The defaults deploy to Base Sepolia with the deployer holding every role. Uncomment lines in `.env` to change that:

| Setting | What it controls | Default |
|---|---|---|
| `ADMIN` | grants and revokes the other roles; sets the base URI and royalties; withdraws mint proceeds | deployer |
| `SIGNER` | signs mint vouchers (the consumer's server key) | `ADMIN` |
| `MINTER` | mints Deeds | `ADMIN` |
| `METADATA_OPERATOR` | refreshes or freezes a Card's metadata | `ADMIN` |
| `COMMITTER` | posts Ledger commitments and roots | `ADMIN` |
| `CARD_NAME` / `CARD_SYMBOL` | token name and symbol | `argus-mint Card` / `CARD` |
| `DEED_NAME` / `DEED_SYMBOL` | soulbound deed name and symbol | `argus-mint Deed` / `DEED` |
| `ROYALTY_RECEIVER` / `ROYALTY_BPS` | ERC-2981 resale royalty | `ADMIN` / 0 |

Leave unused lines commented out: an empty value is not the same as unset.

**Testnet:** the defaults are fine.

**Mainnet:** split the roles.
- `ADMIN` should be a multisig (e.g. a Safe on Base), so one lost or leaked key can't take the contracts.
- `SIGNER` is a separate key held by the consumer's server. It is the key `argus-mint mint` reads from `$ARGUS_MINT_SIGNER_KEY`.
- The deployer then only pays gas and holds nothing afterwards.

## 4. Deploy

```bash
make deploy                       # Base Sepolia
make deploy NETWORK=base          # Base mainnet
```

Forge asks for the keystore password, deploys the three contracts, grants the roles, and verifies the source on Sourcify. It prints the addresses:

```
Card   0x…
Deed   0x…
Ledger 0x…
```

They are also saved under `contracts/broadcast/Deploy.s.sol/<chain id>/run-latest.json`.

### Verification

Sourcify is the default. Blockscout (`base-sepolia.blockscout.com`, `base.blockscout.com`) shows Sourcify-verified sources automatically. To verify somewhere else:

| Command | Verifies on | Key needed |
|---|---|---|
| `make deploy VERIFIER=blockscout` | Blockscout | none |
| `make deploy VERIFIER=etherscan` | Basescan | `ETHERSCAN_API_KEY` in `.env` (try a free key first) |
| `make deploy VERIFIER=none` | skip verification | — |

### RPC

`BASE_SEPOLIA_RPC_URL` / `BASE_RPC_URL` in `.env` default to the public endpoints. If one is rate-limiting or down, swap in another from `.env.example`, or a provider URL.

## 5. After deploying

1. Note the three addresses: the README's demo section and the consumer's config need them.
2. Check the roles landed where you meant, for example:
   ```bash
   cast call <CARD> "hasRole(bytes32,address)(bool)" $(cast keccak SIGNER_ROLE) <SIGNER> --rpc-url https://sepolia.base.org
   ```
3. Mint a test card, end to end. Each step that sends a transaction or signs asks for the keystore password (step 6 below avoids the prompts).
   ```bash
   make ipfs                                   # local Kubo node for the image and metadata
   export ARGUS_MINT_ACCOUNT=deployer          # sign and send as the keystore account

   # render through darkroom, pin, build metadata, sign a voucher (SIGNER_ROLE)
   argus-mint mint --network base-sepolia --card <CARD> --template templates/example-portrait-v1.yaml \
     --var role=ranger --var colour=emerald --var "pose=on guard" --seed 42 --asset demo-1 \
     --to <PLAYER> --name "Demo Ranger" \
     --darkroom http://127.0.0.1:8106 --ipfs-api http://127.0.0.1:5001 > demo-1.json

   argus-mint submit demo-1.json               # sends the voucher; prints the token id
   argus-mint freeze --card <CARD> --token-id 1 --uri <metadata_cid from demo-1.json>   # METADATA_ROLE
   ```
   `submit` checks first what the contract would refuse: a signer without the role, an asset already minted, an expired voucher, or the wrong chain. Those cost no gas. Any account may submit; in production that's the player's wallet.
4. Anchor a Merkle root. The leaves are 32-byte record hashes, one per line:
   ```bash
   argus-mint anchor leaves.txt --ledger <LEDGER> --epoch 1 --tree epoch-1.json   # COMMITTER_ROLE
   ```
   Keep the `--tree` file: proofs for individual records come from it.
5. On mainnet, once the multisig admin works, the deployer can be emptied. It holds no roles if `ADMIN` was set.
6. Unattended signing (servers, cron): put the keystore password in a file only that user can read, and point at it:
   ```bash
   export ARGUS_MINT_KEYSTORE_PASSWORD_FILE=/run/secrets/argus-mint-signer   # mode 600
   ```
   Or give the raw key in `$ARGUS_MINT_SIGNER_KEY`, if your secret store hands out environment variables. A key is never accepted as a command-line argument: it would end up in shell history and in `ps`.

Contracts are not upgradeable: a new version is a new deployment, with new addresses.

## 6. Pin on a VPS (optional)

A Kubo node at home sits behind NAT. Gateways reach it only through relays, slowly, and not at all while the machine is off. A small VPS with a public IP fixes both without opening any home port. It runs the same `compose.yaml`; minting reaches its RPC through an SSH tunnel.

**On the VPS** (Docker with the compose plugin installed):

```bash
scp compose.yaml <user>@<vps>:argus-ipfs/            # from this repo
ssh <user>@<vps>
cd argus-ipfs && docker compose up -d kubo
sudo ufw allow 4001/tcp && sudo ufw allow 4001/udp    # swarm: the only public port
# announce the public swarm port; behind Docker's NAT the node otherwise advertises a random UDP port
docker compose exec kubo ipfs config --json Addresses.AppendAnnounce \
  '["/ip4/<vps ip>/tcp/4001","/ip4/<vps ip>/udp/4001/quic-v1","/ip4/<vps ip>/udp/4001/quic-v1/webtransport"]'
docker compose restart kubo
```

If 5001 or 8080 is already taken on the VPS, move it in `argus-ipfs/.env`, e.g. `IPFS_GATEWAY_PUBLISH=127.0.0.1:8180`.

The RPC (5001) and gateway (8080) stay on the VPS's 127.0.0.1, as `compose.yaml` publishes them. Don't change that: the RPC can pin, unpin and read the node's keys. Docker-published ports bypass `ufw`, so the bind address is what protects them, not the firewall.

**From the minting machine**, open a tunnel and pin through it:

```bash
ssh -N -L 5002:127.0.0.1:5001 <user>@<vps>            # keep running (autossh or a systemd unit for servers)
argus-mint mint ... --ipfs-api http://127.0.0.1:5002
```

Port 5002 keeps it apart from a local node on 5001. With Tailscale or WireGuard, bind the RPC to the VPS's tunnel address instead (`IPFS_API_PUBLISH=100.x.y.z:5001`) and skip the SSH step.

**Moving content pinned elsewhere.** Export each CID as a CAR file, copy it over, and import it. Imported roots are pinned:

```bash
curl -sfX POST "http://127.0.0.1:5001/api/v0/dag/export?arg=<cid>" > <cid>.car   # where it is pinned now
scp <cid>.car <user>@<vps>:argus-ipfs/
ssh <user>@<vps> 'cd argus-ipfs && docker compose cp <cid>.car kubo:/tmp/ && docker compose exec kubo ipfs dag import /tmp/<cid>.car'
```

Each card needs about 1.7 MB (image plus metadata), so 1,000 cards take about 2 GB. Kubo uses 1–2 GB of RAM.

For mainnet, consider a pinning service as a second copy, so cards survive the VPS going down.
