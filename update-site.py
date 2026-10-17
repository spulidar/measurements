#!/usr/bin/env python3
"""Publication-v2 local staging entrypoint.

Deliberately stage-only. No R2 client, upload/delete, Git push, live HTML,
production publisher/state directory or production manifest can be reached.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from publisher.staging import stage_publication_products
from publisher.v2 import build_state_v2, collect_validated_staging, write_local_state_v2
from publisher.windows import discover_scientific_sessions


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publication v2: safe local staging only.")
    parser.add_argument(
        "--stage-only", action="store_true", required=True,
        help="Mandatory safety gate: build 03-site-products only (never publish).",
    )
    parser.add_argument(
        "--milgrau-config", type=Path,
        help="Official MILGRAU config.yaml (defaults to workspace/config.yaml).",
    )
    dates = parser.add_mutually_exclusive_group()
    dates.add_argument("--publish-date", metavar="YYYYMMDD")
    dates.add_argument("--publish-year", metavar="YYYY")
    parser.add_argument("--force-site-products", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Read and plan only; write nothing.")
    args = parser.parse_args(argv)
    if args.publish_year and (len(args.publish_year) != 4 or not args.publish_year.isdigit()):
        parser.error("--publish-year must be YYYY")
    if args.publish_date:
        try:
            datetime.strptime(args.publish_date, "%Y%m%d")
        except ValueError:
            parser.error("--publish-date must be a real YYYYMMDD civil date")
    return args


def resolve_config_path(args: argparse.Namespace) -> Path:
    """Find workspace configuration without pointing at milgrau/config.yaml."""
    candidate = args.milgrau_config or os.environ.get("MILGRAU_CONFIG")
    if candidate is None:
        workspace_root = Path(__file__).resolve().parent.parent
        candidate = Path.cwd() / "config.yaml"
        if not candidate.is_file():
            candidate = workspace_root / "config.yaml"
    path = Path(candidate).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(
            f"MILGRAU config.yaml not found: {path}. Pass --milgrau-config "
            "or configure the shared workspace root."
        )
    return path


def resolve_visualization_root(config: dict, workspace_root: Path) -> Path:
    """Use MILGRAU's official logo resolver; support workspace and package assets."""
    from milgrau.viz import style as milgrau_style

    logos = milgrau_style.configured_logos(config)
    if not logos:
        return workspace_root
    # An editable checkout can expose 'milgrau' as a namespace package with
    # __file__ = None. Anchor on the actual installed renderer module instead.
    package_root = Path(milgrau_style.__file__).resolve().parents[2]
    candidates = (workspace_root, package_root)
    for root in candidates:
        if all(milgrau_style.resolve_logo_path(root, name).is_file() for name, _height in logos):
            return root
    raise FileNotFoundError(
        "Configured MILGRAU logos are not all resolvable from either the "
        f"workspace root ({workspace_root}) or installed MILGRAU root "
        f"({package_root}). Fix visualization.logos in the MILGRAU config."
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        from milgrau.config.loader import load_config
        from milgrau.io.paths import processed_data_root, resolve_project_path, station_id
        from milgrau.viz.config import resolve_visualization_config
    except ImportError as exc:
        raise RuntimeError(
            "MILGRAU is required in the shared environment; "
            "install its current checkout using pip install -e '.[web]'."
        ) from exc

    config_path = resolve_config_path(args)
    config = load_config(config_path)
    root = Path(config["_config_file"]).parent
    station = station_id(config)
    station_catalog = config["_station_catalog"]["station"]
    timezone_name = str(station_catalog.get("timezone", "")).strip()
    if not timezone_name:
        raise ValueError("Missing MILGRAU station.timezone")
    ZoneInfo(timezone_name)
    visual = resolve_visualization_config(config)
    if visual.output_format != "webp":
        raise ValueError("MILGRAU visualization.output_format must be webp for publication v2.")

    scientific_root = processed_data_root(config, root_dir=root)
    site_products = resolve_project_path("03-site-products", root_dir=root).resolve()
    if scientific_root.resolve() == site_products or (
        site_products.is_relative_to(scientific_root.resolve())
        or scientific_root.resolve().is_relative_to(site_products)
    ):
        raise RuntimeError("Publication products must not overlap the scientific data tree.")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger = logging.getLogger("publication-v2")
    visual_root = resolve_visualization_root(config, root)
    sessions = discover_scientific_sessions(scientific_root, station)
    logger.info("MILGRAU %s: %d canonical scientific sessions", station, len(sessions))
    results = stage_publication_products(
        sessions, site_products_root=site_products, station_id=station,
        timezone_name=timezone_name, milgrau_config=config,
        milgrau_config_path=config_path, milgrau_root=visual_root,
        publication_settings={"schema_version": 2, "mode": "stage-only"},
        publish_year=args.publish_year, publish_date=args.publish_date,
        force=args.force_site_products, dry_run=args.dry_run, logger=logger,
    )
    logger.info(
        "Publication views: %d rendered, %d cached, %d without L1 samples",
        sum(result.rendered for result in results),
        sum(result.skipped and not result.no_data for result in results),
        sum(result.no_data for result in results),
    )
    if args.dry_run:
        return 0

    # Only local schema-v2 metadata under 03-site-products; never site production state.
    catalog = collect_validated_staging(site_products, station)
    years = build_state_v2(catalog, station)
    paths = write_local_state_v2(site_products, station, years)
    logger.info(
        "Validated %d windows across %d local state file(s) under %s",
        len(catalog), len(paths), site_products / "_state-v2" / station,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
