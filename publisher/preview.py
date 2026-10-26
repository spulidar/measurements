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
    """Build a read-only local preview from validated WEBPs and sidecars.

    The old, duplicated _state-v2 cache has been retired. Every referenced
    asset is attested by its sidecar and independently SHA-256 verified by
    collect_validated_staging. This works immediately after 'images DATE'.
    """
    root = Path(site_products_root).resolve()
    station = station_id.strip().lower()
    catalog = collect_validated_staging(root, station)
    years = build_state_v2(catalog, station)
    days: dict[str, Any] = {}
    for _year, payload in sorted(years.items()):
        days.update(payload["days"])
    return {
        "schema_version": 2,
        "station_id": station,
        "years": sorted(years, reverse=True),
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
