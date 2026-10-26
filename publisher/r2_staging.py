"""Cloudflare R2 isolated v2 transfer and byte-for-byte remote verification.

No deletion, promotion, production-key write or public-site publication API exists here.
"""
from __future__ import annotations

from hashlib import sha256
import logging
import time
from botocore.exceptions import ReadTimeoutError, ConnectTimeoutError, ConnectionClosedError, EndpointConnectionError
from pathlib import Path
from typing import Any

from .published import validate_publication_object


def _remote_digest_once(client: Any, bucket: str, key: str) -> tuple[int, str] | None:
    """Compute a fresh SHA-256 over every remote response, closing the stream."""
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if str(code) in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    body = response["Body"]
    digest = sha256()
    size = 0
    try:
        for chunk in body.iter_chunks(chunk_size=256 * 1024):
            if chunk:
                size += len(chunk)
                digest.update(chunk)
    finally:
        body.close()
    if int(response.get("ContentLength", size)) != size:
        raise IncompleteR2Read(f"Incomplete R2 response for {key}")
    return size, digest.hexdigest()


class IncompleteR2Read(RuntimeError):
    pass


def _remote_digest(client: Any, bucket: str, key: str) -> tuple[int, str] | None:
    """Retry transient streaming read errors; never trust a partial digest."""
    retryable = (ReadTimeoutError, ConnectTimeoutError, ConnectionClosedError,
                 EndpointConnectionError, TimeoutError, ConnectionError, IncompleteR2Read)
    for attempt in range(1, 6):
        try:
            return _remote_digest_once(client, bucket, key)
        except retryable as exc:
            if attempt == 5:
                raise RuntimeError(f"R2 read failed for {key} after 5 attempts; cache retained") from exc
            delay = 2 ** (attempt - 1)
            logging.getLogger("milgrau-publish").warning(
                "R2 read retry %d/5 for %s after %s; retrying in %ds",
                attempt + 1, key, type(exc).__name__, delay,
            )
            time.sleep(delay)
    raise AssertionError("Unreachable")



def previous_backup_key(key: str, old_sha256: str) -> str:
    """Versioned recovery objects live outside public spu/YYYY/... directories."""
    from .v2 import _FINGERPRINT
    if not isinstance(old_sha256, str) or not _FINGERPRINT.fullmatch(old_sha256):
        raise ValueError("Invalid SHA-256 for R2 backup")
    if not key.startswith("spu/") or key.count("/") != 4:
        raise ValueError("Cannot back up a noncanonical SPU key")
    return f"_publication_v2_backups/{old_sha256}/{key}"


def _make_verified_backup(client: Any, bucket: str, key: str,
                          old: tuple[int, str]) -> str:
    backup = previous_backup_key(key, old[1])
    current_backup = _remote_digest(client, bucket, backup)
    if current_backup is None:
        client.copy_object(
            Bucket=bucket, CopySource={"Bucket": bucket, "Key": key},
            Key=backup, MetadataDirective="COPY",
        )
    elif current_backup != old:
        raise RuntimeError(f"R2 recovery backup conflicts with original: {backup}")
    if _remote_digest(client, bucket, backup) != old:
        raise RuntimeError(f"R2 recovery backup failed SHA-256 verification: {backup}")
    return backup


def restore_previous(client: Any, bucket: str, new: dict[str, Any],
                     previous: dict[str, Any]) -> None:
    """Restore the previously published bytes after a failed replacement."""
    validate_publication_object(new)
    validate_publication_object(previous)
    key = new["key"]
    if previous["key"] != key:
        raise RuntimeError(f"Rollback R2 key mismatch: {key}")
    original = (previous["size"], previous["sha256"])
    now = _remote_digest(client, bucket, key)
    if now == original:
        return
    if now != (new["size"], new["sha256"]):
        raise RuntimeError(f"Refusing rollback of unexpected concurrent R2 change: {key}")
    backup = previous_backup_key(key, previous["sha256"])
    if _remote_digest(client, bucket, backup) != original:
        raise RuntimeError(f"Missing or invalid recovery backup for {key}: {backup}")
    client.copy_object(
        Bucket=bucket, CopySource={"Bucket": bucket, "Key": backup},
        Key=key, MetadataDirective="COPY",
    )
    if _remote_digest(client, bucket, key) != original:
        raise RuntimeError(f"R2 rollback failed verification: {key}")


def ensure_uploaded(
    client: Any,
    bucket: str,
    local_path: Path,
    metadata: dict[str, Any],
    *,
    allow_replace: bool = False,
    previous: dict[str, Any] | None = None,
) -> bool:
    """Skip matching files; require opt-in plus verified backup for any overwrite."""
    validate_publication_object(metadata)
    key = metadata["key"]
    if local_path.is_symlink() or not local_path.is_file():
        raise ValueError(f"Cannot upload an untrusted local file: {local_path}")
    wanted = (metadata["size"], metadata["sha256"])
    current = _remote_digest(client, bucket, key)
    if current == wanted:
        return False
    replacing = current is not None
    if replacing:
        if not allow_replace:
            raise RuntimeError(
                f"R2 object already has different bytes: {key}. "
                "Explicit --replace-existing is required to update a published WEBP."
            )
        if previous is None:
            raise RuntimeError(f"Refusing overwrite of untracked R2 object: {key}")
        validate_publication_object(previous)
        if previous["key"] != key or current != (previous["size"], previous["sha256"]):
            raise RuntimeError(f"R2 object differs from previous verified Git state: {key}")
        _make_verified_backup(client, bucket, key, current)

    try:
        client.upload_file(
            str(local_path), bucket, key,
            ExtraArgs={
                "ContentType": "image/webp",
                "CacheControl": "public, max-age=300, must-revalidate",
                "Metadata": {"sha256": metadata["sha256"]},
            },
        )
        if _remote_digest(client, bucket, key) != wanted:
            raise RuntimeError(f"R2 SHA-256 verification failed after upload: {key}")
    except Exception as exc:
        if replacing and previous is not None:
            try:
                restore_previous(client, bucket, metadata, previous)
            except Exception as rollback_exc:
                raise RuntimeError(
                    f"R2 upload failed and rollback also failed for {key}: {rollback_exc}. "
                    "Inspect the verified backup before retrying."
                ) from exc
        raise
    return True
