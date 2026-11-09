# argus-mint API fixture

Recorded responses of `argus-mint serve` for argus-studio's and argus-assay's demo mode, so
they run without a backend. Regenerate with `uv run python scripts/api_fixture.py ART.png ...`.

- One draft per state: pending, approved, rejected, published (the published one has its
  `publish.json` result: pins, metadata, signed voucher).
- The template, frame and bio template are the repo's examples, marked approved for the fixture.
- Backends were stand-ins: art from files, canned bios, local (non-IPFS) CIDs, a throwaway owner
  wallet, Base Sepolia chain id. Nothing here was pinned or minted.
- Images are half-size, 256-colour previews to keep the fixture small, so their sha256 differs
  from `image_sha256` in the JSON, which refers to the full-size originals.
