// argus-mint verifier: check argus-mint records without trusting the consumer's server.
//
//  - Merkle proofs (OpenZeppelin StandardMerkleTree, leaf encoding ["bytes32"]) against a
//    root read from the Ledger contract
//  - revealed secrets against their sha256 commitments
//  - drand quicknet beacons (BLS signature on G1, scheme bls-unchained-g1-rfc9380)
//
// No framework, no web3 library: Ledger reads are plain JSON-RPC (eth_call / eth_getLogs)
// over fetch. Hex strings are 0x-prefixed throughout.

import { bls12_381 } from "@noble/curves/bls12-381.js";
import { sha256 } from "@noble/hashes/sha2.js";
import { keccak_256 } from "@noble/hashes/sha3.js";
import { bytesToHex, concatBytes, hexToBytes } from "@noble/hashes/utils.js";

// --- hex helpers ---------------------------------------------------------------------------

/** 0x-hex string -> bytes. */
export function fromHex(hex) {
  if (typeof hex !== "string" || !/^0x([0-9a-fA-F]{2})*$/.test(hex)) throw new Error(`not 0x hex: ${hex}`);
  return hexToBytes(hex.slice(2));
}

/** bytes -> 0x-hex string. */
export function toHex(bytes) {
  return "0x" + bytesToHex(bytes);
}

function bytes32(hex) {
  const b = fromHex(hex);
  if (b.length !== 32) throw new Error(`expected 32 bytes, got ${b.length}`);
  return b;
}

function compareBytes(a, b) {
  for (let i = 0; i < Math.min(a.length, b.length); i++) if (a[i] !== b[i]) return a[i] - b[i];
  return a.length - b.length;
}

function equalBytes(a, b) {
  return a.length === b.length && compareBytes(a, b) === 0;
}

// --- Merkle --------------------------------------------------------------------------------

/** Leaf hash of a 32-byte value: keccak256(keccak256(abi.encode(bytes32))). */
export function merkleLeaf(valueHex) {
  return toHex(keccak_256(keccak_256(bytes32(valueHex))));
}

function hashPair(a, b) {
  return keccak_256(compareBytes(a, b) <= 0 ? concatBytes(a, b) : concatBytes(b, a));
}

/** True when `proof` shows `value` is a leaf of the tree with `root`. */
export function verifyMerkleProof(rootHex, valueHex, proofHex) {
  let node = fromHex(merkleLeaf(valueHex));
  for (const p of proofHex) node = hashPair(node, bytes32(p));
  return equalBytes(node, bytes32(rootHex));
}

// --- commit / reveal -----------------------------------------------------------------------

/** sha256(secret) — what the Ledger stores as a commitment. */
export function commitmentFor(secretHex) {
  return toHex(sha256(fromHex(secretHex)));
}

/** True when `secret` is the preimage of `commitment` (sha256). */
export function verifyReveal(commitmentHex, secretHex) {
  return equalBytes(bytes32(commitmentHex), sha256(fromHex(secretHex)));
}

// --- drand quicknet ------------------------------------------------------------------------

/** drand quicknet chain parameters (https://api.drand.sh/<chainHash>/info). */
export const QUICKNET = Object.freeze({
  chainHash: "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971",
  publicKey:
    "83cf0f2896adee7eb8b5f01fcad3912212c437e0073e911fb90022d3e760183c8c4b450b6a0a6c3ac6a5776a2d1064510d1fec758c921cc22b0e17e63aaf4bcb5ed66304de9cf809bd274ca73bab4af5a6e9c76a4bc09e76eae8991ef5ece45a",
  scheme: "bls-unchained-g1-rfc9380",
  genesisTime: 1692803367,
  period: 3,
});

/** Unix time (seconds) at which a quicknet round is published. */
export function drandRoundTime(round, chain = QUICKNET) {
  return chain.genesisTime + (round - 1) * chain.period;
}

/**
 * Verify a drand quicknet beacon. `beacon` is drand's JSON ({round, signature, randomness?},
 * hex without 0x as drand serves it, or with). Returns {ok, randomness}; randomness is
 * sha256(signature). If the beacon carries `randomness`, it must match too.
 */
export function verifyDrandBeacon(beacon, { publicKey = QUICKNET.publicKey } = {}) {
  const strip = (h) => (h.startsWith("0x") ? h.slice(2) : h);
  const round = BigInt(beacon.round);
  if (round < 1n || round >= 1n << 64n) return { ok: false, randomness: null };
  const sig = hexToBytes(strip(beacon.signature));
  const msg = new Uint8Array(8);
  new DataView(msg.buffer).setBigUint64(0, round, false);
  const randomness = bytesToHex(sha256(sig));
  let ok;
  try {
    const sigs = bls12_381.shortSignatures;
    ok = sigs.verify(sig, sigs.hash(sha256(msg)), hexToBytes(strip(publicKey)));
  } catch {
    ok = false; // malformed point encodings
  }
  if (beacon.randomness !== undefined && strip(beacon.randomness).toLowerCase() !== randomness) ok = false;
  return { ok, randomness: ok ? "0x" + randomness : null };
}

// --- Ledger reads (JSON-RPC) ---------------------------------------------------------------

