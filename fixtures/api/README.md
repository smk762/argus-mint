# argus-mint API fixture

Recorded responses of `argus-mint serve` for argus-studio's and argus-assay's demo mode, so
they run without a backend. Regenerate with `uv run python scripts/api_fixture.py ART.png ...`.

- One draft per state: pending, approved, rejected, published (the published one has its
  `publish.json` result: pins, metadata, signed voucher).
- The template (v2), card-green frame and bio template are the repo's examples, marked approved
  for the fixture. Two drafts await catalogue review: template v3 (v2 with one more negative
  term) and the placeholder example-plain frame. One sample job renders v3 in the frame.
- Backends were stand-ins: art from files, canned bios, local (non-IPFS) CIDs, a throwaway owner
  wallet, Base Sepolia chain id. Nothing here was pinned or minted.
- `drafts/<id>/layout.json` is where each line of the card's text sits (`POST /frames/…/layout`), and
  `frames/<id>/<v>/fonts/` holds the fonts the frame names (SIL OFL), for the style editor's preview.
- Images are half-size, 256-colour previews to keep the fixture small, so their sha256 differs
  from `image_sha256` in the JSON, which refers to the full-size originals.
