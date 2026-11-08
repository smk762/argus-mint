# Verifying the demo contracts on Basescan (no API key)

The contracts are already verified on Sourcify, which Blockscout shows, but Basescan keeps its own list. Its web form needs no API key: upload one standard JSON input per contract.

| Contract | Address | Upload | Constructor arguments |
|---|---|---|---|
| Card | `0xCAa3573b5550870A5e15C47c650b8700D70a8fA7` | `Card.standard-input.json` | `Card.args` |
| Deed | `0xe44D67bdFd3416F97206fdE7aeE55F9008EB5D7c` | `Deed.standard-input.json` | `Deed.args` |
| Ledger | `0x4b41B687C694bd8bf3778871752637445c2Aa254` | `Ledger.standard-input.json` | `Ledger.args` |

For each contract:

1. Open `https://sepolia.basescan.org/verifyContract?a=<address>`.
2. Set **Compiler Type** to *Solidity (Standard-Json-Input)*, **Compiler Version** to `v0.8.38+commit.1835d247`, and **License** to MIT. Continue.
3. Upload the contract's `*.standard-input.json`.
4. Paste the contents of its `*.args` file into **Constructor Arguments ABI-encoded**, without the leading `0x`.
5. Submit. Basescan compiles the source and compares the result with the deployed bytecode.

The constructor arguments were checked against the deploy transactions, and the inputs come from `forge verify-contract --show-standard-json-input` (solc 0.8.38, optimizer 200 runs, `evm_version` cancun).

With an Etherscan API key (a free one may be enough), the same is one command per contract from `contracts/`:

```bash
forge verify-contract <address> src/Card.sol:Card --chain 84532 --verifier etherscan \
  --constructor-args $(cat ../demo/base-sepolia/basescan/Card.args) --watch
```
