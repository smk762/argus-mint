// Merkle fixtures from the reference implementation (@openzeppelin/merkle-tree).
// The Python merkle module and the Solidity Ledger.verify must agree with these.
// Usage: node merkle.mjs <out.json>
import { createHash } from "node:crypto";
import { writeFileSync } from "node:fs";
import { StandardMerkleTree } from "@openzeppelin/merkle-tree";

// Deterministic 32-byte values, the way a consumer supplies them (sha256 of a record).
const value = (n, i) => "0x" + createHash("sha256").update(`argus-mint/merkle/${n}/${i}`).digest("hex");

const SIZES = [1, 2, 3, 7, 1000];
const FULL_DUMP_MAX = 16; // full tree dump + every proof for small trees; samples for big ones

const cases = SIZES.map((n) => {
  const values = Array.from({ length: n }, (_, i) => value(n, i));
  const tree = StandardMerkleTree.of(values.map((v) => [v]), ["bytes32"]);
  const indices = n <= FULL_DUMP_MAX ? values.map((_, i) => i) : [0, 1, 2, Math.floor(n / 2), n - 2, n - 1];
  const out = {
    n,
    values,
    root: tree.root,
    proofs: Object.fromEntries(indices.map((i) => [i, tree.getProof(i)])),
  };
  if (n <= FULL_DUMP_MAX) out.dump = tree.dump();
  return out;
});

const doc = {
  generator: "@openzeppelin/merkle-tree@1.0.8 StandardMerkleTree, leafEncoding [\"bytes32\"]",
  value_rule: "sha256(utf8('argus-mint/merkle/<n>/<i>'))",
  cases,
};
writeFileSync(process.argv[2], JSON.stringify(doc, null, 1) + "\n");
console.log(`wrote ${cases.length} cases to ${process.argv[2]}`);
