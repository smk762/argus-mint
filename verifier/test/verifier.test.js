import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  QUICKNET,
  commitmentFor,
  drandRoundTime,
  ledgerVerify,
  merkleLeaf,
  readCommitment,
  readRevealedSecret,
  readRoot,
  verifyAnchoredRecord,
  verifyDrandBeacon,
  verifyEpochSecret,
  verifyMerkleProof,
  verifyReveal,
} from "../src/index.js";

const fixture = (name) => JSON.parse(readFileSync(new URL(`../../tests/fixtures/${name}`, import.meta.url)));
const merkle = fixture("merkle.json"); // from @openzeppelin/merkle-tree
const pyProofs = fixture("proofs.json"); // from argus_mint.merkle
const drand = fixture("drand_quicknet.json"); // from api.drand.sh
const rpc = fixture("ledger_rpc.json"); // real responses from a Ledger on anvil

// --- Merkle --------------------------------------------------------------------------------

test("verifies every OpenZeppelin JS proof", () => {
  for (const c of merkle.cases) {
    for (const [i, proof] of Object.entries(c.proofs)) {
      assert.ok(verifyMerkleProof(c.root, c.values[i], proof), `n=${c.n} i=${i}`);
    }
  }
});

test("leaf hashes match the OpenZeppelin tree dump", () => {
  const c = merkle.cases.find((x) => x.n === 7);
  for (const { value, treeIndex } of c.dump.values) assert.equal(merkleLeaf(value[0]), c.dump.tree[treeIndex]);
});

test("verifies every Python-built proof", () => {
  for (const c of pyProofs.cases) {
    c.values.forEach((v, i) => assert.ok(verifyMerkleProof(c.root, v, c.proofs[i])));
  }
});

test("rejects wrong values, roots and tampered proofs", () => {
  const c = pyProofs.cases[2];
  assert.ok(!verifyMerkleProof(c.root, c.values[1], c.proofs[0]));
  assert.ok(!verifyMerkleProof("0x" + "00".repeat(32), c.values[0], c.proofs[0]));
  const tampered = [...c.proofs[0]];
  tampered[2] = "0x" + "ab".repeat(32);
  assert.ok(!verifyMerkleProof(c.root, c.values[0], tampered));
  assert.ok(!verifyMerkleProof(c.root, c.values[0], c.proofs[0].slice(1)));
  assert.throws(() => verifyMerkleProof(c.root, "0x1234", []));
});

// --- commit / reveal -----------------------------------------------------------------------

test("reveal check is sha256", () => {
  const secret = rpc.secret;
  const c = commitmentFor(secret);
  assert.ok(verifyReveal(c, secret));
  assert.ok(!verifyReveal(c, "0x00"));
  assert.equal(c, "0x630dcd2966c4336691125448bbb25b4ff412a49c732db2c8abc1b8581bd710dd");
});

// --- drand ---------------------------------------------------------------------------------

test("quicknet constants match the chain info", () => {
  assert.equal(QUICKNET.publicKey, drand.info.public_key);
  assert.equal(QUICKNET.chainHash, drand.info.hash);
  assert.equal(QUICKNET.genesisTime, drand.info.genesis_time);
  assert.equal(QUICKNET.period, drand.info.period);
  assert.equal(QUICKNET.scheme, drand.info.schemeID);
  assert.equal(drandRoundTime(1), drand.info.genesis_time);
});

test("verifies real quicknet beacons", () => {
  for (const beacon of drand.rounds) {
    const r = verifyDrandBeacon(beacon);
    assert.ok(r.ok, `round ${beacon.round}`);
    assert.equal(r.randomness, "0x" + beacon.randomness);
  }
});

