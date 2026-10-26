"""One-call publication orchestration, reusing MILGRAU and measurements modules.

Work is serialized: R2 upload and SHA verification, Git state + HTML generated,
one Git commit/push, then disposable local WEBPs are cleaned. No L1 is edited.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
from typing import Any
from datetime import datetime, timezone

from .publication import ensure_git_ready, _git, publish_cache
from .published import load_published_states
from .site_v2 import build_unchanged_frontend
from .site_export import export_site_to_branch
from .staging import stage_publication_products
from .v2 import collect_validated_staging
from .windows import discover_scientific_sessions

LOG = logging.getLogger("milgrau-publish")
BRANCH = "publication-v2"


@dataclass
class Context:
    repo: Path
    workspace: Path
    config_path: Path
    config: dict
    station: str
    timezone: str
    cache: Path
    scientific: Path
    visual_root: Path


def context() -> Context:
    from milgrau.config.loader import load_config
    from milgrau.io.paths import station_id, processed_data_root, resolve_project_path
    from milgrau.viz.config import resolve_visualization_config
    from milgrau.viz import style

    repo = Path(__file__).resolve().parents[1]
    workspace = repo.parent
    config_path = workspace / "config.yaml"
    if not config_path.is_file():
        raise FileNotFoundError(f"Expected MILGRAU workspace config: {config_path}")
    config = load_config(config_path)
    station = station_id(config)
    timezone_name = str(config["_station_catalog"]["station"]["timezone"])
    if resolve_visualization_config(config).output_format != "webp":
        raise ValueError("MILGRAU visualization.output_format must be webp")
    scientific = processed_data_root(config, root_dir=workspace).resolve()
    cache = resolve_project_path(".cache/measurements", root_dir=workspace).resolve()
    if cache == scientific or cache.is_relative_to(scientific) or scientific.is_relative_to(cache):
        raise RuntimeError("Publication cache overlaps scientific products")
    logos = style.configured_logos(config)
    render_roots = (workspace, Path(style.__file__).resolve().parents[2])
    visual_root = next((
        root for root in render_roots
        if all(style.resolve_logo_path(root, name).is_file() for name, _ in logos)
    ), None)
    if visual_root is None:
        raise FileNotFoundError("Configured MILGRAU logo files cannot be resolved")
    return Context(repo, workspace, config_path, config, station, timezone_name,
                   cache, scientific, visual_root)


def secrets(ctx: Context, *, require_r2: bool = False) -> dict[str, str]:
    """Read a known local credentials.py; never import arbitrary working-dir modules."""
    candidate_paths = (
        ctx.workspace / "credentials.py",
        ctx.workspace / "milgrau" / "credentials.py",
    )
    values: dict[str, str] = {}
    for path in candidate_paths:
        if not path.is_file():
            continue
        if path.is_symlink():
            raise RuntimeError("Refusing symlinked publication credentials")
        spec = importlib.util.spec_from_file_location("_milgrau_private_publish_credentials", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Cannot load existing credentials.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for key in ("GITHUB_USER", "GITHUB_TOKEN", "R2_BUCKET_NAME", "R2_ACCESS_KEY",
                    "R2_SECRET_KEY", "R2_ENDPOINT", "R2_PUBLIC_URL"):
            val = getattr(module, key, None)
            if val is not None:
                values[key] = str(val).strip()
        break
    for key in ("GITHUB_USER", "GITHUB_TOKEN", "R2_BUCKET_NAME", "R2_ACCESS_KEY",
                "R2_SECRET_KEY", "R2_ENDPOINT", "R2_PUBLIC_URL"):
        if os.environ.get(key):
            values[key] = os.environ[key].strip()
    if require_r2:
        missing = [k for k in ("R2_BUCKET_NAME", "R2_ACCESS_KEY", "R2_SECRET_KEY",
                              "R2_ENDPOINT", "R2_PUBLIC_URL") if not values.get(k)]
        if missing:
            raise RuntimeError("Missing publication credentials: " + ", ".join(missing))
    return values


def r2_client(settings: dict[str, str]):
    """Use the same authenticated boto3 S3 API as measurements."""
    import boto3
    from urllib.parse import urlsplit
    endpoint = settings["R2_ENDPOINT"]
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("R2_ENDPOINT must be credential-free HTTPS")
    return boto3.client(
        "s3", endpoint_url=endpoint, region_name="auto",
        aws_access_key_id=settings["R2_ACCESS_KEY"],
        aws_secret_access_key=settings["R2_SECRET_KEY"],
    )


def checkout_ready(repo: Path) -> None:
    ensure_git_ready(repo)
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=normal")
    if dirty:
        raise RuntimeError("Publication checkout has uncommitted or untracked files; "
                           "commit or clean it before running milgrau-publish")


def git_push(repo: Path, settings: dict[str, str]) -> None:
    origin = _git(repo, "remote", "get-url", "origin")
    env = os.environ.copy()
    token = settings.get("GITHUB_TOKEN")
    if token and (origin.startswith("https://") or origin.startswith("http://")):
        user = settings.get("GITHUB_USER") or "x-access-token"
        encoded = base64.b64encode(f"{user}:{token}".encode()).decode()
        # Git config via process env avoids tokens in URLs and command arguments.
        pos = int(env.get("GIT_CONFIG_COUNT", "0"))
        env["GIT_CONFIG_COUNT"] = str(pos + 1)
        env[f"GIT_CONFIG_KEY_{pos}"] = "http.https://github.com/.extraheader"
        env[f"GIT_CONFIG_VALUE_{pos}"] = "AUTHORIZATION: basic " + encoded
    result = subprocess.run(
        ["git", "push", "origin", f"HEAD:refs/heads/{BRANCH}"],
        cwd=repo, env=env, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        error = result.stderr.replace(token, "<redacted>") if token else result.stderr
        raise RuntimeError(f"Git push of {BRANCH} failed: {error.strip()}")
    local = _git(repo, "rev-parse", "HEAD")
    remote = _git(repo, "ls-remote", "--heads", "origin", BRANCH)
    if not remote or remote.split()[0] != local:
        raise RuntimeError("GitHub did not confirm published branch HEAD")


def commit_site(ctx: Context, settings: dict[str, str], *, allow_empty: bool = False) -> int:
    """Commit generated pages, persistent state and catalog together."""
    base = settings.get("R2_PUBLIC_URL", "").strip()
    if not base:
        raise RuntimeError("R2_PUBLIC_URL missing from credentials.py")
    generated, count = build_unchanged_frontend(
        ctx.repo, ctx.cache, ctx.station, r2_public_base=base,
    )
    export_site_to_branch(
        ctx.repo, generated, ctx.station, orchestrated=True, allow_empty=allow_empty,
    )
    _git(ctx.repo, "add", "-A", "--", "publisher/state-v2", "measurements.json",
         ":(glob)[0-9][0-9][0-9][0-9]/**/index.html")
    staged = _git(ctx.repo, "diff", "--cached", "--name-only")
    if staged:
        _git(ctx.repo, "commit", "-m", "Publish verified SPU L1 quicklooks and dashboards")
        git_push(ctx.repo, settings)
        LOG.info("Pushed %d changed Git files to %s", len(staged.splitlines()), BRANCH)
    else:
        LOG.info("Git state, HTML and catalog already current")
    return count


def _write_journal(ctx: Context, action: str, status: str, **counts) -> None:
    path = ctx.cache / "_publish-journal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"action": action, "status": status,
               "at_utc": datetime.now(timezone.utc).isoformat(), **counts}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _blocked_dates(ctx: Context) -> set[str]:
    from .history import load_controls
    controls = load_controls(ctx.repo, ctx.station)
    return set(controls["hidden"]) | set(controls["removed"])


def _stage(ctx: Context, *, date: str | None, dry_run: bool) -> dict[str, int]:
    invalid: list[tuple[str, str]] = []
    def report(session_id: str, path: Path, reason: str) -> None:
        invalid.append((session_id, reason))
        LOG.warning("Excluded invalid L1 %s: %s", session_id, reason)

    sessions = discover_scientific_sessions(
        ctx.scientific, ctx.station, publish_date=date, timezone_name=ctx.timezone,
        skip_invalid_l1=True, report_issue=report,
    )
    previous = load_published_states(ctx.repo, ctx.station)
    fingerprints = {
        record["publication_window_id"]: record["source_fingerprint"]
        for yearly in previous.values() for day in yearly["days"].values()
        for record in day["publication_windows"].values()
    }
    results = stage_publication_products(
        sessions, site_products_root=ctx.cache, station_id=ctx.station,
        timezone_name=ctx.timezone, milgrau_config=ctx.config,
        milgrau_config_path=ctx.config_path, publication_settings={
            "schema_version": 2, "mode": "stage-only",
        }, milgrau_root=ctx.visual_root, publish_date=date, dry_run=dry_run,
        excluded_dates=_blocked_dates(ctx), logger=LOG,
        published_fingerprints=fingerprints,
    )
    numbers = {
        "sessions": len(sessions), "invalid_l1": len(invalid),
        "rendered": sum(r.rendered for r in results),
        "already_current": sum(r.skipped and not r.no_data for r in results),
        "no_data": sum(r.no_data for r in results),
    }
    if invalid and not dry_run:
        audit = ctx.cache / "_audit" / f"invalid-l1-{date or 'all'}.json"
        audit.parent.mkdir(parents=True, exist_ok=True)
        audit.write_text(json.dumps({
            "station_id": ctx.station,
            "excluded_sessions": [{"session_id": sid, "reason": reason} for sid, reason in invalid],
        }, indent=2) + "\n")
    if invalid:
        # Never publish stale staged views originating in excluded source sessions.
        catalog = collect_validated_staging(ctx.cache, ctx.station)
        excluded = {sid for sid, _ in invalid}
        bad = [pub_id for pub_id, record in catalog.items()
               if excluded.intersection(record["source_sessions"])]
        if bad and not dry_run:
            raise RuntimeError("Staged WEBPs include invalid L1 session(s): " + ", ".join(bad))
    LOG.info("L1=%d, invalid=%d, rendered=%d, current=%d, no-data=%d",
             numbers["sessions"], numbers["invalid_l1"], numbers["rendered"],
             numbers["already_current"], numbers["no_data"])
    return numbers


def sync(*, date: str | None = None, dry_run: bool = False) -> int:
    ctx = context()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not dry_run:
        checkout_ready(ctx.repo)
    numbers = _stage(ctx, date=date, dry_run=dry_run)
    eligible = {
        k: v for k, v in collect_validated_staging(ctx.cache, ctx.station).items()
        if (date is None or k[:8] == date) and k[:8] not in _blocked_dates(ctx)
    }
    LOG.info("%d staged publication windows eligible for R2", len(eligible))
    if dry_run:
        LOG.info("DRY-RUN: no Git/R2/cache writes")
        return 0
    settings = secrets(ctx, require_r2=bool(eligible))
    if not eligible:
        published = load_published_states(ctx.repo, ctx.station)
        if not published:
            LOG.info("Nothing to publish")
            _write_journal(ctx, "sync", "no-op", **numbers)
            return 0
        commit_site(ctx, settings)
        _write_journal(ctx, "sync", "complete", **numbers, windows=0)
        return 0

    remote = r2_client(settings)
    _write_journal(ctx, "sync", "started", windows=len(eligible), **numbers)
    try:
        result = publish_cache(
            ctx.cache, ctx.station, ctx.repo, settings["R2_BUCKET_NAME"], remote,
            selected_dates={date} if date else None,
            excluded_dates=_blocked_dates(ctx),
            git_persist=lambda _repo, _paths: commit_site(ctx, settings),
        )
    except Exception:
        _write_journal(ctx, "sync", "failed", windows=len(eligible), **numbers)
        raise
    _write_journal(ctx, "sync", "complete", **result, **numbers)
    LOG.info("Published %d windows; uploaded %d WEBPs", result["windows"], result["uploaded_objects"])
    return 0


def images(date: str) -> int:
    ctx = context()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    _stage(ctx, date=date, dry_run=False)
    LOG.info("Local images only; no R2, Git or HTML publication")
    return 0


def publish_html() -> int:
    ctx = context()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    checkout_ready(ctx.repo)
    commit_site(ctx, secrets(ctx))
    _write_journal(ctx, "html", "complete")
    return 0


def status() -> int:
    ctx = context()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from .history import load_controls
    controls = load_controls(ctx.repo, ctx.station)
    states = load_published_states(ctx.repo, ctx.station)
    catalog = collect_validated_staging(ctx.cache, ctx.station)
    n = sum(len(day["publication_windows"])
            for state in states.values() for day in state["days"].values())
    LOG.info("Published: %d verified windows in %d year(s)", n, len(states))
    LOG.info("Staged: %d windows", len(catalog))
    LOG.info("Blocked: %d hidden days; %d removed days",
             len(controls["hidden"]), len(controls["removed"]))
    journal = ctx.cache / "_publish-journal.json"
    if journal.is_file():
        recent = json.loads(journal.read_text())
        LOG.info("Last run: %s (%s) %s", recent["action"], recent["status"], recent["at_utc"])
    return 0
