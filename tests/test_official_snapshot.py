import json
from dataclasses import replace

import pytest

import papertrack.official_snapshot as official_snapshot
from papertrack.official_snapshot import (
    OfficialSnapshotStore,
    SnapshotAttachment,
    SnapshotKey,
    SnapshotValidation,
)


def _validation(*dois: str) -> SnapshotValidation:
    return SnapshotValidation(
        selector_counts={"div.issue-item.clearfix": 31},
        membership_dois=dois,
        parser_version="acs-v1",
    )


def test_snapshot_round_trip_and_corruption_rejection(tmp_path):
    store = OfficialSnapshotStore(tmp_path)
    key = SnapshotKey("acs", "jcim", "66", "12")
    validation = _validation("10.1021/acs.jcim.6c00001")
    stored = store.commit(
        key,
        canonical_url="https://pubs.acs.org/toc/jcisd8/66/12",
        final_url="https://pubs.acs.org/toc/jcisd8/66/12",
        media_type="text/html",
        body=b"<div class='issue-item clearfix'></div>",
        validation=validation,
        attachments=(
            SnapshotAttachment(
                name="abstract-123.json",
                media_type="application/json",
                body=b'{"Success": true}',
            ),
        ),
    )

    assert store.load(key) == stored
    assert stored.attachment_paths["abstract-123.json"].read_bytes() == (
        b'{"Success": true}'
    )

    stored.body_path.write_bytes(b"corrupt")
    assert store.load(key) is None


def test_snapshot_key_rejects_path_components():
    with pytest.raises(ValueError, match="safe path component"):
        SnapshotKey("acs", "../jcim", "66", "12")


@pytest.mark.parametrize("name", ("../abstract.json", "nested/abstract.json", ""))
def test_commit_rejects_unsafe_attachment_names(tmp_path, name):
    store = OfficialSnapshotStore(tmp_path)

    with pytest.raises(ValueError, match="safe path component"):
        store.commit(
            SnapshotKey("acs", "jcim", "66", "12"),
            "https://example.test/canonical",
            "https://example.test/final",
            "text/html",
            b"body",
            _validation(),
            (SnapshotAttachment(name, "application/json", b"{}"),),
        )


def test_commit_rejects_duplicate_attachment_names(tmp_path):
    store = OfficialSnapshotStore(tmp_path)
    attachment = SnapshotAttachment("abstract.json", "application/json", b"{}")

    with pytest.raises(ValueError, match="unique"):
        store.commit(
            SnapshotKey("acs", "jcim", "66", "12"),
            "https://example.test/canonical",
            "https://example.test/final",
            "text/html",
            b"body",
            _validation(),
            (attachment, replace(attachment, body=b'{"second": true}')),
        )


def test_load_rejects_attachment_corruption(tmp_path):
    store = OfficialSnapshotStore(tmp_path)
    key = SnapshotKey("acs", "jcim", "66", "12")
    stored = store.commit(
        key,
        "https://example.test/canonical",
        "https://example.test/final",
        "text/html",
        b"body",
        _validation(),
        (SnapshotAttachment("abstract.json", "application/json", b"{}"),),
    )

    stored.attachment_paths["abstract.json"].write_bytes(b"corrupt")

    assert store.load(key) is None


@pytest.mark.parametrize("mutation", ("extra", "missing"))
def test_load_rejects_manifest_without_exact_schema(tmp_path, mutation):
    store = OfficialSnapshotStore(tmp_path)
    key = SnapshotKey("acs", "jcim", "66", "12")
    store.commit(
        key,
        "https://example.test/canonical",
        "https://example.test/final",
        "text/html",
        b"body",
        _validation(),
    )
    manifest_path = tmp_path / "acs" / "jcim" / "66" / "12" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if mutation == "extra":
        manifest["unexpected"] = True
    else:
        del manifest["media_type"]
    manifest_path.write_text(json.dumps(manifest))

    assert store.load(key) is None


def test_membership_hash_normalizes_and_sorts_dois(tmp_path):
    store = OfficialSnapshotStore(tmp_path)
    first = store.commit(
        SnapshotKey("acs", "jcim", "66", "12"),
        "https://example.test/canonical",
        "https://example.test/final",
        "text/html",
        b"first",
        _validation(" 10.1000/B ", "10.1000/a"),
    )
    second = store.commit(
        SnapshotKey("acs", "jcim", "66", "13"),
        "https://example.test/canonical",
        "https://example.test/final",
        "text/html",
        b"second",
        _validation("10.1000/A", "10.1000/b"),
    )

    assert first.membership_hash == second.membership_hash


def test_failed_manifest_replace_preserves_previous_snapshot(tmp_path, monkeypatch):
    store = OfficialSnapshotStore(tmp_path)
    key = SnapshotKey("acs", "jcim", "66", "12")
    original = store.commit(
        key,
        "https://example.test/original",
        "https://example.test/original",
        "text/html",
        b"original",
        _validation("10.1000/original"),
    )

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(official_snapshot.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        store.commit(
            key,
            "https://example.test/replacement",
            "https://example.test/replacement",
            "text/html",
            b"replacement",
            _validation("10.1000/replacement"),
        )

    assert store.load(key) == original
