#!/usr/bin/env python3
"""Explicit isolated R2 staging publisher (never production or site cutover)."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from publisher.publication import publish_cache
from publisher.r2_staging import r2_client_from_environment
from publisher.v2 import collect_validated_staging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage cached L1 WEBPs to R2 staging; persist verified state to GitHub and clean cache."
    )
    parser.add_argument("--milgrau-config", type=Path)
    parser.add_argument("--staging-bucket", required=True,
                        help="Explicit Cloudflare R2 bucket; objects use staging/publication-v2/ only.")
    parser.add_argument("--confirm-staging-upload", action="store_true",
                        help="Required for any R2 write (NEVER a production release).")
    parser.add_argument("--batch-size", type=int, default=12,
                        help="Maximum publication windows per run (default 12).")
    parser.add_argument("--keep-cache", action="store_true",
                        help="Retain locally staged products after verified GitHub persistence.")
    parser.add_argument("--dry-run", action="store_true",
                        help="List candidate windows; do not contact R2/Git or change files.")
    args = parser.parse_args(argv)

    from milgrau.config.loader import load_config
    from milgrau.io.paths import station_id, processed_data_root, resolve_project_path

    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    candidate = args.milgrau_config or os.environ.get("MILGRAU_CONFIG")
    if candidate is None:
        candidate = Path.cwd() / "config.yaml"
        if not candidate.is_file():
            candidate = Path(__file__).resolve().parent.parent / "config.yaml"
    config_file = Path(candidate).expanduser().resolve()
    if not config_file.is_file():
        parser.error(f"MILGRAU configuration does not exist: {config_file}")
    config = load_config(config_file)
    workspace = Path(config["_config_file"]).parent
    scientific = processed_data_root(config, root_dir=workspace).resolve()
    cached = resolve_project_path(".cache/measurements", root_dir=workspace).resolve()
    repo = Path(__file__).resolve().parent
    if cached == scientific or cached.is_relative_to(scientific) or scientific.is_relative_to(cached):
        raise RuntimeError("Measurement cache must not overlap scientific products")
    if repo == cached or repo.is_relative_to(cached) or cached.is_relative_to(repo):
        raise RuntimeError("Measurement cache must not overlap Git repository")
    station = station_id(config)
    log = logging.getLogger("measurements-v2")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.dry_run:
        catalog = collect_validated_staging(cached, station)
        for pub_id in sorted(catalog)[:args.batch_size]:
            log.info("[DRY-RUN] would publish %s: %d WEBPs", pub_id, len(catalog[pub_id]["assets"]))
        log.info("[DRY-RUN] %d available windows; 0 R2/Git/cache mutations", len(catalog))
        return 0
    if not args.confirm_staging_upload:
        parser.error("R2 upload requires --confirm-staging-upload; use --dry-run first.")
    # Credentials are requested only after the explicit confirmation flag.
    client = r2_client_from_environment()
    result = publish_cache(
        cached, station, repo, args.staging_bucket, client,
        max_windows=args.batch_size, keep_cache=args.keep_cache, logger=log,
    )
    log.info(
        "R2 staging batch complete: %(windows)d windows, %(uploaded_objects)d new objects, "
        "%(changed_years)d state years changed, %(cleaned_windows)d local windows cleaned",
        result,
    )
    log.info("Git branch publication-v2 only. No production website or R2 objects changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