test("rejects forged or mismatched beacons", () => {
  const [, a, b] = drand.rounds;
  assert.ok(!verifyDrandBeacon({ round: a.round + 1, signature: a.signature }).ok);
  assert.ok(!verifyDrandBeacon({ round: a.round, signature: b.signature }).ok);
  assert.ok(!verifyDrandBeacon({ ...a, randomness: b.randomness }).ok);
  assert.ok(!verifyDrandBeacon({ round: a.round, signature: "00".repeat(48) }).ok);
  assert.ok(!verifyDrandBeacon({ round: 0, signature: a.signature }).ok);
  const flipped = a.signature.slice(0, -2) + (a.signature.endsWith("00") ? "01" : "00");
  assert.ok(!verifyDrandBeacon({ round: a.round, signature: flipped }).ok);
});

// --- Ledger reads over recorded JSON-RPC ---------------------------------------------------

const norm = (x) => JSON.stringify(x, (_, v) => (typeof v === "string" ? v.toLowerCase() : v));

function recordedFetch() {
  return async (_url, init) => {
    const { method, params } = JSON.parse(init.body);
    const hit = rpc.calls.find((c) => c.method === method && norm(c.params) === norm(params));
    if (!hit) throw new Error(`no recorded response for ${method} ${JSON.stringify(params)}`);
    return { ok: true, json: async () => ({ jsonrpc: "2.0", id: 1, result: hit.result }) };
  };
}

const opts = { fetch: recordedFetch() };
const URL_ = "http://anvil.invalid";
const tree = pyProofs.cases[1];

test("reads the anchored root", async () => {
  const r = await readRoot(URL_, rpc.ledger, 20400, opts);
  assert.equal(r.root, tree.root);
  assert.equal(r.count, 7);
  assert.ok(r.anchoredAt > 1_600_000_000);
  const none = await readRoot(URL_, rpc.ledger, 20402, opts);
  assert.equal(none.root, "0x" + "00".repeat(32));
  assert.equal(none.count, 0);
});

test("verifies a record against the on-chain root", async () => {
  const ok = await verifyAnchoredRecord(URL_, rpc.ledger, 20400, tree.values[3], tree.proofs[3], opts);
  assert.ok(ok.ok);
  const bad = await verifyAnchoredRecord(URL_, rpc.ledger, 20400, tree.values[4], tree.proofs[3], opts);
  assert.ok(!bad.ok);
  const unanchored = await verifyAnchoredRecord(URL_, rpc.ledger, 20402, tree.values[3], tree.proofs[3], opts);
  assert.ok(!unanchored.ok);
});

test("the contract's own verify agrees", async () => {
  assert.ok(await ledgerVerify(URL_, rpc.ledger, 20400, tree.values[0], tree.proofs[0], opts));
  assert.ok(await ledgerVerify(URL_, rpc.ledger, 20400, tree.values[3], tree.proofs[3], opts));
});

test("reads commitment and revealed secret", async () => {
  const c = await readCommitment(URL_, rpc.ledger, 20400, opts);
  assert.equal(c.hash, commitmentFor(rpc.secret));
  assert.equal(await readRevealedSecret(URL_, rpc.ledger, 20400, opts), rpc.secret);
  assert.equal(await readRevealedSecret(URL_, rpc.ledger, 20401, opts), null);
});

test("verifies an epoch's secret end to end", async () => {
  const r = await verifyEpochSecret(URL_, rpc.ledger, 20400, opts);
  assert.ok(r.ok);
  assert.equal(r.secret, rpc.secret);
  // the commitment must predate the epoch when epochStart is given
  assert.ok((await verifyEpochSecret(URL_, rpc.ledger, 20400, { ...opts, epochStart: r.committedAt + 1 })).ok);
  assert.ok(!(await verifyEpochSecret(URL_, rpc.ledger, 20400, { ...opts, epochStart: r.committedAt })).ok);
  // committed but not yet revealed
  assert.ok(!(await verifyEpochSecret(URL_, rpc.ledger, 20401, opts)).ok);
});

test("surfaces RPC errors", async () => {
  const failing = async () => ({ ok: true, json: async () => ({ error: { message: "boom" } }) });
  await assert.rejects(readRoot(URL_, rpc.ledger, 1, { fetch: failing }), /boom/);
  await assert.rejects(readRoot(URL_, rpc.ledger, -1, opts), /uint64/);
});
