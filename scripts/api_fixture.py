"""Record a demo fixture of the HTTP API for argus-studio's and argus-assay's demo mode.

Runs the real server in-process with stand-in backends (art from files, canned bios, local
pins, a throwaway owner wallet), walks drafts through each state, and writes every response
a UI reads, plus the images, under fixtures/api/:

    health.json templates.json frames.json bios.json drafts.json
    drafts/<id>.json drafts/<id>/image.png drafts/<id>/art.png frames/<id>/<version>/overlay.png
    samples.json samples/<id>.json samples/<id>/<n>-art.png samples/<id>/<n>-framed.png

    uv run python scripts/api_fixture.py ART.png [ART.png ...]

Art is scaled to 1152x896 first, the shape template v2 renders.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yaml
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from PIL import Image

from argus_mint.pin import LocalPinner
from argus_mint.render import GenResult
from argus_mint.server import Settings, create_app

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "fixtures" / "api"
TOKEN = "fixture-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CARD = "0xCAa3573b5550870A5e15C47c650b8700D70a8fA7"
SIGNER = Account.from_key("0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d")  # anvil #1

BIOS = [
    "{name} hums sonnets to the spines of forgotten books, and the library, charmed, rearranges itself to listen.",
    "{name} mends the hearts of broken engines with patient hands, and every gear purrs a little warmer afterwards.",
    "{name} once argued with a thunderstorm for an entire afternoon, and the storm, exhausted, apologised.",
    "{name} keeps a lantern lit for travellers who have not yet set out, just in case they lose their way.",
]
CARDS = [  # name, variables, display text, what happens to it
    ("Emerald Wizard", {"gender": "female", "role": "wizard", "colour": "emerald", "pose": "casting a spell",
                        "background": "library"}, {"class": "Wizard · Emerald", "stats": "No. 1"}, "published"),
    ("Steel Tinker", {"gender": "non-binary", "role": "cyborg", "colour": "steel", "pose": "repairing",
                      "background": "workshop"}, {"class": "Cyborg · Steel", "stats": "No. 2"}, "pending"),
    ("Azure Stormcaller", {"gender": "male", "role": "wizard", "colour": "azure", "pose": "casting a spell",
                           "background": "temple"}, {"class": "Wizard · Azure", "stats": "No. 3"}, "approved"),
    ("Lantern Keeper", {"gender": "female", "role": "priest", "colour": "gold", "pose": "praying",
                        "background": "temple"}, {"class": "Priest · Gold", "stats": "No. 4"}, "rejected"),
]  # fmt: skip


README = """# argus-mint API fixture

Recorded responses of `argus-mint serve` for argus-studio's and argus-assay's demo mode, so
they run without a backend. Regenerate with `uv run python scripts/api_fixture.py ART.png ...`.

- One draft per state: pending, approved, rejected, published (the published one has its
  `publish.json` result: pins, metadata, signed voucher).
- The template (v2), card-green frame and bio template are the repo's examples, marked approved
  for the fixture. Two drafts await catalogue review: template v3 (v2 with one more negative
  term) and the placeholder example-plain frame. One sample job renders v3 in the frame.
- Backends were stand-ins: art from files, canned bios, local (non-IPFS) CIDs, a throwaway owner
  wallet, Base Sepolia chain id. Nothing here was pinned or minted.
- Images are half-size, 256-colour previews to keep the fixture small, so their sha256 differs
  from `image_sha256` in the JSON, which refers to the full-size originals.
