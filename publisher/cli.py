"""Only user-facing publication entrypoint; optionally installed with measurements."""
from __future__ import annotations

import argparse
from datetime import datetime
from . import pipeline


def _date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected a real YYYYMMDD date") from exc
    if len(value) != 8 or not value.isdigit():
        raise argparse.ArgumentTypeError("Expected YYYYMMDD")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="milgrau-publish",
        description="Incrementally publish MILGRAU L1 quicklooks to the isolated measurements branch.",
    )
    parser.add_argument("--date", dest="sync_date", type=_date,
                        help="Synchronize only one station-local day")
    parser.add_argument("--dry-run", action="store_true", help="Plan changes without uploads or writes")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="Report local, staged, published and blocked days")
    images = sub.add_parser("images", help="Render WEBPs locally only")
    images.add_argument("date", type=_date)
    sub.add_parser("html", help="Rebuild HTML and calendar from verified Git state")
    cache = sub.add_parser(
        "cache", help="Audit previously rendered WEBPs; explicit opt-in to publish without rendering"
    )
    cache.add_argument("--date", type=_date, help="Import only one local publication date")
    cache.add_argument("--confirm", action="store_true",
                       help="Upload verified existing WEBPs and publish state/HTML in resumable batches")
    hide = sub.add_parser("hide", help="Hide a day without deleting R2")
    hide.add_argument("date", type=_date)
    remove = sub.add_parser("remove", help="Preview/perform fully verified day removal")
    remove.add_argument("date", type=_date)
    remove.add_argument("--confirm", action="store_true", help="Required to back up and remove R2 objects")
    restore = sub.add_parser("restore", help="Restore a hidden or removed day")
    restore.add_argument("date", type=_date)
    args = parser.parse_args(argv)
    if args.sync_date and args.command:
        parser.error("--date applies to the default sync only")
    if args.dry_run and args.command:
        parser.error("--dry-run applies to the default sync only")
    if args.command == "status":
        return pipeline.status()
    if args.command == "images":
        return pipeline.images(args.date)
    if args.command == "html":
        return pipeline.publish_html()
    if args.command == "cache":
        return pipeline.publish_cached(date=args.date, confirm=args.confirm)
    if args.command in {"hide", "remove", "restore"}:
        from . import history
        return history.operate(args.command, args.date, confirm=getattr(args, "confirm", False))
    return pipeline.sync(date=args.sync_date, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
