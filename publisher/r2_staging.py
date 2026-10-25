"""Cloudflare R2 isolated v2 transfer and byte-for-byte remote verification.

No deletion, promotion, production-key write or public-site publication API exists here.
"""
from __future__ import annotations

from hashlib import sha256
import importlib
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .published import validate_publication_object


def r2_client_from_environment():
    """Credentials are runtime-only, preferring environment to old private credentials.py."""
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install MILGRAU with pip install -e '.[web]' for R2 staging.") from exc
    try:
        private = importlib.import_module("credentials")
    except ImportError:
        private = None

    def secret(env: str, legacy: str) -> str:
        return (os.environ.get(env) or str(getattr(private, legacy, "") or "")).strip()

    endpoint = secret("R2_ENDPOINT", "R2_ENDPOINT")
    key = secret("R2_ACCESS_KEY", "R2_ACCESS_KEY")
    password = secret("R2_SECRET_KEY", "R2_SECRET_KEY")
    if not all((endpoint, key, password)):
        raise RuntimeError(
            "R2 credentials not configured: set R2_ENDPOINT, R2_ACCESS_KEY "
            "and R2_SECRET_KEY (or use the existing private credentials.py)."
        )
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("R2_ENDPOINT must be a credential-free HTTPS endpoint.")
    return boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=key, aws_secret_access_key=password,
        region_name="auto",
    )


def _remote_digest(client: Any, bucket: str, key: str) -> tuple[int, str] | None:
    """Hash the actual downloaded bytes, not just self-declared upload metadata."""
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if str(code) in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise RuntimeError(f"Cannot read R2 staging object {key}: {exc}") from exc

    body = response["Body"]
    digest = sha256()
    size = 0
    try:
        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
            if chunk:
                size += len(chunk)
                digest.update(chunk)
    finally:
        body.close()
    if int(response.get("ContentLength", size)) != size:
        raise RuntimeError(f"Incomplete R2 object response for {key}")
    return size, digest.hexdigest()


def verify_remote(client: Any, bucket: str, metadata: dict[str, Any]) -> bool:
    """Return true only for a byte-for-byte matching R2 object."""
    result = _remote_digest(client, bucket, metadata["key"])
    return result == (metadata["size"], metadata["sha256"])


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
