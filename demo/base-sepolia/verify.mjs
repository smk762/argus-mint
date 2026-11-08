// Check the demo's anchored records against the live Ledger on Base Sepolia.
//   node demo/base-sepolia/verify.mjs [rpc-url]
// For each record: hash it (sha256), read epoch 1's root from the Ledger, check the proof.
// A changed record must fail.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { verifyAnchoredRecord } from "../../verifier/src/index.js";

const rpc = process.argv[2] ?? "https://sepolia.base.org";
const demo = JSON.parse(readFileSync(new URL("./proofs-1.json", import.meta.url)));
const sha256 = (s) => "0x" + createHash("sha256").update(s).digest("hex");

let failures = 0;
for (const { record, proof } of demo.records) {
  const r = await verifyAnchoredRecord(rpc, demo.ledger, demo.epoch, sha256(record), proof);
  console.log(`${r.ok ? "ok  " : "FAIL"}  ${record}`);
  failures += !r.ok;
}
const { record, proof } = demo.records[1];
const forged = await verifyAnchoredRecord(rpc, demo.ledger, demo.epoch, sha256(record + "!"), proof);
console.log(`${forged.ok ? "FAIL" : "ok  "}  a changed record is rejected`);
failures += forged.ok;
process.exit(failures ? 1 : 0);
