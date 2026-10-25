"""Persistent publication-v2 state: Git-tracked, never reconstructed from cache.

Only verified R2 objects may be recorded here. No historical schema-v1 import.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import re
from typing import Any

from .v2 import _ASSET, _FINGERPRINT
from .windows import PUBLICATION_WINDOW_STARTS

_STATION = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_YEAR = re.compile(r"^\d{4}$")
_DATE = re.compile(r"^\d{8}$")
_SAFE_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{1,253}$")
PUBLICATION_PREFIX = "spu"


def publication_object_key(pub_id: str, digest: str, filename: str) -> str:
    """Stable daily WEBP key, with SHA-256 retained in the Git state."""
    match = _ASSET.fullmatch(filename) if isinstance(filename, str) else None
    if match is None or match.group("id") != pub_id:
        raise ValueError(f"Invalid publication asset identity: {filename!r}")
    if not isinstance(digest, str) or not _FINGERPRINT.fullmatch(digest):
        raise ValueError("Invalid SHA-256 for publication asset")
    date, station, hour = pub_id.split("_")
    if station != PUBLICATION_PREFIX or hour not in PUBLICATION_WINDOW_STARTS:
        raise ValueError("Only spu publication objects are allowed in this namespace")
    try:
        datetime.strptime(date, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"Invalid publication asset date: {date!r}") from exc
    return "/".join((station, date[:4], date[4:6], date, filename))


def validate_publication_object(metadata: dict[str, Any]) -> None:
    """Protect R2 writes even when the target bucket also hosts legacy objects."""
    if not isinstance(metadata, dict):
        raise ValueError("Invalid publication object metadata")
    key, digest, size = metadata.get("key"), metadata.get("sha256"), metadata.get("size")
    if not isinstance(key, str):
        raise ValueError("Invalid publication object key")
    filename = key.rsplit("/", 1)[-1]
    match = _ASSET.fullmatch(filename)
    if match is None:
        raise ValueError("R2 object outside canonical spu/YYYY/MM/YYYYMMDD namespace")
    expected = publication_object_key(match.group("id"), digest, filename)
    if key != expected:
        raise ValueError("R2 object outside canonical spu/YYYY/MM/YYYYMMDD namespace")
    if type(size) is not int or size < 1:
        raise ValueError("Invalid publication object size")



def state_root(repository: str | Path, station: str) -> Path:
    if not _STATION.fullmatch(station):
        raise ValueError(f"Invalid station id: {station!r}")
    return Path(repository).resolve() / "publisher" / "state-v2" / station


def empty_year(station: str, year: str) -> dict[str, Any]:
    if not _STATION.fullmatch(station) or not _YEAR.fullmatch(year):
        raise ValueError("Invalid year/station")
    return {"schema_version": 2, "station_id": station, "year": int(year), "days": {}}


def validate_year(payload: dict[str, Any], station: str, year: str) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        raise ValueError(f"Invalid persistent publication v2 state for {year}")
    if payload.get("station_id") != station or payload.get("year") != int(year):
        raise ValueError(f"Persistent state station/year mismatch for {year}")
    days = payload.get("days")
    if not isinstance(days, dict):
        raise ValueError(f"Missing days in persistent state {year}")
    for date, day in days.items():
        if not _DATE.fullmatch(date) or date[:4] != year:
            raise ValueError(f"Invalid day {date!r} in state {year}")
        datetime.strptime(date, "%Y%m%d")
        if set(day) != {"publication_windows"} or not isinstance(day["publication_windows"], dict):
            raise ValueError(f"Unexpected day content in {date}")
        for hour, record in day["publication_windows"].items():
            pub_id = f"{date}_{station}_{hour}"
            if hour not in PUBLICATION_WINDOW_STARTS or record.get("publication_window_id") != pub_id:
                raise ValueError(f"Invalid publication window {pub_id}")
            if record.get("source_fingerprint") is None or not _FINGERPRINT.fullmatch(
                str(record["source_fingerprint"])
            ):
                raise ValueError(f"Missing source fingerprint in {pub_id}")
            if record.get("scientific_sessions_merged") is not False or record.get("scientific_netcdf_split") is not False:
                raise ValueError(f"Science boundaries modified in {pub_id}")
            sessions = record.get("source_sessions")
            if not isinstance(sessions, list) or not sessions:
                raise ValueError(f"Missing scientific provenance in {pub_id}")
            if not isinstance(record.get("source_level1_files"), list) or not isinstance(record.get("source_level2_files"), list):
                raise ValueError(f"Invalid L1/L2 provenance in {pub_id}")
            bucket = record.get("r2_bucket")
            if not isinstance(bucket, str) or not _SAFE_BUCKET.fullmatch(bucket):
                raise ValueError(f"Invalid storage bucket in {pub_id}")
            assets = record.get("assets")
            if not isinstance(assets, dict) or not assets:
                raise ValueError(f"Missing verified assets in {pub_id}")
            for filename, metadata in assets.items():
                match = _ASSET.fullmatch(filename)
                if not match or match.group("id") != pub_id or not isinstance(metadata, dict):
                    raise ValueError(f"Invalid asset in {pub_id}")
                digest = metadata.get("sha256")
                key = metadata.get("key")
                size = metadata.get("size")
                if not isinstance(digest, str) or not _FINGERPRINT.fullmatch(digest):
                    raise ValueError(f"Invalid asset SHA-256 in {pub_id}")
                try:
                    validate_publication_object(metadata)
                except ValueError as exc:
                    raise ValueError(f"Invalid asset key or metadata in {pub_id}: {exc}") from exc
                expected_key = publication_object_key(pub_id, digest, filename)
                if key != expected_key:
                    raise ValueError(f"Asset key outside canonical spu namespace: {pub_id}")


def load_published_states(repository: str | Path, station: str) -> dict[str, dict[str, Any]]:
    directory = state_root(repository, station)
    result: dict[str, dict[str, Any]] = {}
    if not directory.is_dir():
        return result
    for path in sorted(directory.glob("*.json")):
        if not _YEAR.fullmatch(path.stem) or path.is_symlink():
            raise ValueError(f"Unexpected persistent state file: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid persistent state JSON: {path}") from exc
        validate_year(payload, station, path.stem)
        result[path.stem] = payload
    return result


def window_record(pub_id: str, sidecar: dict[str, Any], root: Path, bucket: str) -> dict[str, Any]:
    date, station, hour = pub_id.split("_")
    if not _SAFE_BUCKET.fullmatch(bucket) or sidecar["publication_window_id"] != pub_id:
        raise ValueError("Invalid target bucket or window identity")
    day_dir = root / station / date[:4] / date[4:6] / date
    assets: dict[str, Any] = {}
    for filename in sidecar["assets"]:
        digest = sidecar["asset_hashes"][filename]
        size = (day_dir / filename).stat().st_size
        if size <= 0:
            raise ValueError(f"Refusing to publish empty asset {filename}")
        key = publication_object_key(pub_id, digest, filename)
        assets[filename] = {"key": key, "sha256": digest, "size": size}
    return {
        "publication_window_id": pub_id,
        "source_fingerprint": sidecar["source_fingerprint"],
        "source_sessions": list(sidecar["source_sessions"]),
        "source_level1_files": list(sidecar["source_level1_files"]),
        "source_level2_files": list(sidecar["source_level2_files"]),
        "visualization_only_session_concatenation": sidecar["visualization_only_session_concatenation"],
        "scientific_sessions_merged": False,
        "scientific_netcdf_split": False,
        "r2_bucket": bucket,
        "assets": assets,
    }


def update_published_states(
    existing: dict[str, dict[str, Any]], station: str, records: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Copy-on-write incremental update; previously published days survive cache removal."""
    from copy import deepcopy
    result = deepcopy(existing)
    for pub_id, record in sorted(records.items()):
        date, owner, hour = pub_id.split("_")
        if owner != station or hour not in PUBLICATION_WINDOW_STARTS:
            raise ValueError(f"Incorrect window identity: {pub_id}")
        year = date[:4]
        current = result.setdefault(year, empty_year(station, year))
        day = current["days"].setdefault(date, {"publication_windows": {}})
        day["publication_windows"][hour] = record
    for year, state in result.items():
        validate_year(state, station, year)
    return result


def write_published_states(
    repository: str | Path, station: str,
    original: dict[str, dict[str, Any]], updated: dict[str, dict[str, Any]]
) -> list[Path]:
    """Atomically write only changed, validated state-v2 years in the Git checkout."""
    output = state_root(repository, station)
    changed = [year for year in sorted(updated) if original.get(year) != updated[year]]
    for year in changed:
        validate_year(updated[year], station, year)
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    for year in changed:
        target = output / f"{year}.json"
        if target.is_symlink():
            raise ValueError(f"State file is a symlink: {target}")
        tmp = output / f".{year}.json.tmp"
        if tmp.is_symlink():
            raise ValueError(f"Temporary state is a symlink: {tmp}")
        tmp.write_text(json.dumps(updated[year], indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(target)
        paths.append(target)
    return paths
