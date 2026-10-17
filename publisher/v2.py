"""Publication-v2 staging catalog and schema-v2 local state.

This module has intentionally no boto3/cloud, subprocess, site-HTML or Git
dependencies. It accepts *only* attested products below .cache/measurements.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from .windows import PUBLICATION_WINDOW_STARTS

_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_STATION = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ASSET = re.compile(
    r"^rcs_(?P<id>\d{8}_[a-z0-9][a-z0-9-]*_(?:00|06|12|18))_"
    r"\d+nm_[A-Za-z0-9-]+_\d+(?:\.\d+)?km\.webp$"
)


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _list_of_strings(payload: dict[str, Any], key: str, *, nonempty: bool = False) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"Invalid or missing sidecar field {key!r}")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"Sidecar {key} must contain nonempty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"Duplicate entries in sidecar {key}")
    return value


def _validated_sidecar(path: Path, root: Path, station: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unreadable website staging sidecar: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Sidecar must be a JSON object: {path}")

    pub_id = payload.get("publication_window_id")
    if not isinstance(pub_id, str):
        raise ValueError(f"Missing publication_window_id: {path}")
    pieces = pub_id.split("_")
    if len(pieces) != 3 or pieces[1] != station or pieces[2] not in PUBLICATION_WINDOW_STARTS:
        raise ValueError(f"Invalid station or window identity in {path}")
    date = pieces[0]
    if len(date) != 8 or not date.isdigit():
        raise ValueError(f"Invalid local date in {path}")
    try:
        datetime.strptime(date, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"Invalid civil date in {path}") from exc
    expected = root / station / date[:4] / date[4:6] / date / f"{pub_id}.json"
    if path.is_symlink() or path.resolve() != expected.resolve() or path != expected:
        raise ValueError(f"Sidecar outside expected publication day/path: {path}")

    source_sessions = _list_of_strings(payload, "source_sessions", nonempty=True)
    l1_files = _list_of_strings(payload, "source_level1_files", nonempty=True)
    l2_files = _list_of_strings(payload, "source_level2_files")
    if len(source_sessions) != len(l1_files):
        raise ValueError(f"Source session/L1 provenance mismatch: {path}")
    # Scientific identity parsing is owned by MILGRAU.
    from milgrau.io.paths import session_id_parts
    for session_id, filename in zip(source_sessions, l1_files):
        parsed_station, _start, _end = session_id_parts(session_id)
        if parsed_station != station or filename != f"{session_id}_L1.nc":
            raise ValueError(f"Scientific provenance mismatch in {path}: {session_id}")
    expected_l2_files = {f"{sid}_L2.nc" for sid in source_sessions}
    if any(item not in expected_l2_files for item in l2_files):
        raise ValueError(f"L2 source does not belong to an attested scientific session: {path}")

    for key in ("scientific_sessions_merged", "scientific_netcdf_split"):
        if payload.get(key) is not False:
            raise ValueError(f"{key} must be false in {path}")
    if payload.get("visualization_only_session_concatenation") is not (len(source_sessions) > 1):
        raise ValueError(f"Incorrect visual-concatenation flag in {path}")
    fingerprint = payload.get("source_fingerprint")
    if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
        raise ValueError(f"Missing or invalid source_fingerprint in {path}")

    assets = _list_of_strings(payload, "assets", nonempty=True)
    hashes = payload.get("asset_hashes")
    if not isinstance(hashes, dict) or set(hashes) != set(assets):
        raise ValueError(f"Sidecar assets and asset_hashes disagree: {path}")
    for name in assets:
        if Path(name).name != name or not (match := _ASSET.fullmatch(name)):
            raise ValueError(f"Noncanonical staged asset name {name!r}: {path}")
        if match.group("id") != pub_id:
            raise ValueError(f"Asset belongs to a different publication window: {name}")
        asset = path.parent / name
        if asset.is_symlink() or not asset.is_file() or asset.parent != expected.parent:
            raise ValueError(f"Asset missing/outside expected day directory: {asset}")
        if not isinstance(hashes[name], str) or not _FINGERPRINT.fullmatch(hashes[name]):
            raise ValueError(f"Invalid asset digest for {name}")
        if _hash_file(asset) != hashes[name]:
            raise ValueError(f"Staged asset modified after rendering: {asset}")
    return payload


def collect_validated_staging(site_products_root: str | Path, station_id: str) -> dict[str, dict[str, Any]]:
    """Ignore unlisted orphan WEBPs; reject broken sidecars rather than publish them."""
    root = Path(site_products_root).resolve()
    station = station_id.strip().lower()
    if not _STATION.fullmatch(station):
        raise ValueError(f"Invalid MILGRAU station ID {station_id!r}")
    catalog: dict[str, dict[str, Any]] = {}
    station_root = root / station
    if not station_root.exists():
        return catalog
    for path in sorted(station_root.rglob("*.json")):
        # A sidecar must be directly inside its canonical day folder.
        record = _validated_sidecar(path, root, station)
        pub_id = record["publication_window_id"]
        if pub_id in catalog:
            raise ValueError(f"Duplicate publication window sidecar: {pub_id}")
        catalog[pub_id] = record
    return catalog


def build_state_v2(
    catalog: dict[str, dict[str, Any]], station_id: str
) -> dict[str, dict[str, Any]]:
    """Build clean per-year public view metadata; no scientific merge or upload."""
    years: dict[str, dict[str, Any]] = {}
    for pub_id, sidecar in sorted(catalog.items()):
        date, station, window = pub_id.split("_")
        if station != station_id or window not in PUBLICATION_WINDOW_STARTS:
            raise ValueError(f"Catalog contains foreign station/window {pub_id}")
        year = date[:4]
        state = years.setdefault(
            year, {"schema_version": 2, "station_id": station_id, "year": int(year), "days": {}}
        )
        day = state["days"].setdefault(date, {"publication_windows": {}})
        key_prefix = f"{station}/{year}/{date[4:6]}/{date}/"
        day["publication_windows"][window] = {
            "publication_window_id": pub_id,
            "source_sessions": list(sidecar["source_sessions"]),
            "source_level1_files": list(sidecar["source_level1_files"]),
            "source_level2_files": list(sidecar["source_level2_files"]),
            "source_fingerprint": sidecar["source_fingerprint"],
            "visualization_only_session_concatenation": sidecar[
                "visualization_only_session_concatenation"
            ],
            "assets": {name: key_prefix + name for name in sidecar["assets"]},
        }
    return years


def write_local_state_v2(
    site_products_root: str | Path, station_id: str, years: dict[str, dict[str, Any]]
) -> list[Path]:
    """Only stage local v2 state below .cache/measurements/_state-v2/station/."""
    output_root = Path(site_products_root).resolve() / "_state-v2" / station_id
    output_root.mkdir(parents=True, exist_ok=True)
    written = []
    for year, payload in sorted(years.items()):
        if len(year) != 4 or not year.isdigit() or payload["station_id"] != station_id:
            raise ValueError(f"Invalid staged state year/station: {year}")
        target = output_root / f"{year}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(target)
        written.append(target)
    return written
