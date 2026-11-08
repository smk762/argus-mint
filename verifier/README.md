# argus-mint verifier

A small ES module (browser or Node ≥ 20) that lets anyone check argus-mint records without trusting the consumer's server. Its only dependencies are `@noble/curves` and `@noble/hashes`. Ledger reads are plain JSON-RPC over `fetch`.

```js
import {
  verifyAnchoredRecord, verifyEpochSecret, verifyDrandBeacon, verifyMerkleProof,
} from "argus-mint-verifier";

// Is this record in the tree the consumer anchored for epoch 20400?
const { ok, root, anchoredAt } = await verifyAnchoredRecord(rpcUrl, ledgerAddress, 20400, leafValue, proof);

// Was the epoch's secret committed before the epoch began, and does the reveal match?
const s = await verifyEpochSecret(rpcUrl, ledgerAddress, 20400, { epochStart: 20400 * 86400 });

// Is this drand quicknet beacon real? (BLS on G1, bls-unchained-g1-rfc9380)
const beacon = await (await fetch(`https://api.drand.sh/${QUICKNET.chainHash}/public/1000000`)).json();
const { ok: real, randomness } = verifyDrandBeacon(beacon);
```

| Function | Checks |
| --- | --- |
| `verifyMerkleProof(root, value, proof)` | OpenZeppelin `StandardMerkleTree` (`["bytes32"]`) proof, offline |
| `readRoot`, `verifyAnchoredRecord` | the root anchored on the Ledger for an epoch, then the proof against it |
| `ledgerVerify` | asks the Ledger contract to verify (an on-chain cross-check) |
| `readCommitment`, `readRevealedSecret`, `verifyEpochSecret` | sha256 commitment, the `Revealed` event, and their match; optionally that the commitment predates the epoch |
| `verifyDrandBeacon`, `drandRoundTime`, `QUICKNET` | a quicknet round's BLS signature, and randomness = sha256(signature) |

Tests (`npm test`) run against committed fixtures: proofs from `@openzeppelin/merkle-tree` and from the Python package, real quicknet rounds from api.drand.sh, and JSON-RPC responses recorded from a Ledger on anvil (`make fixtures-rpc`).
