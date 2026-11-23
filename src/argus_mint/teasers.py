"""Composing a teaser: an approved background, the identity painted in (or its frame over it), the quote.

A teaser draft is made from teaser ``T<n>`` in the project's current teaser links (the newest
approved version), and only from reviewed parts (hard rule 2): the links, project, line,
identity, scene and frame must be approved, and so must the chosen background. Nothing is made
while the project has a cross-file problem, a spoiler rule matches any linked text, or a linked
text lacks a fresh passing SingGuard verdict (stored in its file).

- **Embodied** identity: its ``prompt`` (with its ``lora`` once pinned, plus the project's
  ``style_lora``: darkroom takes at most :data:`MAX_LORAS`) is inpainted into the scene's
  ``character_area`` on the background, through darkroom's image-to-image with a mask (SDXL).
  The seed is derived from the project, the teaser and the background, and a safety rejection
  is retried with the next one (hard rules 3 and 7).
- **Viewpoint** identity: the background is the art; the identity's frame goes over it.

Then the frame, the line's ``shown`` text in its ``quote`` slot, the project's attribution in
``attribution`` (when set) and :data:`GENERATED_LABEL` in ``label`` (when ``generated_label``).
The finished PNG is baked with no ancillary chunks (:func:`bake_png`), so it is byte-stable:
stripping metadata (argus-herald does) leaves its sha256 unchanged.

The draft is then reviewed like a card: the moderator, then the owner by SIWE.
"""

from __future__ import annotations

import hashlib
import io
import struct
import zlib
from datetime import UTC, datetime
from typing import Any

from argus_cortex.hashing import sha256_hex

import argus_mint
from argus_mint._codec import hex0x
from argus_mint.backgrounds import Background
from argus_mint.cards import CardDraft, TeaserBackground, TeaserFile, TeaserRecord
from argus_mint.frames import FrameError, LoadedFrame, compose
from argus_mint.projects import (
    Area,
    Identity,
    Line,
    Project,
    ProjectBundle,
    Scene,
    file_strings,
    fresh_verdicts,
    problems,
)
from argus_mint.render import InitImage, Inpainter, render_requests
from argus_mint.spoilers import SpoilerList
from argus_mint.templates import SEED_MAX, InitImageRef, LoraRef, RenderRequest
from argus_mint.workflow import Made

__all__ = [
    "GENERATED_LABEL",
    "INPAINT_DENOISE",
    "MAX_LORAS",
    "TeaserError",
    "TeaserNotFound",
    "bake_png",
    "inpaint_request",
    "make_teaser",
    "mask_png",
    "png_chunks",
    "teaser_loras",
    "teaser_seed",
    "teaser_texts",
]

MAX_LORAS = 2  # darkroom renders at most two: one character LoRA and one style LoRA
INPAINT_DENOISE = 1.0  # the character area is painted afresh; every pixel outside the mask stays the background's
GENERATED_LABEL = "generated image"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_KEPT_CHUNKS = (b"IHDR", b"PLTE", b"IDAT", b"IEND")  # the critical chunks; everything else is dropped


class TeaserError(ValueError):
    """A teaser can't be made as asked: a part isn't approved, a spoiler rule holds it, a slot is missing."""


class TeaserNotFound(TeaserError):
    """No such teaser in the project's current teaser links."""


# -- the PNG ----------------------------------------------------------------------------------------


def png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Every chunk of a PNG, as (type, data). :class:`ValueError` for a file that isn't one."""
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG")
    out, pos = [], len(_PNG_SIGNATURE)
    while pos < len(data):
        if pos + 8 > len(data):
            raise ValueError("a PNG chunk is cut short")
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        if len(body) != length or pos + 12 + length > len(data):
            raise ValueError("a PNG chunk is cut short")
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        if zlib.crc32(kind + body) != crc:
            raise ValueError(f"PNG chunk {kind!r} fails its CRC")
        out.append((kind, body))
        pos += 12 + length
        if kind == b"IEND":
            break
    if not out or out[0][0] != b"IHDR" or out[-1][0] != b"IEND":
        raise ValueError("a PNG starts with IHDR and ends with IEND")
    return out


