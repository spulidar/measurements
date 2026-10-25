#!/usr/bin/env python3
"""Generate the unchanged-layout website from persistent Git-owned state-v2."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from publisher.site_v2 import build_unchanged_frontend
from publisher.site_export import export_site_to_branch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the unchanged measurements frontend from verified Git state-v2."
    )
    parser.add_argument("--milgrau-config", type=Path)
    parser.add_argument("--r2-public-base", required=True,
                        help="HTTPS public base for verified R2 staging objects.")
    parser.add_argument(
        "--write-branch", action="store_true",
        help="After building locally, copy validated pages and measurements.json "
             "to the CLEAN publication-v2 Git working tree. No commit or push.",
    )
    args = parser.parse_args(argv)
    from milgrau.config.loader import load_config
    from milgrau.io.paths import resolve_project_path, station_id

    candidate = args.milgrau_config or os.environ.get("MILGRAU_CONFIG")
    if candidate is None:
        candidate = Path.cwd() / "config.yaml"
        if not candidate.is_file():
            candidate = Path(__file__).resolve().parent.parent / "config.yaml"
    config_file = Path(candidate).expanduser().resolve()
    if not config_file.is_file():
        parser.error(f"MILGRAU config.yaml not found: {config_file}")
    config = load_config(config_file)
    workspace = Path(config["_config_file"]).parent
    cache = resolve_project_path(".cache/measurements", root_dir=workspace)
    path, days = build_unchanged_frontend(
        Path(__file__).resolve().parent,
        cache, station_id(config),
        r2_public_base=args.r2_public_base,
    )
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.info("Built %d days with the EXISTING site layout: %s", days, path)
    logging.info("Serve locally: python -m http.server 8765 --bind 127.0.0.1 --directory %s", path)
    logging.info("Open http://127.0.0.1:8765/ql-measurement-calendar.html")
    if args.write_branch:
        pages, retired = export_site_to_branch(
            Path(__file__).resolve().parent, path, station_id(config),
        )
        logging.info(
            "Copied %d verified daily HTMLs and measurements.json to publication-v2; "
            "removed %d retired generated pages", pages, retired,
        )
        logging.info(
            "No Git push or GitHub Pages deployment occurred. Review the site, "
            "then git add/commit/push the generated HTML and manifest."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
