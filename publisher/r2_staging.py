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


def ensure_uploaded(
    client: Any,
    bucket: str,
    local_path: Path,
    metadata: dict[str, Any],
) -> bool:
    """Upload once if absent/mismatched, then always read back and SHA-256 check."""
    validate_publication_object(metadata)
    key = metadata["key"]
    if local_path.is_symlink() or not local_path.is_file():
        raise ValueError(f"Cannot upload an untrusted local file: {local_path}")
    if not verify_remote(client, bucket, metadata):
        client.upload_file(
            str(local_path), bucket, key,
            ExtraArgs={
                "ContentType": "image/webp",
                "CacheControl": "public, max-age=3600, must-revalidate",
                "Metadata": {"sha256": metadata["sha256"]},
            },
        )
        if not verify_remote(client, bucket, metadata):
            raise RuntimeError(f"R2 SHA-256 verification failed after upload: {key}")
        return True
    return False
