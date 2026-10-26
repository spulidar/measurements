"""Safe incremental publication of cached WEBPs into isolated R2 staging.

A window is removable from local cache only after:
  1. sidecar and local SHA-256 validation,
  2. each remote R2 object re-downloaded and SHA-256 verified,
  3. persistent schema-v2 state committed and pushed to publication-v2,
  4. remote Git branch head confirmed to equal the pushed commit.

A crash or failed upload/commit/push leaves the cache intact for recovery.
"""
from __future__ import annotations

from pathlib import Path
import logging
import re
import subprocess
from typing import Any, Callable

from .published import (
    load_published_states, update_published_states,
    write_published_states, window_record,
)
from .r2_staging import ensure_uploaded, restore_previous
from .v2 import _hash_file, collect_validated_staging

class RemoteStateAmbiguous(RuntimeError):
    """Git outcome unverified; preserve R2 until manual reconciliation."""


BRANCH = "publication-v2"
REPO_URL = re.compile(r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)spulidar/measurements(?:\.git)?/?$")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Git {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def ensure_git_ready(repo: str | Path) -> None:
    """Protect the user's live branch, origin, and unrelated working changes."""
    root = Path(repo).resolve()
    if _git(root, "branch", "--show-current") != BRANCH:
        raise RuntimeError(f"Publication state may be pushed only from {BRANCH}.")
    origin = _git(root, "remote", "get-url", "origin")
    if not REPO_URL.search(origin):
        raise RuntimeError("Git origin must point to spulidar/measurements.")
    dirty = _git(root, "status", "--porcelain", "--", "publisher/state-v2")
    if dirty:
        raise RuntimeError(
            "Uncommitted publication state detected. Resolve the local state-v2 "
            "changes before publishing; cached images remain untouched."
        )
    local_head = _git(root, "rev-parse", "HEAD")
    remote = _git(root, "ls-remote", "--heads", "origin", BRANCH)
    remote_head = remote.split()[0] if remote else ""
    if remote_head != local_head:
        raise RuntimeError(
            "Local publication-v2 is not synchronized with its GitHub branch. "
            "Pull/reconcile before publishing; no R2 upload was started."
        )


def push_state(repo: str | Path, changed_paths: list[Path]) -> None:
    """Commit ONLY Git-tracked state-v2 and verify origin branch HEAD after pushing."""
    root = Path(repo).resolve()
    if changed_paths:
        relative = [str(path.resolve().relative_to(root)) for path in changed_paths]
        _git(root, "add", "--", *relative)
        _git(root, "commit", "--only", "-m", "Record verified R2 staging publication-v2 windows", "--", *relative)
    head = _git(root, "rev-parse", "HEAD")
    _git(root, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
    remote = _git(root, "ls-remote", "--heads", "origin", BRANCH)
    if not remote or remote.split()[0] != head:
        raise RuntimeError("GitHub did not confirm persistent publication-v2 state; keep cache.")


def clean_verified_window(cache_root: str | Path, pub_id: str, sidecar: dict[str, Any]) -> None:
    """Unlink only the attested assets and its own sidecar; never touch other MILGRAU caches."""
    root = Path(cache_root).resolve()
    date, station, hour = pub_id.split("_")
    day_dir = root / station / date[:4] / date[4:6] / date
    if day_dir.is_symlink() or any(part.is_symlink() for part in (root / station, root / station / date[:4], root / station / date[:4] / date[4:6])):
        raise ValueError("Refusing cleanup through a symlinked cache directory.")
    path = day_dir / f"{pub_id}.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Missing safe cache sidecar for cleanup: {path}")
    from .v2 import _validated_sidecar
    validated = _validated_sidecar(path, root, station)
    if validated != sidecar:
        raise ValueError("Sidecar changed during publication; cache retained.")
    # Preflight all assets before deleting any file from this window.
    for filename in sidecar["assets"]:
        file = day_dir / filename
        if file.is_symlink() or not file.is_file() or _hash_file(file) != sidecar["asset_hashes"][filename]:
            raise ValueError(f"Cached asset changed before cleanup: {file}")
    for filename in sidecar["assets"]:
        (day_dir / filename).unlink()
    path.unlink()
    for directory in (day_dir, day_dir.parent, day_dir.parent.parent, day_dir.parent.parent.parent):
        if directory == root:
            break
        try:
            directory.rmdir()
        except OSError:
            break


