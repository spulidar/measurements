#!/usr/bin/env python3
"""Create a local-only measurements v2 website preview from validated staging.

This tool never processes science files, invokes Git, or contacts R2.
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from publisher.preview import build_preview


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build an isolated static preview under .cache/measurements/_preview-v2."
    )
    parser.add_argument("--milgrau-config", type=Path, default=None)
    args = parser.parse_args(argv)

    from milgrau.config.loader import load_config
    from milgrau.io.paths import processed_data_root, resolve_project_path, station_id

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
    scientific = processed_data_root(config, root_dir=workspace).resolve()
    site = resolve_project_path(".cache/measurements", root_dir=workspace).resolve()
    if site == scientific or site.is_relative_to(scientific) or scientific.is_relative_to(site):
        raise RuntimeError("Preview products must never overlap the MILGRAU scientific tree.")

    station = station_id(config)
    index, count = build_preview(site, station)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.info("Created local preview with %d measurement day(s): %s", count, index)
    logging.info(
        "From the workspace root, run: python -m http.server 8765 "
        "--bind 127.0.0.1 --directory .cache/measurements"
    )
    logging.info("Then open: http://127.0.0.1:8765/_preview-v2/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
