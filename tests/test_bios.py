"""Bios: reviewed bio templates, the name token, screening and retries, the writer client, and the CLI."""

from __future__ import annotations

import json

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from argus_mint.bios import BioError, BioFailed, BioTemplate, ChatWriter, clean, load_bio, write_bio
from argus_mint.darkroom import DarkroomClient
from argus_mint.templates import Template

runner = CliRunner()
VALUES = {"role": "ranger", "colour": "none", "pose": "standing"}


@pytest.fixture
def bio_doc():
    return {
        "id": "test-bio",
        "version": 1,
        "status": "approved",
        "approved_by": "reviewer",
        "approved_at": "2026-11-09",
        "model": "mistral-nemo",
        "system": "Fruity flavour text. Call the character {{name}} only.",
        "prompt": "Write about {{name}}, a {role} with {colour} heraldry, {pose}.",
        "max_attempts": 3,
    }


class Writer:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def write(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


class Guard:
    def __init__(self, *verdicts):
        self.verdicts = list(verdicts)
        self.seen = []

    def check_text(self, text):
        self.seen.append(text)
        return self.verdicts.pop(0) if self.verdicts else (True, "")


def test_name_goes_in_after_the_model_and_never_to_it(bio_doc, template_doc):
    bt, t = BioTemplate.model_validate(bio_doc), Template.model_validate(template_doc)
    w, g = Writer("  “{Name} strides forth, {name}!”  "), Guard()
    b = write_bio(w, g, bt, VALUES, t, "Hana of the Strait")
    assert b.text == "Hana of the Strait strides forth, Hana of the Strait!"
    assert all("Hana" not in v for call in w.calls for v in map(str, call.values()))  # never sent to the writer
    assert w.calls[0]["system"] == "Fruity flavour text. Call the character {name} only."
    assert w.calls[0]["prompt"] == "Write about {name}, a ranger with none heraldry, standing."
    assert g.seen == ["{name} strides forth, {name}!"]  # screened with the token, not the name


def test_flagged_and_unusable_replies_are_retried_then_fail(bio_doc, template_doc):
    bt, t = BioTemplate.model_validate(bio_doc), Template.model_validate(template_doc)
    b = write_bio(Writer("{name} and {friend}", "bad", "{name} is fine."), Guard((False, "B")), bt, VALUES, t, "Hana")
    assert [a.outcome for a in b.attempts] == ["unusable", "rejected", "ok"] and b.text == "Hana is fine."
    with pytest.raises(BioFailed) as e:
        write_bio(Writer("a", "b", "c"), Guard((False, "A"), (False, "A"), (False, "A")), bt, VALUES, t, "Hana")
    assert len(e.value.attempts) == 3


def test_bio_templates_are_reviewed_and_keep_the_name_out(bio_doc, template_doc):
    t = Template.model_validate(template_doc)
    draft = BioTemplate.model_validate({**bio_doc, "status": "draft", "approved_by": None, "approved_at": None})
    with pytest.raises(BioError, match="only approved"):
        write_bio(Writer("x"), Guard(), draft, VALUES, t, "Hana")
    assert write_bio(Writer("{name}."), Guard(), draft, VALUES, t, "Hana", review=True).text == "Hana."
    with pytest.raises(ValueError, match="never goes to the model"):
        BioTemplate.model_validate({**bio_doc, "prompt": "About {name}, a {role}."})
    with pytest.raises(ValueError, match="system prompt"):
        BioTemplate.model_validate({**bio_doc, "system": "Be {mood}."})
    with pytest.raises(BioError, match="doesn't have"):
        BioTemplate.model_validate({**bio_doc, "prompt": "A {weapon}."}).fill(VALUES, t)


def test_clean_cuts_long_replies_at_a_sentence():
    text = "First sentence here. Second one goes on. " + "and on " * 40
    out = clean(text, 60)
    assert out == "First sentence here. Second one goes on."
    assert clean("x " * 100, 50).endswith("…")


def test_chat_writer_and_darkroom_text_check():
    seen = []

    def chat(request):
        seen.append((request.url.path, request.headers.get("authorization"), json.loads(request.content)))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{name} sings."}}]})

    w = ChatWriter("http://writer/", api_key="k", client=httpx.Client(transport=httpx.MockTransport(chat)))
    assert w.write(model="m", system="s", prompt="p", temperature=0.5, max_tokens=10) == "{name} sings."
    path, auth, body = seen[0]
    assert path == "/chat" and auth == "Bearer k" and body["messages"][1] == {"role": "user", "content": "p"}

    def preflight(request):
        body = json.loads(request.content)
        refused = "blood" in body["user_prompt"]
        assert body["enforcing_policy"] == "default" and body["expand"] is False
        return httpx.Response(
            200, json={"outcome": "refused" if refused else "ok", "refusal": {"rule": "B. Crimes"} if refused else None}
        )

    d = DarkroomClient("http://darkroom", client=httpx.Client(transport=httpx.MockTransport(preflight)))
    assert d.check_text("{name} sings.") == (True, "")
    assert d.check_text("blood everywhere") == (False, "B. Crimes")


def test_draft_with_bio_frame_and_display_text(tmp_path, template_doc, bio_doc, monkeypatch):
    from argus_mint import cli

    from .test_cli import approve, draft_args, publish_args
    from .test_frames import ART, _frame

    frame = _frame(tmp_path)  # slots: name, bio
    (tmp_path / "bio.yaml").write_text(yaml.safe_dump(bio_doc))
    image = tmp_path / "render.png"
    image.write_bytes(ART)
    (tmp_path / "t.yaml").write_text(yaml.safe_dump(template_doc))
    monkeypatch.setattr(cli, "ChatWriter", lambda url, **kw: Writer("{name} hums a tune."))
    monkeypatch.setattr(cli, "DarkroomClient", lambda url, **kw: Guard())
    base = draft_args(tmp_path / "t.yaml", tmp_path / "d", "--image", str(image), "--frame", str(frame))

    result = runner.invoke(cli.app, [*base, "--bio", str(tmp_path / "bio.yaml"), "--writer", "http://w"])
    assert result.exit_code == 2 and "needs --darkroom" in result.output
    result = runner.invoke(cli.app, [*base, "--text", "stats=3/2", "--bio", str(tmp_path / "bio.yaml")])
    assert result.exit_code != 0  # the test frame has no stats slot, and --bio needs --writer
    result = runner.invoke(
        cli.app, [*base, "--bio", str(tmp_path / "bio.yaml"), "--writer", "http://w", "--darkroom", "http://dr"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["bio"] == "Hana of the Strait hums a tune."
    approve(tmp_path / "d")
    monkeypatch.setenv("ARGUS_MINT_SIGNER_KEY", "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d")
    pub = json.loads(runner.invoke(cli.app, publish_args(tmp_path / "d", "--dry-run")).stdout)
    assert pub["metadata"]["description"] == "Hana of the Strait hums a tune."
    assert pub["metadata"]["provenance"]["bio"]["id"] == "test-bio"
    assert pub["metadata"]["provenance"]["bio"]["model"] == "mistral-nemo"


def test_draft_text_needs_a_frame(tmp_path, template_doc):
    from argus_mint import cli

    from .test_cli import draft_args

    (tmp_path / "t.yaml").write_text(yaml.safe_dump(template_doc))
    args = draft_args(tmp_path / "t.yaml", tmp_path / "d", "--placeholder", "--text", "class=Ranger")
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2 and "needs a --frame" in result.output
    args = draft_args(tmp_path / "t.yaml", tmp_path / "d", "--placeholder", "--text", "name=Other")
    assert runner.invoke(cli.app, args).exit_code == 2


def test_repo_example_bios_are_valid():
    from pathlib import Path

    from argus_mint.templates import load_template

    root = Path(__file__).resolve().parents[1]
    t = load_template(root / "templates" / "example-portrait-v2.yaml")
    for f in (root / "bios").glob("*.yaml"):
        bt = load_bio(f)
        bt.fill({k: v[0] for k, v in t.variables.items()}, t)
