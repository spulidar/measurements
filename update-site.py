"""SPU-Lidar static-site publisher backed by MILGRAU scientific sessions.

Responsibility boundary
-----------------------
MILGRAU owns continuous scientific sessions and their L1/L2 NetCDF products.
This repository owns station-local website publication windows (00–06, 06–12,
12–18, 18–24), website-only figure staging, R2 publication state and static
HTML.

Normal publication after MILGRAU processing::

    python update-site.py --publish-date 20250510 --no-push

The command reads L1/L2 directly, builds visualization-only publication views
in 03-site-products/, uploads only those staged WEBPs, updates persistent state,
and renders the daily site.  No scientific NetCDF is split or modified.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any, Mapping

import yaml

from publisher.catalog import (
    collect_staged_publication_assets,
    discover_legacy_html_paths,
    inventory_to_year_states,
)
from publisher.publish import (
    delete_r2_keys,
    get_cloud_credentials,
    get_cloud_inventory,
    push_site_updates,
    upload_to_r2,
)
from publisher.render import (
    dashboard_fingerprint,
    day_output_path,
    read_dashboard_fingerprint,
    render_day_dashboard,
    render_legacy_redirect,
    write_manifest,
)
from publisher.staging import stage_publication_products
from publisher.state import (
    all_dates,
    cleanup_candidates,
    commit_publication_window,
    get_day,
    load_states,
    mark_historical_retired,
    publication_window_payload,
    required_canonical_keys_for_cleanup,
    save_states,
    state_dir,
    validate_states,
)
from publisher.windows import discover_scientific_sessions


def load_config(config_path: str | Path = "config.yaml") -> dict[str, Any]:
    path = Path(config_path)
    if not path.is_absolute():
        path = (Path(__file__).resolve().parent / path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise RuntimeError(f"Configuration file is empty or invalid: {path}")
    for section in ("directories", "processing", "publication"):
        if section not in config:
            raise KeyError(f"Configuration file is missing required section: {section}")
    return config


def setup_logger(module_name: str, log_dir: str | Path) -> logging.Logger:
    log_root = Path(log_dir)
    log_root.mkdir(parents=True, exist_ok=True)
    log_filename = log_root / f"{module_name}_run_{datetime.now().strftime('%Y%m%d')}.log"
    logger = logging.getLogger(module_name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(log_filename, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MILGRAU session-aware website publication pipeline."
    )
    parser.add_argument("--config", default="config.yaml", help="measurements config.yaml")
    parser.add_argument(
        "--bootstrap-state-from-r2",
        action="store_true",
        help="One-time: rebuild publisher/state/YYYY.json from the actual R2 inventory.",
    )
    parser.add_argument(
        "--force-bootstrap-state",
        action="store_true",
        help="Allow --bootstrap-state-from-r2 to replace existing state files.",
    )
    parser.add_argument(
        "--validate-state",
        action="store_true",
        help="Validate persistent publication state and print a summary.",
    )
    parser.add_argument(
        "--write-legacy-redirects",
        action="store_true",
        help="Replace old saam/sapm/sant Dashboard/Gallery HTML with redirects.",
    )
    parser.add_argument(
        "--html-only",
        action="store_true",
        help="Render manifest/dashboards only; do not stage or upload website images.",
    )
    parser.add_argument(
        "--stage-only",
        action="store_true",
        help="Build 03-site-products from MILGRAU L1/L2, then stop before R2/state/site updates.",
    )
    parser.add_argument(
        "--skip-stage",
        action="store_true",
        help="Publish already-staged 03-site-products without reading/rendering MILGRAU products.",
    )
    parser.add_argument(
        "--force-site-products",
        action="store_true",
        help="Regenerate selected publication-window WEBPs even when their sidecar fingerprint is current.",
    )

    publish_group = parser.add_mutually_exclusive_group()
    publish_group.add_argument(
        "--publish-year",
        metavar="YYYY",
        help="Limit staging/publication to website windows whose local date is in this year.",
    )
    publish_group.add_argument(
        "--publish-date",
        metavar="YYYYMMDD",
        help="Limit staging/publication to the four website windows on this station-local date.",
    )

    parser.add_argument(
        "--cleanup-report",
        metavar="YEAR",
        help="Write an R2 cleanup report for historical assets superseded in YEAR.",
    )
    parser.add_argument(
        "--prune-r2",
        metavar="YEAR",
        help="Delete superseded historical R2 assets after replacement verification.",
    )
    parser.add_argument(
        "--confirm-r2-delete",
        action="store_true",
        help="Required together with --prune-r2.",
    )
    parser.add_argument("--no-push", action="store_true", help="Do not commit/push the site repository.")
    parser.add_argument("--dry-run", action="store_true", help="Do not render/write/upload/delete/commit/push.")
    return parser.parse_args()


def _workspace_paths(config: Mapping[str, Any]) -> dict[str, Path]:
    repo_root = Path(__file__).resolve().parent
    workspace_root = repo_root.parent
    directories = config["directories"]

    def workspace_path(key: str) -> Path:
        value = Path(str(directories[key])).expanduser()
        return value if value.is_absolute() else workspace_root / value

    log_value = Path(str(directories.get("log_dir", "logs"))).expanduser()
    log_dir = log_value if log_value.is_absolute() else repo_root / log_value

    return {
        "repo_root": repo_root,
        "workspace_root": workspace_root,
        "processed_data": workspace_path("processed_data"),
        "site_products": workspace_path("site_products"),
        "site_output": workspace_path("site_output"),
        "log_dir": log_dir,
    }


def _publication_settings(config: Mapping[str, Any]) -> dict[str, Any]:
    publication = config.get("publication")
    if not isinstance(publication, Mapping):
        raise ValueError("publication must be a mapping.")
    required = ("station_id", "timezone", "milgrau_config")
    missing = [name for name in required if not str(publication.get(name, "")).strip()]
    if missing:
        raise KeyError(f"Missing required publication setting(s): {missing}")
    result = dict(publication)
    result["station_id"] = str(result["station_id"]).strip().lower()
    result["timezone"] = str(result["timezone"]).strip()
    result["output_format"] = str(result.get("output_format", "webp")).strip().lstrip(".").lower()
    return result


def _resolve_milgrau_config_path(
    publication: Mapping[str, Any],
    workspace_root: Path,
) -> Path:
    value = Path(str(publication["milgrau_config"])).expanduser()
    return value.resolve() if value.is_absolute() else (workspace_root / value).resolve()


def _load_milgrau_config(path: Path) -> dict[str, Any]:
    try:
        from milgrau.config.loader import load_config as load_milgrau_config
    except ImportError as exc:
        raise RuntimeError(
            "MILGRAU session-refactor must be installed in the same environment. "
            "For now use `pip install -e ../milgrau`."
        ) from exc
    return load_milgrau_config(path)


def _validate_milgrau_station(
    milgrau_config: Mapping[str, Any],
    publication: Mapping[str, Any],
) -> None:
    station = milgrau_config.get("_station_catalog", {}).get("station", {})
    milgrau_station = str(station.get("id", "")).strip().lower()
    milgrau_timezone = str(station.get("timezone", "")).strip()
    if milgrau_station != publication["station_id"]:
        raise RuntimeError(
            f"measurements station {publication['station_id']!r} does not match "
            f"MILGRAU station {milgrau_station!r}."
        )
    if milgrau_timezone and milgrau_timezone != publication["timezone"]:
        raise RuntimeError(
            f"measurements timezone {publication['timezone']!r} does not match "
            f"MILGRAU station timezone {milgrau_timezone!r}."
        )


def _validate_filters(args: argparse.Namespace) -> None:
    if args.prune_r2 and not args.confirm_r2_delete:
        raise SystemExit("--prune-r2 requires --confirm-r2-delete")
    if args.publish_year and (len(args.publish_year) != 4 or not args.publish_year.isdigit()):
        raise SystemExit("--publish-year must be YYYY")
    if args.publish_date:
        try:
            datetime.strptime(args.publish_date, "%Y%m%d")
        except ValueError as exc:
            raise SystemExit("--publish-date must be a real YYYYMMDD civil date") from exc
    if args.html_only and args.stage_only:
        raise SystemExit("--html-only and --stage-only are mutually exclusive")


def _state_summary(logger: logging.Logger, states: dict[str, dict]) -> dict[str, int]:
    counts = validate_states(states)
    logger.info(
        "State: %d years | %d days | %d historical products | %d publication windows | %d asset keys",
        counts["years"],
        counts["days"],
        counts["historical_products"],
        counts["publication_windows"],
        counts["asset_keys"],
    )
    return counts


def _existing_payload(states: dict[str, dict], date: str, window_start: str) -> dict | None:
    day = get_day(states, date, create=False)
    if not day:
        return None
    return day.get("canonical", {}).get(window_start)


def _run_staging(
    *,
    config: Mapping[str, Any],
    paths: Mapping[str, Path],
    publication: Mapping[str, Any],
    args: argparse.Namespace,
    logger: logging.Logger,
) -> tuple:
    milgrau_config_path = _resolve_milgrau_config_path(publication, paths["workspace_root"])
    milgrau_config = _load_milgrau_config(milgrau_config_path)
    _validate_milgrau_station(milgrau_config, publication)

    sessions = discover_scientific_sessions(paths["processed_data"], publication["station_id"])
    logger.info("Discovered %d canonical MILGRAU scientific session(s).", len(sessions))
    results = stage_publication_products(
        sessions,
        site_products_root=paths["site_products"],
        station_id=publication["station_id"],
        timezone_name=publication["timezone"],
        milgrau_config=milgrau_config,
        milgrau_config_path=milgrau_config_path,
        publication_settings=publication,
        milgrau_root=milgrau_config_path.parent,
        publish_year=args.publish_year,
        publish_date=args.publish_date,
        force=args.force_site_products,
        dry_run=args.dry_run,
        logger=logger,
    )
    logger.info(
        "Website staging: %d rendered | %d current/skipped | %d without data.",
        sum(1 for item in results if item.rendered),
        sum(1 for item in results if item.skipped and not item.no_data),
        sum(1 for item in results if item.no_data),
    )
    return results


def _publish_staged_products(
    states: dict[str, dict],
    *,
    site_products_root: Path,
    station_id: str,
    logger: logging.Logger,
    dry_run: bool,
    publish_year: str | None = None,
    publish_date: str | None = None,
) -> tuple[int, int]:
    catalog = collect_staged_publication_assets(site_products_root, station_id)
    if not catalog.windows:
        logger.info("No staged website publication WEBPs found; persistent state is unchanged.")
        return 0, 0

    client, bucket, _ = get_cloud_credentials(logger)
    uploaded = 0
    updated_windows = 0

    for (date, window_start), window in sorted(catalog.windows.items()):
        if publish_year and date[:4] != publish_year:
            continue
        if publish_date and date != publish_date:
            continue

        candidate = publication_window_payload(window)
        existing = _existing_payload(states, date, window_start)
        if existing and existing.get("fingerprint") == candidate.get("fingerprint"):
            continue

        logger.info(
            "Publishing %s (%d staged asset(s), %d source session(s)).",
            window.publication_window_id,
            len(window.assets),
            len(window.source_sessions),
        )
        success = True
        for asset in sorted(window.assets, key=lambda item: item.key):
            if dry_run:
                logger.info("[DRY-RUN] upload %s -> %s", asset.path.name, asset.key)
                continue
            if upload_to_r2(client, bucket, asset.path, asset.key, logger):
                uploaded += 1
            else:
                success = False

        if success:
            if not dry_run:
                commit_publication_window(states, window)
            updated_windows += 1
        else:
            logger.error(
                "State for %s was NOT changed because at least one R2 upload failed.",
                window.publication_window_id,
            )

    return uploaded, updated_windows


def _render_site(
    states: dict[str, dict],
    site_root: Path,
    public_url: str,
    incremental: bool,
    force_all: bool,
    logger: logging.Logger,
    dry_run: bool,
) -> tuple[int, int]:
    write_manifest(states, site_root, dry_run=dry_run)
    rendered = 0
    skipped = 0

    for date in all_dates(states):
        day = get_day(states, date, create=False)
        assert day is not None
        output = day_output_path(site_root, date)
        new_fp = dashboard_fingerprint(date, day)
        changed = read_dashboard_fingerprint(output) != new_fp
        if force_all or changed or not incremental:
            logger.info("Rendering daily dashboard: %s", output)
            render_day_dashboard(date, day, site_root, public_url, dry_run=dry_run)
            rendered += 1
        else:
            skipped += 1
    return rendered, skipped


def _write_redirects(site_root: Path, logger: logging.Logger, dry_run: bool) -> int:
    discovered = discover_legacy_html_paths(site_root)
    count = 0
    for date, rel_paths in sorted(discovered.items()):
        target = f"{date[4:6]}/{date}/index.html"
        for rel in rel_paths:
            source = site_root / rel
            logger.info("Redirecting %s -> %s", rel, target)
            render_legacy_redirect(source, target, dry_run=dry_run)
            count += 1
    return count


def _cleanup_plan(states: dict[str, dict], year: str, inventory: dict[str, int]) -> dict:
    entries: list[dict] = []
    total_bytes = 0
    eligible_bytes = 0

    for candidate in cleanup_candidates(states, year):
        day = get_day(states, candidate["date"], create=False)
        assert day is not None
        replacement_keys = required_canonical_keys_for_cleanup(day, candidate["product"])
        missing_replacements = sorted(key for key in replacement_keys if key not in inventory)
        existing_historical = sorted(key for key in candidate["keys"] if key in inventory)
        already_missing_historical = sorted(key for key in candidate["keys"] if key not in inventory)
        bytes_here = sum(inventory.get(key, 0) for key in existing_historical)
        eligible = bool(replacement_keys) and not missing_replacements
        total_bytes += bytes_here
        if eligible:
            eligible_bytes += bytes_here
        entries.append(
            {
                **candidate,
                "eligible": eligible,
                "replacement_keys": sorted(replacement_keys),
                "missing_replacement_keys": missing_replacements,
                "existing_historical_keys": existing_historical,
                "already_missing_historical_keys": already_missing_historical,
                "bytes": bytes_here,
            }
        )

    return {
        "schema_version": 1,
        "year": str(year),
        "candidates": len(entries),
        "eligible_candidates": sum(1 for item in entries if item["eligible"]),
        "bytes_present": total_bytes,
        "eligible_bytes": eligible_bytes,
        "entries": entries,
    }


def _write_cleanup_report(report_dir: Path, year: str, plan: dict, dry_run: bool) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"r2-cleanup-report-{year}.json"
    if not dry_run:
        path.write_text(
            json.dumps(plan, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return path


def main() -> int:
    args = parse_args()
    _validate_filters(args)
    config = load_config(args.config)
    paths = _workspace_paths(config)
    publication = _publication_settings(config)
    logger = setup_logger("update-site", paths["log_dir"])

    if not args.dry_run:
        paths["site_output"].mkdir(parents=True, exist_ok=True)
        paths["site_products"].mkdir(parents=True, exist_ok=True)

    if args.stage_only:
        _run_staging(
            config=config,
            paths=paths,
            publication=publication,
            args=args,
            logger=logger,
        )
        return 0

    existing_state_files = (
        list(state_dir(paths["site_output"]).glob("*.json"))
        if state_dir(paths["site_output"]).exists()
        else []
    )

    public_url = str(publication.get("cloud_public_url", "")).rstrip("/")
    if args.bootstrap_state_from_r2:
        if existing_state_files and not args.force_bootstrap_state:
            raise SystemExit(
                "Persistent state already exists. Refusing to bootstrap over it; "
                "use --force-bootstrap-state only if intentional."
            )
        client, bucket, credential_public_url = get_cloud_credentials(logger)
        if not public_url:
            public_url = credential_public_url
        inventory = get_cloud_inventory(client, bucket, logger)
        states = inventory_to_year_states(inventory, publication["station_id"])
        _state_summary(logger, states)
        if not args.dry_run:
            save_states(paths["site_output"], states)
    else:
        states = load_states(paths["site_output"])
        if not states:
            raise SystemExit(
                "No persistent publication state found. Run --bootstrap-state-from-r2 first."
            )

    if args.validate_state:
        _state_summary(logger, states)
        only_validate = not any(
            [
                args.bootstrap_state_from_r2,
                args.write_legacy_redirects,
                args.cleanup_report,
                args.prune_r2,
                args.publish_year,
                args.publish_date,
            ]
        ) and not args.html_only
        if only_validate:
            return 0

    if args.cleanup_report or args.prune_r2:
        year = str(args.cleanup_report or args.prune_r2)
        client, bucket, credential_public_url = get_cloud_credentials(logger)
        if not public_url:
            public_url = credential_public_url
        inventory = get_cloud_inventory(client, bucket, logger)
        plan = _cleanup_plan(states, year, inventory)
        report_path = _write_cleanup_report(paths["log_dir"], year, plan, args.dry_run)
        logger.info(
            "Cleanup %s: %d eligible historical products, %.2f MiB removable. Report: %s",
            year,
            plan["eligible_candidates"],
            plan["eligible_bytes"] / (1024 * 1024),
            report_path,
        )

        changed = False
        if args.prune_r2:
            for item in plan["entries"]:
                if not item["eligible"]:
                    continue
                keys = item["existing_historical_keys"]
                if args.dry_run:
                    logger.info(
                        "[DRY-RUN] delete %d historical keys for %s %s",
                        len(keys),
                        item["date"],
                        item["label"],
                    )
                    continue
                deleted = delete_r2_keys(client, bucket, keys, logger)
                if set(keys).issubset(deleted):
                    mark_historical_retired(states, item["date"], item["product"])
                    changed = True
                else:
                    logger.error(
                        "Historical state retained for %s %s because not all deletes succeeded.",
                        item["date"],
                        item["label"],
                    )
            if changed and not args.dry_run:
                save_states(paths["site_output"], states)

        if changed:
            _render_site(
                states,
                paths["site_output"],
                public_url,
                incremental=True,
                force_all=False,
                logger=logger,
                dry_run=args.dry_run,
            )
        if args.prune_r2:
            push_site_updates(paths["site_output"], logger, no_push=args.no_push, dry_run=args.dry_run)
        return 0

    if not args.html_only and not args.bootstrap_state_from_r2 and not args.skip_stage:
        _run_staging(
            config=config,
            paths=paths,
            publication=publication,
            args=args,
            logger=logger,
        )

    uploaded = 0
    updated_windows = 0
    if not args.html_only and not args.bootstrap_state_from_r2:
        uploaded, updated_windows = _publish_staged_products(
            states,
            site_products_root=paths["site_products"],
            station_id=publication["station_id"],
            logger=logger,
            dry_run=args.dry_run,
            publish_year=args.publish_year,
            publish_date=args.publish_date,
        )
        if updated_windows and not args.dry_run:
            save_states(paths["site_output"], states)

    if not public_url:
        _, _, public_url = get_cloud_credentials(logger)

    force_all = bool(args.html_only or args.bootstrap_state_from_r2)
    rendered, skipped = _render_site(
        states,
        paths["site_output"],
        public_url,
        incremental=bool(config["processing"].get("incremental", False)),
        force_all=force_all,
        logger=logger,
        dry_run=args.dry_run,
    )

    redirects = 0
    if args.write_legacy_redirects:
        redirects = _write_redirects(paths["site_output"], logger, args.dry_run)

    logger.info(
        "Finished: %d dashboards rendered | %d skipped | %d staged assets uploaded | "
        "%d publication windows updated | %d redirects written.",
        rendered,
        skipped,
        uploaded,
        updated_windows,
        redirects,
    )

    push_site_updates(paths["site_output"], logger, no_push=args.no_push, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
