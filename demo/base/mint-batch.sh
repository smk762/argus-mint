#!/usr/bin/env bash
# Base mainnet demo: render, pin (VPS Kubo via tunnel on :5002), sign, mint and freeze six cards.
# Run from the repo root after `make deploy NETWORK=base`. Each step's JSON lands in demo/base/.
# Skips a card whose freeze output already exists, so a failed run can be re-run.
# Historical: written for the one-shot `argus-mint mint`, which `draft` / `review` / `publish`
# have since replaced (every card image now needs moderator and owner approval). Kept as the
# record of how the demo was made; it no longer runs against the current CLI.
set -euo pipefail

CARD=0xCAa3573b5550870A5e15C47c650b8700D70a8fA7
TO=0xcb172f229574DacbC9f6C8D8f277FF5fDFF37669
DARKROOM=${DARKROOM:-http://127.0.0.1:8106}
IPFS_API=${IPFS_API:-http://127.0.0.1:5002}
OUT=demo/base
am() { uv run --quiet argus-mint "$@"; }
export ARGUS_MINT_ACCOUNT=${ARGUS_MINT_ACCOUNT:-deployer}

# n | name | gender | role | colour | pose | background | seed
CARDS=(
  "1|Azure Knight|male|knight|azure|on guard|castle|8453"
  "2|Emerald Wizard|female|wizard|emerald|casting a spell|library|8454"
  "3|Steel Cyborg|non-binary|cyborg|steel|repairing|workshop|8455"
  "4|Silver Huntress|female|elf|silver|hunting|forest|8456"
  "5|Crimson Orc|male|orc|crimson|fighting|dungeon|8457"
  "6|Golden Priest|female|priest|gold|praying|temple|8458"
)

curl -sf -X POST "$IPFS_API/api/v0/version" >/dev/null || { echo "no Kubo at $IPFS_API (open the tunnel)"; exit 1; }

for spec in "${CARDS[@]}"; do
  IFS='|' read -r n name gender role colour pose background seed <<<"$spec"
  [[ -s $OUT/freeze-$n.json ]] && { echo "card $n done, skipping"; continue; }
  echo "== card $n: $name" >&2
  am mint --network base --card "$CARD" --template templates/example-portrait-v1.yaml \
    --var "gender=$gender" --var "role=$role" --var "colour=$colour" --var "pose=$pose" \
    --var "background=$background" --seed "$seed" --asset "mainnet-$n" --name "$name" --to "$TO" \
    --darkroom "$DARKROOM" --ipfs-api "$IPFS_API" >"$OUT/mint-$n.json"
  am submit "$OUT/mint-$n.json" --network base >"$OUT/submit-$n.json"
  token=$(jq -r .token_id "$OUT/submit-$n.json")
  am freeze --network base --card "$CARD" --token-id "$token" \
    --uri "$(jq -r .metadata_cid "$OUT/mint-$n.json")" >"$OUT/freeze-$n.json"
  echo "   token $token: $(jq -r .token_url "$OUT/submit-$n.json")" >&2
done
