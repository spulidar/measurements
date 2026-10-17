"""Read-only local preview builder for the publication-v2 state.

The browser reads a generated manifest made *only* from validated state-v2.
It never reads science NetCDF, uploads objects or edits the historical site.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Any

from .v2 import build_state_v2, collect_validated_staging

PREVIEW_FOLDER = "_preview-v2"
ASSET_FILES = ("index.html", "app.js", "styles.css")


def preview_manifest(site_products_root: str | Path, station_id: str) -> dict[str, Any]:
    """Reject stale/tampered state and assets before exposing any preview entries."""
    root = Path(site_products_root).resolve()
    station = station_id.strip().lower()
    catalog = collect_validated_staging(root, station)
    expected = build_state_v2(catalog, station)
    state_dir = root / "_state-v2" / station
    stored_years = {
        path.stem: path
        for path in state_dir.glob("*.json")
    } if state_dir.is_dir() else {}
    if set(stored_years) != set(expected):
        raise ValueError(
            "Local state-v2 years disagree with validated staging. "
            "Run update-site.py --stage-only again before building the preview."
        )
    days: dict[str, Any] = {}
    for year, expected_payload in sorted(expected.items()):
        path = stored_years[year]
        if path.is_symlink():
            raise ValueError(f"Symlink state file is not permitted: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Unreadable v2 state file: {path}") from exc
        if payload != expected_payload:
            raise ValueError(
                f"Stale or modified state-v2 file: {path}. "
                "Re-run update-site.py --stage-only."
            )
        days.update(payload["days"])
    return {
        "schema_version": 2,
        "station_id": station,
        "years": sorted(expected, reverse=True),
        "days": days,
    }


def build_preview(site_products_root: str | Path, station_id: str) -> tuple[Path, int]:
    """Write a self-contained HTTP-served preview only under .cache/measurements."""
    root = Path(site_products_root).resolve()
    manifest = preview_manifest(root, station_id)
    output = root / PREVIEW_FOLDER
    if output.is_symlink():
        raise ValueError(f"Refusing symlink preview output directory: {output}")
    if output.exists() and not output.is_dir():
        raise ValueError(f"Preview output is not a directory: {output}")

    assets = Path(__file__).resolve().parent / "preview_assets"
    for filename in ASSET_FILES:
        if not (assets / filename).is_file():
            raise FileNotFoundError(f"Missing preview template asset: {filename}")
        if (output / filename).is_symlink():
            raise ValueError(f"Refusing symlink preview target: {output / filename}")
    if (output / "manifest.json").is_symlink():
        raise ValueError(f"Refusing symlink preview manifest: {output / 'manifest.json'}")

    output.mkdir(parents=True, exist_ok=True)
    for filename in ASSET_FILES:
        shutil.copyfile(assets / filename, output / filename)
    target = output / "manifest.json"
    temporary = output / ".manifest.json.tmp"
    if temporary.is_symlink():
        raise ValueError(f"Refusing symlink temporary manifest: {temporary}")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return output / "index.html", len(manifest["days"])