def publish_cache(
    cache_root: str | Path, station: str, repository: str | Path,
    bucket: str, r2_client: Any, *,
    max_windows: int | None = None,
    selected_dates: set[str] | None = None,
    excluded_dates: set[str] | frozenset[str] = frozenset(),
    keep_cache: bool = False,
    replace_existing: bool = False,
    logger: logging.Logger | None = None,
    git_ready: Callable[[str | Path], None] = ensure_git_ready,
    git_persist: Callable[[str | Path, list[Path]], None] = push_state,
) -> dict[str, int]:
    """Incrementally publish selected locally staged windows, without touching production."""
    if max_windows is not None and max_windows < 1:
        raise ValueError("max_windows must be positive")
    # Check Git before any network upload or local mutation.
    git_ready(repository)
    catalog = collect_validated_staging(cache_root, station)
    items = sorted(
        (pub_id, sidecar) for pub_id, sidecar in catalog.items()
        if (selected_dates is None or pub_id[:8] in selected_dates)
        and pub_id[:8] not in excluded_dates
    )
    if max_windows is not None:
        items = items[:max_windows]
    previous = load_published_states(repository, station)
    records = {}
    uploaded = 0
    overwritten: list[tuple[dict[str, Any], dict[str, Any]]] = []
    planned = []
    for pub_id, sidecar in items:
        record = window_record(pub_id, sidecar, Path(cache_root).resolve(), bucket)
        earlier = (previous.get(pub_id[:4], {})
                   .get("days", {}).get(pub_id[:8], {})
                   .get("publication_windows", {}).get(pub_id[-2:]))
        if earlier and earlier["r2_bucket"] != bucket:
            raise ValueError(
                f"Window {pub_id} belongs to another R2 bucket; "
                "explicit migration is required."
            )
        planned.append((pub_id, sidecar, record, earlier))

    try:
        for pub_id, sidecar, record, earlier in planned:
            for filename, metadata in record["assets"].items():
                old_metadata = (earlier or {}).get("assets", {}).get(filename)
                local = Path(cache_root).resolve() / station / pub_id[:4] / pub_id[4:6] / pub_id[:8] / filename
                if ensure_uploaded(
                    r2_client, bucket, local, metadata,
                    allow_replace=replace_existing, previous=old_metadata,
                ):
                    uploaded += 1
                    if old_metadata and old_metadata["sha256"] != metadata["sha256"]:
                        overwritten.append((metadata, old_metadata))
            records[pub_id] = record
            if logger:
                logger.info("Verified R2 publication window %s: %d object(s)", pub_id, len(record["assets"]))
        updated = update_published_states(previous, station, records)
        changed_paths = write_published_states(repository, station, previous, updated)
        # Keep local images until GitHub confirms its publication state.
        git_persist(repository, changed_paths)
    except Exception as exc:
        if isinstance(exc, RemoteStateAmbiguous):
            # Never restore old R2 bytes if Git might already reference the new ones.
            raise
        rollback_errors: list[str] = []
        for new_metadata, old_metadata in reversed(overwritten):
            try:
                restore_previous(r2_client, bucket, new_metadata, old_metadata)
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            raise RuntimeError(
                "Publication failed; some R2 overwrites could not be restored: "
                + "; ".join(rollback_errors)
            ) from exc
        raise
    if not keep_cache:
        for pub_id, sidecar in items:
            clean_verified_window(cache_root, pub_id, sidecar)
    return {"windows": len(items), "uploaded_objects": uploaded,
            "changed_years": len(changed_paths),
            "cleaned_windows": 0 if keep_cache else len(items)}
