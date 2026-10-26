"""Install the generated, current-layout site into publication-v2 only.

This writes the reviewed development Git working tree; it does NOT create
commits, push to GitHub, publish GitHub Pages, or interact with R2.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil

from .publication import _git, ensure_git_ready
from .render import read_dashboard_fingerprint
from .published import load_published_states

_YEAR = re.compile(r"^\d{4}$")
_MONTH = re.compile(r"^(0[1-9]|1[0-2])$")
_DATE = re.compile(r"^\d{8}$")


def _generated_day_path(date: str) -> Path:
    if not _DATE.fullmatch(date):
        raise ValueError(f"Invalid site date: {date!r}")
    return Path(date[:4]) / date[4:6] / date / "index.html"


def _ensure_safe_destination(root: Path, path: Path) -> None:
    if not path.is_relative_to(root):
        raise ValueError(f"Destination outside development repository: {path}")
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError(f"Refusing to write through symlink: {current}")
        current = current.parent


def export_site_to_branch(
    repository: str | Path, generated_site: str | Path, station: str,
    *, orchestrated: bool = False, allow_empty: bool = False,
) -> tuple[int, int]:
    """Copy only verified-state HTML and manifest into a clean development branch.

    Refuse empty catalogs and unexpected/orphan content. Previously generated
    pages missing from the new state are deleted only after all safety checks.
    """
    repo = Path(repository).resolve()
    generated = Path(generated_site).resolve()
    if not generated.is_dir() or generated.is_relative_to(repo) or repo.is_relative_to(generated):
        raise ValueError("The generated site must be an external staging directory.")
    if not orchestrated:
        ensure_git_ready(repo)
        dirty = _git(repo, "status", "--porcelain", "--untracked-files=normal")
        if dirty:
            raise RuntimeError(
                "Refusing site export with uncommitted/untracked Git files. "
                "Review, commit or stash your changes first."
            )
    # The orchestrator verifies the clean checkout BEFORE updating Git-owned state.
    # Only that checked workflow may export after state changes but before commit.

    states = load_published_states(repo, station)
    expected_dates = {
        date
        for state in states.values()
        for date in state["days"]
    }
    if not expected_dates and not allow_empty:
        raise RuntimeError(
            "No verified Git-owned publication windows: refusing to export "
            "an empty public catalog into the branch."
        )

    manifest_src = generated / "measurements.json"
    manifest = json.loads(manifest_src.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 4 or not isinstance(manifest.get("days"), dict):
        raise ValueError("Generated measurements.json is not the expected schema-v4 manifest")
    if set(manifest["days"]) != expected_dates:
        raise ValueError(
            "Generated measurements.json does not match verified Git-owned state-v2. "
            "Rebuild the local site before export."
        )

    # Preflight before writing anything, including stale HTML deletion.
    copies: list[tuple[Path, Path]] = []
    expected_paths: set[Path] = set()
    for date in sorted(expected_dates):
        relative = _generated_day_path(date)
        if manifest["days"][date].get("day_url") != relative.as_posix():
            raise ValueError(f"Incorrect day URL for {date}")
        source = generated / relative
        destination = repo / relative
        if source.is_symlink() or not source.is_file() or read_dashboard_fingerprint(source) is None:
            raise ValueError(f"Missing/generated HTML without publisher fingerprint: {source}")
        _ensure_safe_destination(repo, destination)
        if destination.exists() and (
            not destination.is_file() or read_dashboard_fingerprint(destination) is None
        ):
            raise RuntimeError(f"Refusing to overwrite an unrelated dashboard: {destination}")
        copies.append((source, destination))
        expected_paths.add(relative)

    existing: list[Path] = []
    for year in sorted(repo.iterdir()):
        if not _YEAR.fullmatch(year.name):
            continue
        if year.is_symlink() or not year.is_dir():
            raise ValueError(f"Unsafe site year directory: {year}")
        for month in year.iterdir():
            if month.is_symlink() or not month.is_dir() or not _MONTH.fullmatch(month.name):
                raise RuntimeError(f"Unexpected content in site year directory: {month}")
            for day in month.iterdir():
                if day.is_symlink() or not day.is_dir() or not _DATE.fullmatch(day.name):
                    raise RuntimeError(f"Unexpected content in site month directory: {day}")
                if day.name[:6] != year.name + month.name or sorted(p.name for p in day.iterdir()) != ["index.html"]:
                    raise RuntimeError(f"Unexpected content in date directory: {day}")
                page = day / "index.html"
                if page.is_symlink() or read_dashboard_fingerprint(page) is None:
                    raise RuntimeError(f"Refusing to modify unrelated daily HTML: {page}")
                existing.append(page)

    manifest_dst = repo / "measurements.json"
    _ensure_safe_destination(repo, manifest_dst)
    if manifest_dst.exists():
        current = json.loads(manifest_dst.read_text(encoding="utf-8"))
        if current.get("schema_version") != 4:
            raise RuntimeError("Refusing to overwrite an unrelated root catalog")
    calendar_source = repo / "ql-measurement-calendar.html"
    if not calendar_source.is_file():
        raise RuntimeError("The existing calendar source is missing")
    # Ensure local generated calendars use relative links before copying HTML.
    generated_calendar = (generated / "ql-measurement-calendar.html").read_text(encoding="utf-8")
    if 'const siteBaseUrl="./";' not in generated_calendar:
        raise ValueError("Generated calendar links are not relative to the development site")

    for source, destination in copies:
        if destination.is_file() and destination.read_bytes() == source.read_bytes():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    if not manifest_dst.is_file() or manifest_dst.read_bytes() != manifest_src.read_bytes():
        manifest_dst.write_bytes(manifest_src.read_bytes())

    retired = 0
    for old in existing:
        if old.relative_to(repo) in expected_paths:
            continue
        old.unlink()
        retired += 1
        for folder in (old.parent, old.parent.parent, old.parent.parent.parent):
            try:
                folder.rmdir()
            except OSError:
                break
    return len(copies), retired
