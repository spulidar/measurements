"""SPU lidar static-site publisher with persistent R2-backed publication state.

One-time migration:
    python update-site.py --bootstrap-state-from-r2 --html-only --no-push
    python update-site.py --validate-state
    python update-site.py --write-legacy-redirects --html-only --no-push

Normal publication after reprocessing:
    python update-site.py --no-push

R2 cleanup after canonical replacement:
    python update-site.py --cleanup-report 2024
    python update-site.py --prune-r2 2024 --confirm-r2-delete --no-push
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
from pathlib import Path

import yaml

from publisher.catalog import (
    collect_local_canonical_assets,
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
from publisher.state import (
    all_dates,
    canonical_period_payload,
    cleanup_candidates,
    clone_states,
    commit_canonical_period,
    get_day,
    load_states,
    mark_historical_retired,
    required_canonical_keys_for_cleanup,
    save_states,
    state_dir,
    validate_states,
)


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
        description="MILGRAU measurements publisher with persistent publication state."
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml.")
    parser.add_argument(
        "--bootstrap-state-from-r2",
        action="store_true",
        help="One-time: build publisher/state/YYYY.json from the actual R2 inventory.",
    )
    parser.add_argument(
        "--force-bootstrap-state",
        action="store_true",
        help="Allow --bootstrap-state-from-r2 to replace existing state files.",
    )
    parser.add_argument(
        "--validate-state",
        action="store_true",
        help="Validate persistent state and print a summary.",
    )
    parser.add_argument(
        "--write-legacy-redirects",
        action="store_true",
        help="Replace old saam/sapm/sant Dashboard/Gallery HTML with tiny redirects.",
    )
    parser.add_argument(
        "--html-only",
        action="store_true",
        help="Render state/manifest/dashboards only; never publish local quicklooks to R2.",
    )
    publish_group = parser.add_mutually_exclusive_group()
    publish_group.add_argument(
        "--publish-year",
        metavar="YYYY",
        help="Only consider local canonical quicklooks from this year for R2 publication.",
    )
    publish_group.add_argument(
        "--publish-date",
        metavar="YYYYMMDD",
        help="Only consider local canonical quicklooks from this civil date for R2 publication.",
    )
    parser.add_argument(
        "--cleanup-report",
        metavar="YEAR",
        help="Write an R2 cleanup report for historical assets superseded in YEAR.",
    )
    parser.add_argument(
        "--prune-r2",
        metavar="YEAR",
        help="Delete superseded historical R2 assets for YEAR after replacement verification.",
    )
    parser.add_argument(
        "--confirm-r2-delete",
        action="store_true",
        help="Required together with --prune-r2.",
    )
    parser.add_argument("--no-push", action="store_true", help="Do not commit/push the repository.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write, upload, delete, commit, or push.")
    return parser.parse_args()


def _state_summary(logger: logging.Logger, states: dict[str, dict]) -> dict[str, int]:
    counts = validate_states(states)
    logger.info(
        "State: %d years | %d days | %d historical products | %d canonical periods | %d asset keys",
        counts["years"],
        counts["days"],
        counts["historical_products"],
        counts["canonical_periods"],
        counts["asset_keys"],
    )
    return counts


def _existing_payload(states: dict[str, dict], date: str, period: str) -> dict | None:
    day = get_day(states, date, create=False)
    if not day:
        return None
    return day.get("canonical", {}).get(period)



def _publish_local_canonical(
    states: dict[str, dict],
    data_root: Path,
    logger: logging.Logger,
    dry_run: bool,
    publish_year: str | None = None,
    publish_date: str | None = None,
) -> tuple[int, int]:
    all_images = sorted(data_root.rglob("*.webp")) if data_root.exists() else []
    local = collect_local_canonical_assets(all_images)
    if not local.periods:
        logger.info("No local canonical quicklooks found; persistent published state is unchanged.")
        return 0, 0

    client, bucket, _ = get_cloud_credentials(logger)
    uploaded = 0
    updated_periods = 0

    for (date, period_id), period in sorted(local.periods.items()):
        if publish_year and date[:4] != publish_year:
            continue
        if publish_date and date != publish_date:
            continue

        candidate = canonical_period_payload(period)
        existing = _existing_payload(states, date, period_id)

        if existing and existing.get("fingerprint") == candidate.get("fingerprint"):
            continue

        logger.info("Publishing canonical period %s_%s (%d assets).", date, period_id, len(period.assets))
        success = True
        for asset in sorted(period.assets, key=lambda item: item.key):
            if dry_run:
                logger.info("[DRY-RUN] upload %s -> %s", asset.path.name, asset.key)
                continue
            if upload_to_r2(client, bucket, asset.path, asset.key, logger):
                uploaded += 1
            else:
                success = False

        if success:
            if not dry_run:
                commit_canonical_period(states, period)
            updated_periods += 1
        else:
            logger.error(
                "State for %s_%s was NOT changed because at least one upload failed.",
                date,
                period_id,
            )

    return uploaded, updated_periods


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


def _cleanup_plan(
    states: dict[str, dict],
    year: str,
    inventory: dict[str, int],
) -> dict:
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
        path.write_text(json.dumps(plan, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    logger = setup_logger("update-site", config["directories"].get("log_dir", "logs"))

    root_dir = Path.cwd().parent
    data_root = root_dir / config["directories"]["processed_data"]
    site_root = root_dir / config["directories"].get("site_output", "measurements")
    public_url = str(config.get("publication", {}).get("cloud_public_url", "")).rstrip("/")
    if not args.dry_run:
        site_root.mkdir(parents=True, exist_ok=True)

    if args.prune_r2 and not args.confirm_r2_delete:
        raise SystemExit("--prune-r2 requires --confirm-r2-delete")

    if args.publish_year and (len(args.publish_year) != 4 or not args.publish_year.isdigit()):
        raise SystemExit("--publish-year must be YYYY")
    if args.publish_date and (len(args.publish_date) != 8 or not args.publish_date.isdigit()):
        raise SystemExit("--publish-date must be YYYYMMDD")

    existing_state_files = list(state_dir(site_root).glob("*.json")) if state_dir(site_root).exists() else []

    if args.bootstrap_state_from_r2:
        if existing_state_files and not args.force_bootstrap_state:
            raise SystemExit(
                "Persistent state already exists. Refusing to bootstrap over it; use --force-bootstrap-state only if intentional."
            )
        client, bucket, credential_public_url = get_cloud_credentials(logger)
        if not public_url:
            public_url = credential_public_url
        inventory = get_cloud_inventory(client, bucket, logger)
        states = inventory_to_year_states(inventory)
        _state_summary(logger, states)
        if not args.dry_run:
            save_states(site_root, states)
    else:
        states = load_states(site_root)
        if not states:
            raise SystemExit(
                "No persistent publication state found. Run --bootstrap-state-from-r2 before replacing old dashboards."
            )

    if args.validate_state:
        _state_summary(logger, states)
        only_validate = not any(
            [
                args.bootstrap_state_from_r2,
                args.write_legacy_redirects,
                args.cleanup_report,
                args.prune_r2,
            ]
        ) and args.html_only is False
        if only_validate:
            return 0

    # Cleanup modes use R2 as an independent safety check and do not publish local products.
    if args.cleanup_report or args.prune_r2:
        year = str(args.cleanup_report or args.prune_r2)
        client, bucket, credential_public_url = get_cloud_credentials(logger)
        if not public_url:
            public_url = credential_public_url
        inventory = get_cloud_inventory(client, bucket, logger)
        plan = _cleanup_plan(states, year, inventory)
        report_path = _write_cleanup_report(
            Path(config["directories"].get("log_dir", "logs")),
            year,
            plan,
            dry_run=args.dry_run,
        )
        logger.info(
            "Cleanup %s: %d eligible historical products, %.2f MiB removable. Report: %s",
            year,
            plan["eligible_candidates"],
            plan["eligible_bytes"] / (1024 * 1024),
            report_path,
        )

        if args.prune_r2:
            changed = False
            for item in plan["entries"]:
                if not item["eligible"]:
                    continue
                keys = item["existing_historical_keys"]
                if args.dry_run:
                    logger.info("[DRY-RUN] delete %d historical keys for %s %s", len(keys), item["date"], item["label"])
                    continue
                deleted = delete_r2_keys(client, bucket, keys, logger)
                if set(keys).issubset(deleted):
                    mark_historical_retired(states, item["date"], item["product"])
                    changed = True
                    logger.info("Retired historical %s %s after R2 cleanup.", item["date"], item["label"])
                else:
                    logger.error("Historical state retained for %s %s because not all deletes succeeded.", item["date"], item["label"])
            if changed and not args.dry_run:
                save_states(site_root, states)

            if changed:
                rendered, skipped = _render_site(
                    states,
                    site_root,
                    public_url,
                    incremental=True,
                    force_all=False,
                    logger=logger,
                    dry_run=args.dry_run,
                )
                logger.info("Post-cleanup rendering: %d updated, %d unchanged.", rendered, skipped)

        if args.prune_r2:
            push_site_updates(site_root, logger, no_push=args.no_push, dry_run=args.dry_run)
        return 0

    uploaded = 0
    updated_periods = 0
    if not args.html_only and not args.bootstrap_state_from_r2:
        uploaded, updated_periods = _publish_local_canonical(
            states,
            data_root,
            logger,
            args.dry_run,
            publish_year=args.publish_year,
            publish_date=args.publish_date,
        )
        if updated_periods and not args.dry_run:
            save_states(site_root, states)

    if not public_url:
        _, _, public_url = get_cloud_credentials(logger)

    force_all = bool(args.html_only or args.bootstrap_state_from_r2)
    rendered, skipped = _render_site(
        states,
        site_root,
        public_url,
        incremental=bool(config["processing"].get("incremental", False)),
        force_all=force_all,
        logger=logger,
        dry_run=args.dry_run,
    )

    redirects = 0
    if args.write_legacy_redirects:
        redirects = _write_redirects(site_root, logger, args.dry_run)

    logger.info(
        "Finished: %d dashboards rendered | %d skipped | %d assets uploaded | %d canonical periods updated | %d redirects written.",
        rendered,
        skipped,
        uploaded,
        updated_periods,
        redirects,
    )

    push_site_updates(site_root, logger, no_push=args.no_push, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
