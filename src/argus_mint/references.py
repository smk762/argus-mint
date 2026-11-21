"""An identity's references: where each came from, and a real person's consent to its use.

Checked with argus-cortex's own models, so mint refuses for the same reasons, in the same words,
as mask, curator and darkroom (cortex ``provenance`` and ``consent``):

- each reference's ``provenance`` must be a valid cortex ``Provenance``;
- only cleared kinds (``stock``, ``synthetic``, ``consented``) may be approved: a teaser is public,
  and a ``personal`` or ``curated`` source isn't cleared to be shown;
- a ``consented`` reference needs the consent record its ``release_sha256`` names, in the private
  consent directory (``ARGUS_MINT_CONSENT_DIR``, ``<release_sha256>.json``). The record must be
  the one the provenance names (``consent_matches``), must bind this image (its sha256 is in the
  record's ``reference_images``), and must cover ``training`` of the subject's face today
  (``consent_valid``): references train the identity's LoRA and are scored against.

A consent record names a person, so it is never served, logged or copied: problems name the
reference's file and cortex's refusal code, never the subject.

Not pure: importing argus-cortex loads its HTTP clients, so this lives beside the server.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from argus_cortex.consent import ConsentRecord, consent_matches, consent_valid
from argus_cortex.provenance import CLEARED_KINDS, Provenance
from pydantic import ValidationError

from argus_mint.projects import Identity

__all__ = ["ConsentStore", "reference_problems"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
USE = "training"


def _errors(e: ValidationError) -> str:
    """A validation error without the values it saw (a provenance or record may name a person)."""
    return "; ".join(f"{'.'.join(map(str, err['loc'])) or 'record'}: {err['msg']}" for err in e.errors())


class ConsentStore:
    """Consent records by release sha256, read from a private directory on demand (never cached:
    a revocation counts at once)."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def get(self, release_sha256: str) -> ConsentRecord | str:
        """The record, or why there is none to use."""
        if not _SHA256.fullmatch(release_sha256):
            return "not a release sha256"
        path = self.directory / f"{release_sha256}.json"
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "no consent record for its release here"
        except OSError as e:
            return f"its consent record can't be read ({type(e).__name__})"
        except UnicodeDecodeError:
            return "its consent record isn't UTF-8"
        try:
            return ConsentRecord.model_validate(json.loads(raw))
        except json.JSONDecodeError:
            return "its consent record isn't JSON"
        except ValidationError as e:
            return f"its consent record is invalid: {_errors(e)}"


def reference_problems(identity: Identity, consent: ConsentStore | None, *, at: datetime) -> list[str]:
    """Everything that stops this identity's references being approved, as sentences that name no one."""
    out: list[str] = []
    for r in identity.references:
        label = f"identity {identity.id} v{identity.version}: reference {r.file}"
        try:
            prov = Provenance.model_validate(r.provenance)
        except ValidationError as e:
            out.append(f"{label}: its provenance isn't a valid argus-cortex Provenance: {_errors(e)}")
            continue
        if prov.kind not in CLEARED_KINDS:
            out.append(f"{label}: a {prov.kind} source isn't cleared to be shown, and teasers are public")
            continue
        if prov.kind != "consented":
            continue
        if consent is None:
            out.append(f"{label}: shows a real person, and no consent records are configured (ARGUS_MINT_CONSENT_DIR)")
            continue
        record = consent.get(prov.release_sha256 or "")
        if isinstance(record, str):
            out.append(f"{label}: {record}")
            continue
        if not consent_matches(record, prov):
            out.append(f"{label}: the consent record is for another release or subject")
            continue
        if r.sha256 not in record.reference_images:
            out.append(f"{label}: the consent record doesn't bind this image (its sha256 isn't one of the release's)")
            continue
        verdict = consent_valid(record, use=USE, likeness="face", at=at)
        if not verdict.ok:
            out.append(f"{label}: consent refused ({verdict.reason}): {verdict.detail}")
    return out