def bake_png(image: bytes) -> bytes:
    """``image`` as an opaque RGB PNG with only its critical chunks (IHDR, IDAT, IEND).

    No text, time, colour-profile or physical-size chunk survives, so nothing in it can differ
    between two bakes of the same pixels, and stripping metadata later changes no byte. The
    pixels are flattened onto black (a full-bleed teaser is opaque everywhere anyway).
    Deterministic for a given Pillow and zlib.
    """
    from PIL import Image

    with Image.open(io.BytesIO(image)) as im:
        im.load()
        rgba = im.convert("RGBA")
    flat = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
    flat.alpha_composite(rgba)
    out = io.BytesIO()
    flat.convert("RGB").save(out, format="PNG", optimize=False, compress_level=6)
    return _strip(out.getvalue())


def _strip(png: bytes) -> bytes:
    parts = [_PNG_SIGNATURE]
    for kind, body in png_chunks(png):
        if kind in _KEPT_CHUNKS:
            parts.append(struct.pack(">I4s", len(body), kind) + body + struct.pack(">I", zlib.crc32(kind + body)))
    return b"".join(parts)


def mask_png(width: int, height: int, area: Area) -> bytes:
    """The inpainting mask: black (kept) everywhere, white (repainted) over ``area``. A 1-bit-clean
    greyscale PNG with only its critical chunks, the same bytes every time."""
    from PIL import Image, ImageDraw

    if area.x + area.width > width or area.y + area.height > height:
        raise TeaserError(f"the character area falls outside the {width}x{height} background")
    im = Image.new("L", (width, height), 0)
    ImageDraw.Draw(im).rectangle((area.x, area.y, area.x + area.width - 1, area.y + area.height - 1), fill=255)
    out = io.BytesIO()
    im.save(out, format="PNG", optimize=False, compress_level=6)
    return _strip(out.getvalue())


# -- the requests -----------------------------------------------------------------------------------


def teaser_seed(project: str, number: str, background_sha256: str) -> int:
    """The seed asked for a teaser's figure: the same for the same teaser on the same background."""
    digest = hashlib.sha256(f"argus-mint/teaser/{project}/{number}/{background_sha256}".encode()).digest()
    return int.from_bytes(digest[:4], "big") & SEED_MAX


def teaser_loras(project: Project, identity: Identity) -> tuple[LoraRef, ...]:
    """The identity's LoRA (once pinned) then the project's style LoRA. Never more than :data:`MAX_LORAS`."""
    loras = tuple(x for x in (identity.lora, project.style_lora) if x is not None)
    if len(loras) > MAX_LORAS:  # pragma: no cover - two optional fields; kept as the rule's guard
        raise TeaserError(f"a render takes at most {MAX_LORAS} LoRAs, not {len(loras)}")
    if len({x.name for x in loras}) != len(loras):
        raise TeaserError(f"the identity and the project name the same LoRA ({loras[0].name})")
    return loras


def inpaint_request(project: Project, identity: Identity, scene: Scene, init: InitImageRef, seed: int) -> RenderRequest:
    """The request that paints ``identity`` into a background of ``scene``.

    Its ``template_*`` name the identity (id, version, hash); the checkpoint, sampling and size
    are the scene's, so the figure is painted at the background's own size.
    """
    if identity.presence != "embodied":
        raise TeaserError(f"identity {identity.id} is a viewpoint: nothing is painted in")
    loras = teaser_loras(project, identity)
    if len(loras) > MAX_LORAS:  # pragma: no cover - teaser_loras refuses first
        raise TeaserError(f"a render takes at most {MAX_LORAS} LoRAs")
    return RenderRequest(
        template_id=identity.id,
        template_version=identity.version,
        template_hash=identity.hash(),
        model=scene.model,
        loras=loras,
        sampling=scene.sampling,
        width=scene.width,
        height=scene.height,
        prompt=identity.prompt,
        negative=identity.negative,
        variables={},
        seed=seed,
        init_image=init,
    )


