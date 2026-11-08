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

- **Faucets:** the Coinbase Developer Platform faucet and most RPC providers (Alchemy, QuickNode, …) hand out Base Sepolia ETH.
- **Bridge:** if you already hold Ethereum Sepolia ETH, bridge it to Base Sepolia.

Check the balance:

```bash
cast balance --ether --rpc-url https://sepolia.base.org $(cast wallet address --account deployer)
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
3. Mint a test card. The voucher must name this chain and the Card address:
   ```bash
   make ipfs
   export ARGUS_MINT_SIGNER_KEY=…        # the SIGNER's key; in a shell, not a file
   argus-mint mint --network base-sepolia --card <CARD> --template … --var … \
     --darkroom http://127.0.0.1:8106 --ipfs-api http://127.0.0.1:5001 …
   ```
4. On mainnet, once the multisig admin works, the deployer can be emptied. It holds no roles if `ADMIN` was set.

Contracts are not upgradeable: a new version is a new deployment, with new addresses.