"""


def landscape(path: Path) -> bytes:
    im = Image.open(path).convert("RGB")
    w, h = im.size
    crop_h = round(w * 896 / 1152)
    top = max(0, (h - crop_h) // 2)
    im = im.crop((0, top, w, top + crop_h)).resize((1152, 896), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def preview(png: bytes) -> bytes:
    """Half size, 256 colours: small enough to commit. Hashes in the JSON refer to the originals."""
    im = Image.open(io.BytesIO(png))
    im = im.resize((im.width // 2, im.height // 2), Image.Resampling.LANCZOS)
    im = im.quantize(256, method=Image.Quantize.FASTOCTREE) if im.mode == "RGBA" else im.quantize(256)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def approved(src: Path, dst: Path) -> None:
    """Copy a reviewed file, marked approved for the fixture (the real one may still be a draft)."""
    doc = yaml.safe_load(src.read_text())
    doc |= {"status": "approved", "approved_by": "fixture", "approved_at": "2026-11-09"}
    dst.write_text(yaml.safe_dump(doc, sort_keys=False))


def main(arts: list[Path]) -> None:
    images = [landscape(p) for p in arts]
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for d in ("templates", "frames", "bios"):
            (work / d).mkdir()
        approved(ROOT / "templates" / "example-portrait-v2.yaml", work / "templates" / "t.yaml")
        approved(ROOT / "frames" / "card-green-v1.yaml", work / "frames" / "card-green-v1.yaml")
        shutil.copy(ROOT / "frames" / "card-green-v1.png", work / "frames")
        # drafts awaiting review, for the catalogue pages: the placeholder frame, and a v3 of the template
        shutil.copy(ROOT / "frames" / "example-plain-v1.yaml", work / "frames")
        shutil.copy(ROOT / "frames" / "example-plain-v1.png", work / "frames")
        v3 = yaml.safe_load((ROOT / "templates" / "example-portrait-v2.yaml").read_text())
        v3 |= {"version": 3, "status": "draft", "negative": v3["negative"] + ", blurry"}
        (work / "templates" / "t3.yaml").write_text(yaml.safe_dump(v3, sort_keys=False))
        shutil.copytree(ROOT / "frames" / "fonts", work / "frames" / "fonts")
        approved(ROOT / "bios" / "example-bio-v1.yaml", work / "bios" / "b.yaml")

        state = {"n": 0}

        class Darkroom:
            def generate(self, req):
                return GenResult.ok(images[state["n"] % len(images)], ref=f"fixture-{state['n']}")

            def check_text(self, text):
                return True, ""

        class Writer:
            calls = 0

            def write(self, **kw):
                Writer.calls += 1
                return BIOS[(state["n"] + Writer.calls) % len(BIOS)]

        cfg = Settings(
            drafts_dir=work / "drafts", samples_dir=work / "samples", templates_dir=work / "templates", frames_dir=work / "frames",
            bios_dir=work / "bios", darkroom_url="http://darkroom:8106", writer_url="http://writer:9150",
            ipfs_api="http://kubo:5001", network="base-sepolia", card=CARD, moderator_token=TOKEN,
            siwe_domain="localhost:3108",
        )  # fmt: skip
        app = create_app(
            cfg, darkroom=Darkroom(), writer=Writer(), pinner=LocalPinner(), signer=SIGNER, probe_fn=lambda u: True
        )
        c = TestClient(app)
        owner = Account.create()
        for i, (name, values, texts, fate) in enumerate(CARDS):
            state["n"] = i
            r = c.post(
                "/drafts",
                headers=AUTH,
                json={
                    "template": {"id": "example-portrait", "version": 2}, "variables": values, "seed": 8453 + i,
                    "asset": f"fixture-{i + 1}", "to": owner.address, "name": name,
                    "frame": {"id": "card-green", "version": 1}, "bio": {"id": "example-bio", "version": 1},
                    "texts": texts,
                },
            )  # fmt: skip
            r.raise_for_status()
            draft_id = r.json()["id"]
            if fate == "pending":
                continue
            approve = fate != "rejected"
            note = "" if approve else "The halo reads as a second face at card size."
            moderator = {"role": "moderator", "approve": approve, "by": "moderator-1", "note": note}
            c.post(f"/drafts/{draft_id}/review", headers=AUTH, json=moderator).raise_for_status()
            if fate == "rejected":
                continue
            ch = c.post(f"/drafts/{draft_id}/challenge", json={"approve": True}).json()
            sig = owner.sign_message(encode_defunct(text=ch["message"])).signature.hex()
            owner_review = {"role": "owner", "approve": True, "message": ch["message"], "signature": sig}
            c.post(f"/drafts/{draft_id}/review", json=owner_review).raise_for_status()
            if fate == "published":
                c.post(f"/drafts/{draft_id}/publish", headers=AUTH, json={"deadline": 1900000000}).raise_for_status()

        state["n"] = 0
        sample = c.post(
            "/samples",
            headers=AUTH,
            json={
                "template": {"id": "example-portrait", "version": 3}, "frame": {"id": "card-green", "version": 1},
                "bio": {"id": "example-bio", "version": 1}, "count": 2, "seed": 3, "name": "Sample",
                "texts": {"class": "Sample class", "stats": "No. 0"},
            },
        )  # fmt: skip
        sample.raise_for_status()
        job_id = sample.json()["id"]
        for _ in range(500):
            if c.get(f"/samples/{job_id}").json()["status"] in ("done", "failed"):
                break
            time.sleep(0.02)

        if OUT.exists():
            shutil.rmtree(OUT)
        (OUT / "drafts").mkdir(parents=True)

        def dump(rel: str, path: str) -> object:
            data = c.get(path).json()
            (OUT / rel).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            return data

        dump("health.json", "/health")
        dump("templates.json", "/templates")
        frames = dump("frames.json", "/frames")
        dump("bios.json", "/bios")
        listing = dump("drafts.json", "/drafts")
        for item in listing["items"]:  # type: ignore[index]
            dump(f"drafts/{item['id']}.json", f"/drafts/{item['id']}")
            (OUT / "drafts" / item["id"]).mkdir()
            for which in ("image", "art"):
                png = preview(c.get(f"/drafts/{item['id']}/{which}").content)
                (OUT / "drafts" / item["id"] / f"{which}.png").write_bytes(png)
        jobs = dump("samples.json", "/samples")
        (OUT / "samples").mkdir()
        for job in jobs["items"]:  # type: ignore[index]
            dump(f"samples/{job['id']}.json", f"/samples/{job['id']}")
            (OUT / "samples" / job["id"]).mkdir()
            for item in job["items"]:
                for which in ("art", "framed"):
                    if item[which]:
                        png = preview(c.get(f"/samples/{job['id']}/{item['n']}/{which}").content)
                        (OUT / "samples" / job["id"] / f"{item['n']}-{which}.png").write_bytes(png)
        for f in frames:  # type: ignore[union-attr]
            fid, ver = f["frame"]["id"], f["frame"]["version"]
            (OUT / "frames" / fid / str(ver)).mkdir(parents=True)
            (OUT / "frames" / fid / str(ver) / "overlay.png").write_bytes(c.get(f"/frames/{fid}/{ver}/overlay").content)
    (OUT / "README.md").write_text(README)
    print(OUT)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main([Path(a) for a in sys.argv[1:]])