def teaser_texts(frame: LoadedFrame, project: Project, line: Line) -> dict[str, str]:
    """What goes in each slot. A slot the teaser needs and the frame lacks is refused, not left out."""
    texts = {"quote": line.shown}
    if project.attribution:
        texts["attribution"] = project.attribution
    if project.generated_label:
        texts["label"] = GENERATED_LABEL
    missing = sorted(texts.keys() - frame.frame.texts.keys())
    if missing:
        raise TeaserError(
            f"frame {frame.frame.id} v{frame.frame.version} has no {', '.join(missing)} slot, which this teaser needs"
        )
    return texts


# -- the draft --------------------------------------------------------------------------------------


def _guard_gaps(obj: Any, policy: str) -> list[str]:
    """Why ``obj``'s texts lack a fresh passing SingGuard verdict under ``policy`` (none: they all have one)."""
    fresh, missing = fresh_verdicts(obj, policy)
    return [f"{f} has no verdict for its text under policy {policy!r}" for f, _ in missing] + [
        f"{f} was refused ({v.detail or 'no reason given'})" for f, v in fresh.items() if not v.passed
    ]


def _file(obj: Any) -> TeaserFile:
    return TeaserFile(id=obj.id, version=obj.version, hash=hex0x(obj.hash()))


def make_teaser(
    *,
    bundle: ProjectBundle,
    number: str,
    frame: LoadedFrame,
    background: Background,
    background_image: bytes,
    to: str,
    renderer: Inpainter | None,
    spoilers: SpoilerList,
    frames: set[tuple[str, int]] | None = None,
) -> Made:
    """Make the draft for teaser ``number``. Nothing is posted, pinned or signed.

    Raises :class:`TeaserNotFound`, :class:`TeaserError` (a refusal: something isn't approved,
    a spoiler rule matches, a problem stands, the frame can't take the text, darkroom can't
    inpaint), ``RenderError``/``RenderFailed`` from the inpainting, and ``FrameError``.
    """
    links = bundle.current_teasers()
    if links is None:
        raise TeaserError(f"project {bundle.id} has no approved teaser links; only approved links make teasers")
    if number not in links.teasers:
        raise TeaserNotFound(f"project {bundle.id} has no teaser {number} in its teaser links v{links.version}")
    link = links.teasers[number]
    found = problems(bundle, frames)
    if found:
        raise TeaserError("the project has problems: " + "; ".join(found))
    project = bundle.project
    line = bundle.lines[link.line.key][0]
    identity = bundle.identities[link.identity.key][0]
    scene = bundle.scenes[link.scene.key][0]
    for kind, obj in (("project", project), ("line", line), ("identity", identity), ("scene", scene)):
        if obj.status != "approved":
            raise TeaserError(f"{kind} {obj.id} v{obj.version} is {obj.status}; only approved files make teasers")
    if (frame.frame.id, frame.frame.version) != link.frame.key:
        raise TeaserError(f"{number} uses frame {link.frame.id} v{link.frame.version}, not {frame.frame.id}")
    if frame.frame.status != "approved":
        raise TeaserError(f"frame {frame.frame.id} v{frame.frame.version} is {frame.frame.status}")

    if link.background is None:
        raise TeaserError(f"{number} has no background chosen yet (its `background` in the teaser links)")
    if background.id != link.background:
        raise TeaserError(f"{number}'s background is {link.background}, not {background.id}")
    if (background.request.template_id, background.request.template_version) != link.scene.key:
        raise TeaserError(f"background {background.id} wasn't rendered from scene {scene.id} v{scene.version}")
    if background.request.template_hash != scene.hash():
        raise TeaserError(f"background {background.id} was rendered from another file than scene {scene.id}")
    if background.status != "approved":
        raise TeaserError(f"background {background.id} is {background.status}; only approved backgrounds are used")
    if sha256_hex(background_image) != background.sha256:
        raise TeaserError(f"background {background.id} on disk differs from the one approved")

    linked: list[tuple[str, Any]] = [
        ("project", project), ("line", line), ("identity", identity), ("scene", scene), ("teaser links", links)
    ]  # fmt: skip
    # A project's own frame is spoiler-checked when approved; checked again here, as the linked
    # texts are, so a rule added since holds it (its YAML's strings: comments were read at approval).
    frame_strings = file_strings(frame.frame.model_dump(mode="json")) if frame.frame.project is not None else []
    hits = spoilers.hits(*(s for _, obj in linked[:-1] for s in file_strings(obj.model_dump(mode="json"))),
                         *file_strings(link.model_dump(mode="json")), *frame_strings)  # fmt: skip
    if hits:
        raise TeaserError("held by spoiler rule " + ", ".join(map(str, hits)))
    unguarded = [
        f"{kind} {obj.id} v{obj.version}: {problem}"
        for kind, obj in linked
        for problem in _guard_gaps(obj, project.policy)
    ]
    if unguarded:
        raise TeaserError("SingGuard hasn't passed every linked text: " + "; ".join(unguarded))

    texts = teaser_texts(frame, project, line)
    made_with = {"argus-mint": argus_mint.__version__}
    if identity.presence == "embodied":
        if renderer is None:
            raise TeaserError("painting the identity in needs darkroom, and none is configured")
        if scene.character_area is None:  # pragma: no cover - problems() reports it first
            raise TeaserError(f"scene {scene.id} has no character_area")
        loras = teaser_loras(project, identity)
        check = getattr(renderer, "check_inpaint", None)
        if check is not None:
            refused = check(scene.model, len(loras))
            if refused:
                raise TeaserError("darkroom can't inpaint this teaser: " + "; ".join(refused))
        init = InitImage(
            image=background_image, mask=mask_png(scene.width, scene.height, scene.character_area),
            denoise=INPAINT_DENOISE,
        )  # fmt: skip
        ref = init.ref()
        rendered = render_requests(
            renderer,
            lambda seed: inpaint_request(project, identity, scene, ref, seed),
            teaser_seed(bundle.id, number, background.sha256),
            scene.max_attempts,
            init=init,
            label=f"{bundle.id} {number}: {identity.id} into {background.id}",
        )
        art, req = rendered.image, rendered.request
        attempts = [a.to_json() for a in rendered.attempts]
        if rendered.renderer_version:
            made_with["darkroom"] = rendered.renderer_version
    else:
        art, req, attempts = background_image, background.request, list(background.attempts)
        if "darkroom" in background.made_with:
            made_with["darkroom"] = background.made_with["darkroom"]

    try:
        image = bake_png(compose(frame, art, texts))
    except (OSError, ValueError) as e:
        raise FrameError(f"the teaser couldn't be composed: {e}") from e
    draft = CardDraft(
        kind="teaser",
        request=req,
        attempts=tuple(attempts),
        renderer="darkroom",
        to=to,
        name=f"{project.title} {number}",
        frame=frame.ref(),
        texts=texts,
        teaser=TeaserRecord(
            project=_file(project),
            links=_file(links),
            number=number,
            line=_file(line),
            identity=_file(identity),
            scene=_file(scene),
            presence=identity.presence,
            background=TeaserBackground(id=background.id, sha256=background.sha256),
            alt=link.alt,
        ),
        made_with=made_with,
        art_sha256=sha256_hex(art),
        image_sha256=sha256_hex(image),
        created_at=datetime.now(UTC),
    )
    return Made(draft, art, image, None)
