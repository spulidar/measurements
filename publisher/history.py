"""Explicit reversible hiding and audited removal of SPU publication days.

Removing a day first saves verified R2 backups, publishes the changed Git
catalog, and only then deletes exact R2 keys. Tombstones block rediscovery.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from .published import load_published_states, write_published_states, validate_year
from .r2_staging import _remote_digest, _make_verified_backup, previous_backup_key


def controls_path(repo: Path, station: str) -> Path:
    if station != "spu":
        raise ValueError("Only the SPU publication-v2 station is supported")
    return repo / "publisher" / "state-v2" / "_controls" / f"{station}.json"


def load_controls(repo: Path, station: str) -> dict[str, Any]:
    path = controls_path(Path(repo), station)
    empty = {"schema_version": 1, "station_id": station, "hidden": [], "removed": {}}
    if not path.exists():
        return empty
    if path.is_symlink() or not path.is_file():
        raise ValueError("Unsafe publication controls file")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict) or set(result) != set(empty):
        raise ValueError("Invalid publication controls structure")
    if result["schema_version"] != 1 or result["station_id"] != station:
        raise ValueError("Invalid publication controls station/schema")
    if not isinstance(result["hidden"], list) or not isinstance(result["removed"], dict):
        raise ValueError("Invalid hidden/removed publication controls")
    for date in [*result["hidden"], *result["removed"]]:
        if not isinstance(date, str) or len(date) != 8 or not date.isdigit():
            raise ValueError("Invalid controlled date")
        datetime.strptime(date, "%Y%m%d")
    if len(set(result["hidden"])) != len(result["hidden"]):
        raise ValueError("Duplicate hidden day")
    if set(result["hidden"]) & set(result["removed"]):
        raise ValueError("A day cannot be both hidden and removed")
    for date, value in result["removed"].items():
        if not isinstance(value, dict) or set(value) != {"day", "r2_bucket"}:
            raise ValueError("Invalid removed-day record")
        if not isinstance(value["r2_bucket"], str) or not value["r2_bucket"]:
            raise ValueError("Removed-day bucket is missing")
        validate_year({
            "schema_version": 2, "station_id": station,
            "year": int(date[:4]), "days": {date: value["day"]},
        }, station, date[:4])
    return result


def save_controls(repo: Path, station: str, value: dict[str, Any]) -> None:
    path = controls_path(repo, station)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Validate exact structure before moving atomic temporary file into place.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        from shutil import copyfile
        # Validation does not read temporary names: perform semantic check here.
        if value["schema_version"] != 1 or value["station_id"] != station:
            raise ValueError("Invalid controls")
        if set(value["hidden"]) & set(value["removed"]):
            raise ValueError("Conflicting controls")
        tmp.replace(path)
        load_controls(repo, station)
    finally:
        if tmp.exists():
            tmp.unlink()


def _assets(day: dict[str, Any]):
    for hour, record in sorted(day["publication_windows"].items()):
        for name, metadata in sorted(record["assets"].items()):
            yield name, metadata


def _verify_backup(client, bucket: str, day: dict[str, Any]) -> None:
    for _, metadata in _assets(day):
        expected = (metadata["size"], metadata["sha256"])
        actual = _remote_digest(client, bucket, metadata["key"])
        if actual != expected:
            raise RuntimeError(f"Refusing removal: R2 object does not match Git state: {metadata['key']}")
        _make_verified_backup(client, bucket, metadata["key"], expected)


def _cleanup_removed(client, bucket: str, day: dict[str, Any]) -> int:
    count = 0
    for _, metadata in _assets(day):
        key = metadata["key"]
        expected = (metadata["size"], metadata["sha256"])
        current = _remote_digest(client, bucket, key)
        if current is None:
            continue
        if current != expected:
            raise RuntimeError(f"Refusing deletion of unexpectedly modified R2 key: {key}")
        backup = previous_backup_key(key, metadata["sha256"])
        if _remote_digest(client, bucket, backup) != expected:
            raise RuntimeError(f"Verified R2 recovery backup is missing: {backup}")
        client.delete_object(Bucket=bucket, Key=key)
        if _remote_digest(client, bucket, key) is not None:
            raise RuntimeError(f"R2 deletion did not complete: {key}")
        count += 1
    return count


def _restore_removed(client, bucket: str, day: dict[str, Any]) -> int:
    restored = 0
    for _, metadata in _assets(day):
        key = metadata["key"]
        original = (metadata["size"], metadata["sha256"])
        current = _remote_digest(client, bucket, key)
        if current == original:
            continue
        if current is not None:
            raise RuntimeError(f"Untracked image exists; refusing restore overwrite: {key}")
        backup = previous_backup_key(key, metadata["sha256"])
        if _remote_digest(client, bucket, backup) != original:
            raise RuntimeError(f"Missing verified R2 backup: {backup}")
        client.copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": backup},
                           Key=key, MetadataDirective="COPY")
        if _remote_digest(client, bucket, key) != original:
            raise RuntimeError(f"R2 restoration verification failed: {key}")
        restored += 1
    return restored


def operate(command: str, date: str, *, confirm: bool = False) -> int:
    from . import pipeline
    pipeline.LOG.setLevel("INFO")
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ctx = pipeline.context()
    controls = load_controls(ctx.repo, ctx.station)
    states = load_published_states(ctx.repo, ctx.station)
    year = date[:4]
    day = states.get(year, {}).get("days", {}).get(date)
    removed = controls["removed"].get(date)

    if command == "remove" and not confirm:
        if day:
            keys = [m["key"] for _, m in _assets(day)]
            pipeline.LOG.info("PREVIEW: remove %s (%d verified R2 objects)", date, len(keys))
            for key in keys:
                pipeline.LOG.info("Candidate: %s", key)
        elif removed:
            pipeline.LOG.info("Day %s is already removed; --confirm retries pending R2 cleanup", date)
        else:
            pipeline.LOG.info("Day %s is not published", date)
        pipeline.LOG.info("Nothing changed. Use remove DATE --confirm for deletion.")
        return 0

    pipeline.checkout_ready(ctx.repo)
    settings = pipeline.secrets(ctx, require_r2=command == "remove" or (command == "restore" and bool(removed)))
    if command == "hide":
        if removed:
            raise RuntimeError("Removed day cannot be hidden; restore it first")
        if not day:
            raise RuntimeError(f"No published day to hide: {date}")
        if date in controls["hidden"]:
            pipeline.LOG.info("Day %s is already hidden", date)
            return 0
        controls["hidden"].append(date)
        controls["hidden"].sort()
        save_controls(ctx.repo, ctx.station, controls)
        pipeline.commit_site(ctx, settings, allow_empty=True)
        pipeline.LOG.info("Hidden %s without deleting R2", date)
        return 0

    if command == "remove":
        client = pipeline.r2_client(settings)
        if removed:
            if removed["r2_bucket"] != settings["R2_BUCKET_NAME"]:
                raise RuntimeError("Removed day belongs to a different R2 bucket")
            count = _cleanup_removed(client, settings["R2_BUCKET_NAME"], removed["day"])
            pipeline.LOG.info("Retried removal: %d objects deleted", count)
            return 0
        if not day:
            raise RuntimeError(f"No published day to remove: {date}")
        bucket = settings["R2_BUCKET_NAME"]
        buckets = {r["r2_bucket"] for r in day["publication_windows"].values()}
        if buckets != {bucket}:
            raise RuntimeError("Stored publication belongs to another bucket")
        _verify_backup(client, bucket, day)
        updated = deepcopy(states)
        updated[year]["days"].pop(date)
        write_published_states(ctx.repo, ctx.station, states, updated)
        controls["hidden"] = [value for value in controls["hidden"] if value != date]
        controls["removed"][date] = {"day": deepcopy(day), "r2_bucket": bucket}
        save_controls(ctx.repo, ctx.station, controls)
        # HTML/calendars are removed from Git before deleting their remote assets.
        pipeline.commit_site(ctx, settings, allow_empty=True)
        count = _cleanup_removed(client, bucket, day)
        pipeline.LOG.info("Removed %s: %d R2 objects; verified backup retained", date, count)
        return 0

    if command == "restore":
        if removed:
            client = pipeline.r2_client(settings)
            bucket = removed["r2_bucket"]
            if bucket != settings["R2_BUCKET_NAME"]:
                raise RuntimeError("Cannot restore from another R2 bucket")
            count = _restore_removed(client, bucket, removed["day"])
            updated = deepcopy(states)
            current = updated.setdefault(year, {
                "schema_version": 2, "station_id": ctx.station,
                "year": int(year), "days": {},
            })
            if date in current["days"]:
                raise RuntimeError("Restore collides with an already-published day")
            current["days"][date] = deepcopy(removed["day"])
            write_published_states(ctx.repo, ctx.station, states, updated)
            controls["removed"].pop(date)
            save_controls(ctx.repo, ctx.station, controls)
            pipeline.commit_site(ctx, settings)
            pipeline.LOG.info("Restored %s: %d WEBPs reuploaded", date, count)
            return 0
        if date not in controls["hidden"]:
            raise RuntimeError(f"No hidden/removed day to restore: {date}")
        controls["hidden"].remove(date)
        save_controls(ctx.repo, ctx.station, controls)
        pipeline.commit_site(ctx, settings)
        pipeline.LOG.info("Unhidden %s", date)
        return 0
    raise ValueError(f"Unknown maintenance command: {command}")
