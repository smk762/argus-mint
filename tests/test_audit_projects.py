"""Regression tests for audit findings in posting, projects, server_projects, references and darkroom."""

from __future__ import annotations

import contextlib
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

from argus_mint.posting import AnchorLeaf, AnchorStore, approved_at
from tests.test_posting import _record

SHA = "ab" * 32


def test_approved_at_is_none_for_an_approved_draft_without_a_matching_approval():
    other = SimpleNamespace(at=datetime.now(UTC), image_sha256="cd" * 32, decision="approved")
    d = SimpleNamespace(status="approved", image_sha256=SHA, reviews=[other])
    assert approved_at(d) is None


def test_concurrent_anchor_adds_keep_every_record(tmp_path, monkeypatch):
    store = AnchorStore(tmp_path / "anchors")
    load = store.load
    barrier = threading.Barrier(4, timeout=0.2)

    def slow_load(project):  # every add reads before any writes, unless add serialises them
        out = load(project)
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()
        return out

    monkeypatch.setattr(store, "load", slow_load)

    def add(i):
        leaf = AnchorLeaf(draft=f"d{i}", number=f"T{i}", sha256=f"{i:064x}")
        store.add("lamp", _record(i, [leaf], tmp_path / f"tree-{i}.json"))

    threads = [threading.Thread(target=add, args=(i,)) for i in range(1, 5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.epoch for r in load("lamp")) == [1, 2, 3, 4]


def _example_links():
    from argus_mint.projects import load_bundle
    from tests.test_teasers import PID, REPO

    b = load_bundle(REPO / "projects" / PID)
    (links, _path), *_ = b.teasers.values()
    return b, links


def test_a_viewpoint_teaser_on_its_identitys_frame_has_no_frame_problem():
    from argus_mint.projects import teaser_problems

    b, links = _example_links()
    assert not [p for p in teaser_problems(b, links) if "frame" in p]


def test_a_viewpoint_teaser_on_another_frame_says_it_must_use_its_identitys():
    from argus_mint.projects import FrameLink, teaser_problems

    b, links = _example_links()
    t2 = links.teasers["T2"].model_copy(update={"frame": FrameLink(id="example-teaser", version=1)})
    links = links.model_copy(update={"teasers": {**links.teasers, "T2": t2}})
    assert "T2: a viewpoint teaser must use its identity's frame (example-teasers/harbour-feed v1)" in teaser_problems(
        b, links
    )


def test_a_viewpoint_identity_without_a_frame_is_a_problem_not_a_crash():
    from argus_mint.projects import teaser_problems

    b, links = _example_links()
    key = links.teasers["T2"].identity.key
    ident, path = b.identities[key]
    b.identities[key] = (ident.model_copy(update={"frame": None}), path)  # skips the model's own check
    assert "T2: viewpoint identity lamp has no frame" in teaser_problems(b, links)


def test_a_draft_saved_over_while_it_is_checked_is_not_approved(tmp_path):
    from tests.test_server_projects import Guard, _approve, make

    build = make.__wrapped__(tmp_path)
    path = None

    class SavesMidCheck(Guard):  # a concurrent save rewrites the draft while SingGuard judges it
        def check_text(self, text, policy=None):
            path.write_text(path.read_text().replace(text, "The sea keeps what it is given."))
            return super().check_text(text, policy)

    c, _, root = build(SavesMidCheck())
    path = root / "lines" / "every-boat-v1.yaml"
    r = _approve(c, "line", "every-boat", 1)
    assert r.status_code == 409 and "changed while it was checked" in r.json()["detail"]
    assert "status: draft" in path.read_text()


def test_a_new_project_version_never_overwrites_an_approved_project_yaml(tmp_path):
    from tests.test_server_projects import AUTH, PID, _approve, make

    c, _, root = make.__wrapped__(tmp_path)()
    assert _approve(c, "project", PID, 1).status_code == 200
    before = (root / "project.yaml").read_text()
    r = c.post(f"/projects/{PID}/files/project/{PID}/2", json={"doc": {"title": "Replaced"}}, headers=AUTH)
    assert r.status_code == 409 and "approved v1" in r.json()["detail"]
    assert (root / "project.yaml").read_text() == before