const selector = (sig) => keccak_256(new TextEncoder().encode(sig)).slice(0, 4);
const SEL_ROOT = selector("root(uint64)");
const SEL_COMMITMENT = selector("commitment(uint64)");
const SEL_VERIFY = selector("verify(uint64,bytes32,bytes32[])");
const TOPIC_REVEALED = toHex(keccak_256(new TextEncoder().encode("Revealed(uint64,bytes)")));

function word(n) {
  const out = new Uint8Array(32);
  let v = BigInt(n);
  for (let i = 31; i >= 0 && v > 0n; i--, v >>= 8n) out[i] = Number(v & 0xffn);
  return out;
}

function wordAt(data, i) {
  return data.slice(i * 32, (i + 1) * 32);
}

function toBigInt(bytes) {
  return bytes.reduce((acc, b) => (acc << 8n) | BigInt(b), 0n);
}

function checkEpoch(epoch) {
  const e = BigInt(epoch);
  if (e < 0n || e >= 1n << 64n) throw new Error("epoch must be a uint64");
  return e;
}

async function rpc(url, method, params, fetchImpl) {
  const res = await fetchImpl(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  if (!res.ok) throw new Error(`RPC HTTP ${res.status}`);
  const body = await res.json();
  if (body.error) throw new Error(`RPC error: ${body.error.message ?? JSON.stringify(body.error)}`);
  return body.result;
}

async function call(url, to, data, block, fetchImpl) {
  return fromHex(await rpc(url, "eth_call", [{ to, data: toHex(data) }, block], fetchImpl));
}

/** Ledger.root(epoch) -> {root, count, anchoredAt}; root is 0x00…00 when nothing is anchored. */
export async function readRoot(rpcUrl, ledger, epoch, { fetch: f = fetch, block = "latest" } = {}) {
  const out = await call(rpcUrl, ledger, concatBytes(SEL_ROOT, word(checkEpoch(epoch))), block, f);
  return {
    root: toHex(wordAt(out, 0)),
    count: Number(toBigInt(wordAt(out, 1))),
    anchoredAt: Number(toBigInt(wordAt(out, 2))),
  };
}

/** Ledger.commitment(epoch) -> {hash, committedAt}. */
export async function readCommitment(rpcUrl, ledger, epoch, { fetch: f = fetch, block = "latest" } = {}) {
  const out = await call(rpcUrl, ledger, concatBytes(SEL_COMMITMENT, word(checkEpoch(epoch))), block, f);
  return { hash: toHex(wordAt(out, 0)), committedAt: Number(toBigInt(wordAt(out, 1))) };
}

/** The secret from the epoch's Revealed event, or null if it hasn't been revealed. */
export async function readRevealedSecret(rpcUrl, ledger, epoch, { fetch: f = fetch, fromBlock = "0x0" } = {}) {
  const logs = await rpc(
    rpcUrl,
    "eth_getLogs",
    [{ address: ledger, fromBlock, toBlock: "latest", topics: [TOPIC_REVEALED, toHex(word(checkEpoch(epoch)))] }],
    f,
  );
  if (!logs.length) return null;
  const data = fromHex(logs[0].data); // abi.encode(bytes): offset, length, bytes
  const offset = Number(toBigInt(wordAt(data, 0)));
  const len = Number(toBigInt(data.slice(offset, offset + 32)));
  return toHex(data.slice(offset + 32, offset + 32 + len));
}

/** Ask the Ledger contract itself to verify (an on-chain cross-check of verifyMerkleProof). */
export async function ledgerVerify(rpcUrl, ledger, epoch, valueHex, proofHex, { fetch: f = fetch } = {}) {
  const data = concatBytes(
    SEL_VERIFY,
    word(checkEpoch(epoch)),
    bytes32(valueHex),
    word(96), // offset of the proof array
    word(proofHex.length),
    ...proofHex.map(bytes32),
  );
  const out = await call(rpcUrl, ledger, data, "latest", f);
  return toBigInt(wordAt(out, 0)) === 1n;
}

/**
 * Check that a record is in the tree anchored for `epoch`: reads the root from the Ledger
 * and verifies the proof locally. Returns {ok, root, count, anchoredAt}.
 */
export async function verifyAnchoredRecord(rpcUrl, ledger, epoch, valueHex, proofHex, opts = {}) {
  const r = await readRoot(rpcUrl, ledger, epoch, opts);
  const anchored = !/^0x0+$/.test(r.root);
  return { ok: anchored && verifyMerkleProof(r.root, valueHex, proofHex), ...r };
}

/**
 * Check an epoch's revealed secret: read the commitment and the Revealed event and confirm
 * sha256(secret) matches. Returns {ok, secret, commitment, committedAt}; pass `epochStart`
 * (unix seconds) to also require the commitment to predate the epoch.
 */
export async function verifyEpochSecret(rpcUrl, ledger, epoch, { epochStart, ...opts } = {}) {
  const c = await readCommitment(rpcUrl, ledger, epoch, opts);
  const secret = await readRevealedSecret(rpcUrl, ledger, epoch, opts);
  let ok = secret !== null && !/^0x0+$/.test(c.hash) && verifyReveal(c.hash, secret);
  if (epochStart !== undefined && !(c.committedAt > 0 && c.committedAt < epochStart)) ok = false;
  return { ok, secret, commitment: c.hash, committedAt: c.committedAt };
}
