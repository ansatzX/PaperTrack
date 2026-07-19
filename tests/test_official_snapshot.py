import hashlib
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


def _stored_snapshot_with_attachment(store, key):
    return store.commit(
        key,
        canonical_url="https://pubs.acs.org/toc/jcisd8/66/12",
        final_url="https://pubs.acs.org/toc/jcisd8/66/12",
        media_type="text/html",
        body=b"<div class='issue-item clearfix'></div>",
        validation=_validation("10.1021/acs.jcim.6c00001"),
        attachments=(
            SnapshotAttachment(
                name="abstract-123.json",
                media_type="application/json",
                body=b'{"Success": true}',
            ),
        ),
    )


def test_load_result_distinguishes_absent_snapshot(tmp_path):
    store = OfficialSnapshotStore(tmp_path)
    key = SnapshotKey("acs", "jcim", "66", "12")

    result = store.load_result(key)

    assert result.snapshot is None
    assert result.status == "absent"
    assert result.reason == "manifest_absent"
    assert result.cache_snapshot_path == (
        tmp_path / "acs" / "jcim" / "66" / "12" / "manifest.json"
    )
    assert store.load(key) is None


def test_load_result_reports_invalid_manifest_schema(tmp_path):
    store = OfficialSnapshotStore(tmp_path)
    key = SnapshotKey("acs", "jcim", "66", "12")
    _stored_snapshot_with_attachment(store, key)
    manifest_path = tmp_path / "acs" / "jcim" / "66" / "12" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["unexpected"] = True
    manifest_path.write_text(json.dumps(manifest))

    result = store.load_result(key)

    assert result.snapshot is None
    assert result.status == "rejected"
    assert result.reason == "invalid_schema"
    assert result.cache_snapshot_path == manifest_path
    assert store.load(key) is None


def test_load_result_reports_manifest_decode_failure(tmp_path):
    store = OfficialSnapshotStore(tmp_path)
    key = SnapshotKey("acs", "jcim", "66", "12")
    _stored_snapshot_with_attachment(store, key)
    manifest_path = tmp_path / "acs" / "jcim" / "66" / "12" / "manifest.json"
    manifest_path.write_bytes(b"\xffnot-json")

    result = store.load_result(key)

    assert result.snapshot is None
    assert result.status == "rejected"
    assert result.reason == "read_decode_failure"
    assert result.cache_snapshot_path == manifest_path


def test_load_result_reports_content_hash_failure(tmp_path):
    store = OfficialSnapshotStore(tmp_path)
    key = SnapshotKey("acs", "jcim", "66", "12")
    stored = _stored_snapshot_with_attachment(store, key)
    stored.body_path.write_bytes(b"corrupt")

    result = store.load_result(key)

    assert result.snapshot is None
    assert result.status == "rejected"
    assert result.reason == "content_hash_failure"
    assert result.cache_snapshot_path == stored.body_path


def test_load_result_reports_attachment_hash_failure(tmp_path):
    store = OfficialSnapshotStore(tmp_path)
    key = SnapshotKey("acs", "jcim", "66", "12")
    stored = _stored_snapshot_with_attachment(store, key)
    attachment_path = stored.attachment_paths["abstract-123.json"]
    attachment_path.write_bytes(b"corrupt")

    result = store.load_result(key)

    assert result.snapshot is None
    assert result.status == "rejected"
    assert result.reason == "attachment_hash_failure"
    assert result.cache_snapshot_path == attachment_path


def test_load_result_reports_membership_hash_mismatch(tmp_path):
    store = OfficialSnapshotStore(tmp_path)
    key = SnapshotKey("acs", "jcim", "66", "12")
    _stored_snapshot_with_attachment(store, key)
    manifest_path = tmp_path / "acs" / "jcim" / "66" / "12" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["membership_hash"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))

    result = store.load_result(key)

    assert result.snapshot is None
    assert result.status == "rejected"
    assert result.reason == "membership_hash_mismatch"
    assert result.cache_snapshot_path == manifest_path


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


@pytest.mark.parametrize("unsafe", (".", ".."))
@pytest.mark.parametrize("field_index", range(4))
def test_snapshot_key_rejects_dot_components_in_every_field(unsafe, field_index):
    components = ["acs", "jcim", "66", "12"]
    components[field_index] = unsafe

    with pytest.raises(ValueError, match="safe path component"):
        SnapshotKey(*components)


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


def test_interrupted_blob_fsync_leaves_no_final_blob_and_retry_recovers(
    tmp_path, monkeypatch
):
    store = OfficialSnapshotStore(tmp_path)
    key = SnapshotKey("acs", "jcim", "66", "12")
    body = b"new official response"
    digest = hashlib.sha256(body).hexdigest()
    blob_path = tmp_path / "blobs" / "sha256" / digest
    real_fsync = official_snapshot.os.fsync

    def fail_fsync(file_descriptor):
        raise OSError("simulated interrupted blob fsync")

    monkeypatch.setattr(official_snapshot.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="simulated interrupted blob fsync"):
        store.commit(
            key,
            "https://example.test/canonical",
            "https://example.test/final",
            "text/html",
            body,
            _validation(),
        )

    assert not blob_path.exists()

    monkeypatch.setattr(official_snapshot.os, "fsync", real_fsync)
    stored = store.commit(
        key,
        "https://example.test/canonical",
        "https://example.test/final",
        "text/html",
        body,
        _validation(),
    )

    assert stored.body_path == blob_path
    assert store.load(key) == stored


def test_commit_atomically_recovers_an_incomplete_digest_blob(tmp_path):
    store = OfficialSnapshotStore(tmp_path)
    key = SnapshotKey("acs", "jcim", "66", "12")
    body = b"complete official response"
    digest = hashlib.sha256(body).hexdigest()
    blob_path = tmp_path / "blobs" / "sha256" / digest
    blob_path.parent.mkdir(parents=True)
    blob_path.write_bytes(b"incomplete")

    stored = store.commit(
        key,
        "https://example.test/canonical",
        "https://example.test/final",
        "text/html",
        body,
        _validation(),
    )

    assert blob_path.read_bytes() == body
    assert store.load(key) == stored


def test_commit_does_not_replace_an_existing_valid_blob(tmp_path):
    store = OfficialSnapshotStore(tmp_path)
    first_key = SnapshotKey("acs", "jcim", "66", "12")
    second_key = SnapshotKey("acs", "jcim", "66", "13")
    body = b"shared official response"
    first = store.commit(
        first_key,
        "https://example.test/canonical",
        "https://example.test/final",
        "text/html",
        body,
        _validation(),
    )
    initial_inode = first.body_path.stat().st_ino

    second = store.commit(
        second_key,
        "https://example.test/canonical",
        "https://example.test/final",
        "text/html",
        body,
        _validation(),
    )

    assert second.body_path.stat().st_ino == initial_inode
