"""Generate the existing measurements site appearance from Git-owned state-v2 only.

This is a local cutover preview; neither old R2 objects nor .cache WEBPs are read.
The existing calendar and dashboard visual renderer are retained; only the
obsolete Mean RCS tab is hidden in newly generated pages.
"""
from __future__ import annotations

from pathlib import Path
import json
import re
from urllib.parse import urlsplit

from .published import load_published_states
from .render import (
    render_day_dashboard, read_dashboard_fingerprint,
    dashboard_fingerprint, day_output_path,
)
from .windows import PUBLICATION_WINDOW_LABELS, PUBLICATION_WINDOW_STARTS


def build_unchanged_frontend(
    repository: str | Path, cache_root: str | Path, station: str,
    *, r2_public_base: str, site_base: str = "./",
) -> tuple[Path, int]:
    """Rebuild from permanent Git state even when .cache/measurements has no WEBPs."""
    parsed = urlsplit(r2_public_base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("--r2-public-base must be a credential-free HTTPS URL.")
    if any(char in r2_public_base for char in ('<', '>', '"', "'")):
        raise ValueError("Invalid characters in R2 public URL")
    if any(char in site_base for char in ('<', '>', '"', "'")):
        raise ValueError("Invalid site URL")

    repo = Path(repository).resolve()
    root = Path(cache_root).resolve()
    target = root / "_site-v2"
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError("Unsafe local website preview directory")
    states = load_published_states(repo, station)
    from .history import load_controls
    controls = load_controls(repo, station)
    blocked_dates = set(controls["hidden"]) | set(controls["removed"])

    days: dict[str, Any] = {}
    for year, state in sorted(states.items()):
        for date, day in sorted(state["days"].items()):
            if date in blocked_dates:
                continue
            views = day["publication_windows"]
            coverage = {hour: hour in views for hour in PUBLICATION_WINDOW_STARTS}
            slots = {
                hour: {"available": coverage[hour], **(
                    {"publication_window_id": views[hour]["publication_window_id"]}
                    if coverage[hour] else {}
                )}
                for hour in PUBLICATION_WINDOW_STARTS
            }
            days[date] = {
                "day_url": f"{year}/{date[4:6]}/{date}/index.html",
                "coverage": coverage, "slots": slots,
            }

    # Keep verified HTMLs stable, regenerate only changed days. A site renderer
    # change or R2 public URL change updates the page fingerprint.
    target.mkdir(parents=True, exist_ok=True)
    expected = {day_output_path(target, date) for date in days}
    for year_root in target.iterdir():
        if not re.fullmatch(r"\d{4}", year_root.name):
            continue
        if year_root.is_symlink():
            raise ValueError(f"Unsafe generated site year symlink: {year_root}")
        if not year_root.is_dir():
            continue
        for stale in year_root.glob("*/*/index.html"):
            if stale in expected:
                continue
            if any(part.is_symlink() for part in (stale, stale.parent, stale.parent.parent)):
                raise ValueError(f"Unsafe generated daily HTML symlink: {stale}")
            if read_dashboard_fingerprint(stale) is None:
                raise ValueError(f"Refusing to remove unrelated HTML: {stale}")
            stale.unlink()
            for folder in (stale.parent, stale.parent.parent, year_root):
                try:
                    folder.rmdir()
                except OSError:
                    break

    for _year, state in sorted(states.items()):
        for date, day in sorted(state["days"].items()):
            if date in blocked_dates:
                continue
            output = day_output_path(target, date)
            if output.is_symlink() or any(parent.is_symlink() for parent in (
                output.parent, output.parent.parent, output.parent.parent.parent,
            )):
                raise ValueError(f"Unsafe generated dashboard destination: {output}")
            desired = dashboard_fingerprint(date, day, r2_public_base)
            if read_dashboard_fingerprint(output) != desired:
                render_day_dashboard(date, day, target, r2_public_base)

    manifest = {
        "schema_version": 4,
        "publication_window_labels": dict(PUBLICATION_WINDOW_LABELS),
        "period_labels": dict(PUBLICATION_WINDOW_LABELS),
        "days": days,
    }
    catalog = json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    catalog_path = target / "measurements.json"
    if not catalog_path.is_file() or catalog_path.read_text(encoding="utf-8") != catalog:
        catalog_path.write_text(catalog, encoding="utf-8")

    calendar = (repo / "ql-measurement-calendar.html").read_text(encoding="utf-8")
    exact = "const siteBaseUrl='https://spulidar.github.io/measurements/';"
    if exact not in calendar:
        raise RuntimeError("The original calendar changed: review site contract before proceeding.")
    calendar = calendar.replace(exact, "const siteBaseUrl=" + json.dumps(site_base) + ";")
    calendar_path = target / "ql-measurement-calendar.html"
    if not calendar_path.is_file() or calendar_path.read_text(encoding="utf-8") != calendar:
        calendar_path.write_text(calendar, encoding="utf-8")
    return target, len(days)
