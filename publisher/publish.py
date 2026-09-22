"""External publication side effects: Cloudflare R2 and Git push."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


def get_cloud_credentials(logger: logging.Logger):
    try:
        import credentials

        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=credentials.R2_ENDPOINT,
            aws_access_key_id=credentials.R2_ACCESS_KEY,
            aws_secret_access_key=credentials.R2_SECRET_KEY,
            region_name="auto",
        )
        return client, credentials.R2_BUCKET_NAME, credentials.R2_PUBLIC_URL.rstrip("/")
    except ImportError as exc:
        logger.critical("credentials.py and boto3 are required for R2 publication.")
        raise RuntimeError("Cloud publication credentials are unavailable.") from exc
    except AttributeError as exc:
        logger.critical("Missing required R2 variable in credentials.py: %s", exc)
        raise RuntimeError("Cloud publication credentials are incomplete.") from exc


def cloud_key(date: str, filename: str) -> str:
    """Return YYYY/MM/YYYYMMDD/filename for one published image."""
    return f"{date[:4]}/{date[4:6]}/{date}/{filename}"


def upload_to_r2(
    client: Any,
    bucket_name: str,
    local_file_path: str | Path,
    object_key: str,
    logger: logging.Logger,
) -> bool:
    try:
        client.upload_file(
            str(local_file_path),
            bucket_name,
            object_key,
            ExtraArgs={
                "ContentType": "image/webp",
                # Quicklooks can be regenerated under the same canonical name.
                "CacheControl": "public, max-age=3600, must-revalidate",
            },
        )
        return True
    except Exception as exc:
        logger.error("R2 upload failed for %s: %s", local_file_path, exc)
        return False


def get_cloud_existing_keys(client: Any, bucket_name: str, logger: logging.Logger) -> set[str]:
    keys: set[str] = set()
    logger.info("SYNC MODE: building Cloudflare R2 index...")
    paginator = client.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=bucket_name):
            for obj in page.get("Contents", []):
                keys.add(obj["Key"])
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch Cloudflare R2 index: {exc}") from exc
    logger.info("Cloud index contains %d objects.", len(keys))
    return keys


def push_site_updates(
    site_dir: str | Path,
    logger: logging.Logger,
    no_push: bool = False,
    dry_run: bool = False,
) -> None:
    if no_push:
        logger.info("Git push disabled by --no-push.")
        return
    if dry_run:
        logger.info("[DRY-RUN] git add/commit/push would run now.")
        return

    try:
        import credentials

        gh_user = getattr(credentials, "GITHUB_USER", "spulidar")
        gh_token = credentials.GITHUB_TOKEN
    except (ImportError, AttributeError):
        logger.error("GitHub token not found in credentials.py; push aborted.")
        return

    original = Path.cwd()
    try:
        os.chdir(site_dir)
        subprocess.run(["git", "add", "."], check=True)
        commit = subprocess.run(
            ["git", "commit", "-m", "Update daily lidar dashboards and calendar manifest"],
            capture_output=True,
            text=True,
        )
        output = commit.stdout + commit.stderr
        if commit.returncode != 0:
            if "nothing to commit" in output.lower():
                logger.info("Nothing to commit.")
                return
            logger.error("Git commit failed: %s", output.strip())
            return

        remote = f"https://{gh_token}@github.com/{gh_user}/measurements.git"
        pushed = subprocess.run(["git", "push", remote, "main"], capture_output=True, text=True)
        if pushed.returncode == 0:
            logger.info("Successfully pushed measurements site updates.")
        else:
            logger.error("Git push failed: %s", pushed.stderr.replace(gh_token, "***HIDDEN_TOKEN***").strip())
    except subprocess.CalledProcessError as exc:
        logger.error("Git command failed: %s", exc)
    finally:
        os.chdir(original)
