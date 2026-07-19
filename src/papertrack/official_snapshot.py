"""Durable, validated snapshots of authoritative journal issue responses."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .identity import normalize_doi


_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class SnapshotKey:
    provider: str
    journal: str
    volume: str
    issue: str

    def __post_init__(self) -> None:
        for value in (self.provider, self.journal, self.volume, self.issue):
            if value in {".", ".."} or not re.fullmatch(
                r"[A-Za-z0-9_.-]+", value
            ):
                raise ValueError("snapshot key requires a safe path component")


@dataclass(frozen=True)
class SnapshotAttachment:
    name: str
    media_type: str
    body: bytes


@dataclass(frozen=True)
class SnapshotValidation:
    selector_counts: dict[str, int]
    membership_dois: tuple[str, ...]
    parser_version: str


@dataclass(frozen=True)
class OfficialSnapshot:
    key: SnapshotKey
    canonical_url: str
    final_url: str
    media_type: str
    content_hash: str
    membership_hash: str
    captured_at: str
    validation: SnapshotValidation
    body_path: Path
    attachment_paths: dict[str, Path]


class OfficialSnapshotStore:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)

    def load(self, key: SnapshotKey) -> OfficialSnapshot | None:
        """Return only a manifest/body pair whose hashes and exact schema validate."""
        manifest_path = self._manifest_path(key)
        try:
            manifest = json.loads(manifest_path.read_bytes())
            self._validate_manifest(manifest)
            manifest_key = SnapshotKey(**manifest["key"])
            if manifest_key != key:
                return None

            content_hash = manifest["content_hash"]
            body_path = self._blob_path(content_hash)
            if not self._blob_matches(body_path, content_hash):
                return None

            attachment_paths: dict[str, Path] = {}
            for attachment in manifest["attachments"]:
                digest = attachment["content_hash"]
                path = self._blob_path(digest)
                if not self._blob_matches(path, digest):
                    return None
                attachment_paths[attachment["name"]] = path

            validation_data = manifest["validation"]
            validation = SnapshotValidation(
                selector_counts=validation_data["selector_counts"],
                membership_dois=tuple(validation_data["membership_dois"]),
                parser_version=validation_data["parser_version"],
            )
            if self._membership_hash(validation.membership_dois) != manifest[
                "membership_hash"
            ]:
                return None

            return OfficialSnapshot(
                key=key,
                canonical_url=manifest["canonical_url"],
                final_url=manifest["final_url"],
                media_type=manifest["media_type"],
                content_hash=content_hash,
                membership_hash=manifest["membership_hash"],
                captured_at=manifest["captured_at"],
                validation=validation,
                body_path=body_path,
                attachment_paths=attachment_paths,
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def commit(
        self,
        key: SnapshotKey,
        canonical_url: str,
        final_url: str,
        media_type: str,
        body: bytes,
        validation: SnapshotValidation,
        attachments: tuple[SnapshotAttachment, ...] = (),
    ) -> OfficialSnapshot:
        """Fsync immutable content blobs, then atomically replace the issue manifest."""
        names: set[str] = set()
        for attachment in attachments:
            if not _SAFE_COMPONENT.fullmatch(attachment.name):
                raise ValueError("attachment name requires a safe path component")
            if attachment.name in names:
                raise ValueError("attachment names must be unique")
            names.add(attachment.name)

        content_hash = self._digest(body)
        self._write_blob(content_hash, body)
        attachment_records = []
        for attachment in attachments:
            digest = self._digest(attachment.body)
            self._write_blob(digest, attachment.body)
            attachment_records.append(
                {
                    "name": attachment.name,
                    "media_type": attachment.media_type,
                    "content_hash": digest,
                }
            )

        captured_at = datetime.now(timezone.utc).isoformat()
        membership_hash = self._membership_hash(validation.membership_dois)
        manifest = {
            "key": {
                "provider": key.provider,
                "journal": key.journal,
                "volume": key.volume,
                "issue": key.issue,
            },
            "canonical_url": canonical_url,
            "final_url": final_url,
            "media_type": media_type,
            "content_hash": content_hash,
            "membership_hash": membership_hash,
            "captured_at": captured_at,
            "validation": {
                "selector_counts": validation.selector_counts,
                "membership_dois": list(validation.membership_dois),
                "parser_version": validation.parser_version,
            },
            "attachments": attachment_records,
        }
        self._validate_manifest(manifest)
        self._replace_manifest(key, manifest)

        stored = self.load(key)
        if stored is None:
            raise RuntimeError("committed snapshot failed validation")
        return stored

    def _manifest_path(self, key: SnapshotKey) -> Path:
        return (
            self.root
            / key.provider
            / key.journal
            / key.volume
            / key.issue
            / "manifest.json"
        )

    def _blob_path(self, digest: str) -> Path:
        return self.root / "blobs" / "sha256" / digest

    @staticmethod
    def _digest(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    @classmethod
    def _membership_hash(cls, dois: tuple[str, ...]) -> str:
        normalized = sorted(normalize_doi(doi) for doi in dois)
        return cls._digest("\n".join(normalized).encode("utf-8"))

    def _write_blob(self, digest: str, body: bytes) -> None:
        path = self._blob_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._blob_matches(path, digest):
            self._fsync_directory(path.parent)
            return

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{digest}-", delete=False
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())

            try:
                os.link(temporary_path, path)
            except FileExistsError:
                if self._blob_matches(path, digest):
                    self._fsync_directory(path.parent)
                    return
                os.replace(temporary_path, path)
                temporary_path = None
            else:
                temporary_path.unlink()
                temporary_path = None
            self._fsync_directory(path.parent)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        directory_descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    @staticmethod
    def _blob_matches(path: Path, expected_hash: str) -> bool:
        if not _SHA256.fullmatch(expected_hash):
            return False
        try:
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return False
        return actual_hash == expected_hash

    def _replace_manifest(self, key: SnapshotKey, manifest: dict[str, Any]) -> None:
        path = self._manifest_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=".manifest-", delete=False
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _validate_manifest(manifest: Any) -> None:
        if not isinstance(manifest, dict) or set(manifest) != {
            "key",
            "canonical_url",
            "final_url",
            "media_type",
            "content_hash",
            "membership_hash",
            "captured_at",
            "validation",
            "attachments",
        }:
            raise ValueError("invalid snapshot manifest schema")

        key = manifest["key"]
        if not isinstance(key, dict) or set(key) != {
            "provider",
            "journal",
            "volume",
            "issue",
        }:
            raise ValueError("invalid snapshot key schema")
        SnapshotKey(**key)

        for field in (
            "canonical_url",
            "final_url",
            "media_type",
            "captured_at",
        ):
            if not isinstance(manifest[field], str):
                raise ValueError(f"invalid snapshot manifest field: {field}")
        for field in ("content_hash", "membership_hash"):
            if not isinstance(manifest[field], str) or not _SHA256.fullmatch(
                manifest[field]
            ):
                raise ValueError(f"invalid snapshot manifest field: {field}")

        validation = manifest["validation"]
        if not isinstance(validation, dict) or set(validation) != {
            "selector_counts",
            "membership_dois",
            "parser_version",
        }:
            raise ValueError("invalid snapshot validation schema")
        counts = validation["selector_counts"]
        if not isinstance(counts, dict) or not all(
            isinstance(selector, str)
            and isinstance(count, int)
            and not isinstance(count, bool)
            for selector, count in counts.items()
        ):
            raise ValueError("invalid snapshot selector counts")
        if not isinstance(validation["membership_dois"], list) or not all(
            isinstance(doi, str) for doi in validation["membership_dois"]
        ):
            raise ValueError("invalid snapshot membership DOIs")
        if not isinstance(validation["parser_version"], str):
            raise ValueError("invalid snapshot parser version")

        attachments = manifest["attachments"]
        if not isinstance(attachments, list):
            raise ValueError("invalid snapshot attachments")
        names: set[str] = set()
        for attachment in attachments:
            if not isinstance(attachment, dict) or set(attachment) != {
                "name",
                "media_type",
                "content_hash",
            }:
                raise ValueError("invalid snapshot attachment schema")
            name = attachment["name"]
            if not isinstance(name, str) or not _SAFE_COMPONENT.fullmatch(name):
                raise ValueError("invalid snapshot attachment name")
            if name in names:
                raise ValueError("snapshot attachment names must be unique")
            names.add(name)
            if not isinstance(attachment["media_type"], str):
                raise ValueError("invalid snapshot attachment media type")
            digest = attachment["content_hash"]
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ValueError("invalid snapshot attachment hash")
