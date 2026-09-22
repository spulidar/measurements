"""SPU lidar static-site publisher.

Canonical quicklooks are grouped by civil day and fixed six-hour period. Existing
saam/sapm/sant dashboards remain indexed as migration fallbacks until the
historical archive is reprocessed.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from datetime import datetime

import yaml

from publisher.catalog import collect_canonical_assets, discover_legacy_dashboards, merge_catalogs
from publisher.publish import cloud_key, get_cloud_credentials, get_cloud_existing_keys, push_site_updates, upload_to_r2
from publisher.render import day_output_path, read_dashboard_fingerprint, render_day_dashboard, write_manifest


def load_config(config_path: str = "config.yaml") -> dict:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise RuntimeError(f"Configuration file is empty or invalid: {config_path}")
    for section in ("directories", "processing"):
        if section not in config:
            raise KeyError(f"Configuration file is missing required section: {section}")
    return config


def setup_logger(module_name: str, log_dir: str = "logs") -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_filename = Path(log_dir) / f"{module_name}_run_{datetime.now().strftime('%Y%m%d')}.log"
    logger = logging.getLogger(module_name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(log_filename, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MILGRAU web publisher: R2 assets + daily GitHub Pages dashboards.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml.")
    parser.add_argument("--html-only", action="store_true", help="Rebuild manifest/dashboards without uploading images.")
    parser.add_argument("--sync-missing-uploads", action="store_true", help="Upload canonical images missing from R2.")
    parser.add_argument("--no-push", action="store_true", help="Do not commit/push the measurements repository.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write, upload, commit, or push.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    logger = setup_logger("update-site", config["directories"].get("log_dir", "logs"))

    root_dir = Path.cwd().parent
    data_root = root_dir / config["directories"]["processed_data"]
    site_root = root_dir / config["directories"].get("site_output", "measurements")
    if not args.dry_run:
        site_root.mkdir(parents=True, exist_ok=True)

    logger.info("Discovering canonical quicklooks and legacy dashboards...")
    all_images = sorted(data_root.rglob("*.webp")) if data_root.exists() else []
    canonical = collect_canonical_assets(all_images)
    legacy = discover_legacy_dashboards(site_root)
    catalog = merge_catalogs(legacy, canonical)
    logger.info(
        "Catalog: %d civil days, %d with canonical six-hour products.",
        len(catalog.days),
        len(catalog.canonical_days),
    )

    # The calendar is a static client of this deterministic manifest.
    write_manifest(catalog, site_root, dry_run=args.dry_run)

    public_url = str(config.get("publication", {}).get("cloud_public_url", "")).rstrip("/")
    cloud = None
    cloud_existing: set[str] = set()
    if catalog.canonical_days and not args.html_only:
        cloud = get_cloud_credentials(logger)
        if not public_url:
            public_url = cloud[2]
        if args.sync_missing_uploads:
            cloud_existing = get_cloud_existing_keys(cloud[0], cloud[1], logger)
    elif catalog.canonical_days and not public_url:
        # Backward-compatible fallback for older local configs.
        cloud = get_cloud_credentials(logger)
        public_url = cloud[2]

    rendered = 0
    uploaded = 0
    skipped = 0

    for day in catalog.site_days:
        output_path = day_output_path(site_root, day)
        current_fp = read_dashboard_fingerprint(output_path)
        new_fp = day.fingerprint()
        changed = current_fp != new_fp
        should_render = args.html_only or args.sync_missing_uploads or changed or not bool(config["processing"].get("incremental", False))

        if cloud is not None and not args.html_only:
            client, bucket, public_url = cloud
            should_upload_day = changed or not bool(config["processing"].get("incremental", False)) or args.sync_missing_uploads
            if should_upload_day:
                for period in day.canonical.values():
                    for asset in period.assets:
                        key = cloud_key(day.date, asset.filename)
                        if args.sync_missing_uploads and key in cloud_existing:
                            continue
                        if args.dry_run:
                            logger.info("[DRY-RUN] upload %s -> %s", asset.filename, key)
                        elif upload_to_r2(client, bucket, asset.path, key, logger):
                            uploaded += 1
                        cloud_existing.add(key)

        if should_render:
            logger.info("Rendering daily dashboard: %s", output_path)
            render_day_dashboard(day, site_root, public_url, dry_run=args.dry_run)
            rendered += 1
        else:
            skipped += 1

    logger.info("Finished: %d dashboards rendered, %d assets uploaded, %d unchanged days skipped.", rendered, uploaded, skipped)
    push_site_updates(site_root, logger, no_push=args.no_push, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
