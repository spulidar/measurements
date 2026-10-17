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
from typing import Any

from .published import load_published_states
from .render import render_day_dashboard
from .windows import PUBLICATION_WINDOW_LABELS, PUBLICATION_WINDOW_STARTS

_QUICKLOOK = re.compile(r"^\d+nm_[A-Za-z0-9-]+_\d+(?:\.\d+)?km$")


def _public_day(date: str, day: dict[str, Any]) -> dict[str, Any]:
    """Minimal presentational adapter. No historical fallback or schema-v1 import."""
    canonical = {}
    for hour, record in day["publication_windows"].items():
        channels: dict[str, dict[str, str]] = {}
        for filename, metadata in record["assets"].items():
            suffix = filename.removeprefix(f"rcs_{record['publication_window_id']}_").removesuffix(".webp")
            if not _QUICKLOOK.fullmatch(suffix):
                raise ValueError(f"Unexpected published asset: {filename}")
            channel, altitude = suffix.rsplit("_", 1)
            channels.setdefault(channel, {})[altitude.removesuffix("km")] = metadata["key"]
        canonical[hour] = {
            "publication_window_id": record["publication_window_id"],
            "channels": channels,
            "mean": None,
        }
    return {"canonical": canonical, "historical": {}}


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

    days: dict[str, Any] = {}
    for year, state in sorted(states.items()):
        for date, day in sorted(state["days"].items()):
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

    # Leave historical site output and Git-tracked HTML untouched.
    target.mkdir(parents=True, exist_ok=True)
    for year, state in sorted(states.items()):
        for date, day in sorted(state["days"].items()):
            render_day_dashboard(
                date, _public_day(date, day), target,
                r2_public_base, show_mean=False,
            )

    manifest = {
        "schema_version": 4,
        "publication_window_labels": dict(PUBLICATION_WINDOW_LABELS),
        "period_labels": dict(PUBLICATION_WINDOW_LABELS),
        "days": days,
    }
    (target / "measurements.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    calendar = (repo / "ql-measurement-calendar.html").read_text(encoding="utf-8")
    exact = "const siteBaseUrl='https://spulidar.github.io/measurements/';"
    if exact not in calendar:
        raise RuntimeError("The original calendar changed: review site contract before proceeding.")
    calendar = calendar.replace(exact, "const siteBaseUrl=" + json.dumps(site_base) + ";")
    (target / "ql-measurement-calendar.html").write_text(calendar, encoding="utf-8")
    return target, len(days)
