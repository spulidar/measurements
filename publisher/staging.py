"""Build website-only publication products directly from MILGRAU L1/L2.

This module never writes or modifies scientific NetCDFs.  It creates an
in-memory visualization view for each station-local publication window, using
all intersecting scientific sessions.  The view may concatenate distinct
sessions along time *only for plotting*; source session identity is retained as
a coordinate and in the sidecar provenance.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import importlib
import json
import logging
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
import xarray as xr

from .windows import (
    PublicationWindow,
    ScientificSessionSource,
    iter_candidate_windows,
    sessions_intersecting_window,
)


@dataclass(frozen=True, slots=True)
class StageWindowResult:
    publication_window_id: str
    source_sessions: tuple[str, ...]
    generated_assets: tuple[Path, ...]
    rendered: bool
    skipped: bool
    no_data: bool = False


def publication_stage_dir(
    site_products_root: str | Path,
    window: PublicationWindow,
) -> Path:
    return (
        Path(site_products_root)
        / window.station_id
        / window.date_key[:4]
        / window.date_key[4:6]
        / window.date_key
    )


def publication_sidecar_path(
    site_products_root: str | Path,
    window: PublicationWindow,
) -> Path:
    return publication_stage_dir(site_products_root, window) / f"{window.publication_window_id}.json"


def _milgrau_render_api():
    """Load the public/generic MILGRAU plotting and contract functions lazily."""
    try:
        from milgrau.io.contracts import validate_level1_contract, validate_level2_contract
        from milgrau.viz.config import resolve_visualization_config
        from milgrau.viz.quicklooks import (
            format_channel_name,
            plot_quicklook,
        )
    except ImportError as exc:  # pragma: no cover - runtime environment dependent
        raise RuntimeError(
            "The current MILGRAU installation is required for website staging. "
            "Install it with `pip install -e '.[web]'`."
        ) from exc
    return {
        "validate_level1_contract": validate_level1_contract,
        "validate_level2_contract": validate_level2_contract,
        "resolve_visualization_config": resolve_visualization_config,
        "format_channel_name": format_channel_name,
        "plot_quicklook": plot_quicklook,
    }


def _utc_naive_np(value) -> np.datetime64:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    return np.datetime64(timestamp.to_datetime64(), "ns")


def _normalize_altitude_to_km(ds: xr.Dataset) -> xr.Dataset:
    altitude = np.asarray(ds["altitude"].values, dtype=np.float64)
    result = ds
    if altitude.size and float(np.nanmax(altitude)) > 100.0:
        result = ds.assign_coords(altitude=ds["altitude"] / 1000.0)
    result["altitude"].attrs["units"] = "km"
    result["altitude"].attrs["long_name"] = "Altitude above ground level"
    return result


def _subset_level1_for_window(
    session: ScientificSessionSource,
    window: PublicationWindow,
    validator,
) -> xr.Dataset | None:
    """Load only the L1 variables needed by the generic website renderers."""
    with xr.open_dataset(session.level1_path, cache=False) as source:
        validator(source)
        times = np.asarray(source["time"].values).astype("datetime64[ns]")
        mask = (times >= _utc_naive_np(window.start_utc)) & (times < _utc_naive_np(window.end_utc))
        indices = np.flatnonzero(mask)
        if not indices.size:
            return None

        variables = ["range_corrected_signal", "range_corrected_signal_error"]
        if "PBL_Height_km" in source:
            variables.append("PBL_Height_km")

        subset = source[variables].isel(time=indices).load()
        subset.attrs = dict(source.attrs)

    subset = _normalize_altitude_to_km(subset)
    subset = subset.assign_coords(
        source_session_id=(
            "time",
            np.asarray([session.session_id] * int(subset.sizes.get("time", 0)), dtype=str),
        )
    )
    return subset


def concatenate_level1_publication_view(
    subsets: Iterable[xr.Dataset],
    *,
    window: PublicationWindow,
) -> xr.Dataset | None:
    """Concatenate selected L1 slices for visualization without scientific merge."""
    materialized = [subset for subset in subsets if subset is not None and subset.sizes.get("time", 0)]
    if not materialized:
        return None

    if len(materialized) == 1:
        combined = materialized[0]
    else:
        # xarray's outer join would otherwise silently align incompatible scientific
        # altitude/channel grids. This is a public visualization, not a regridding step.
        for candidate in materialized[1:]:
            for coordinate in ("altitude", "channel"):
                if not materialized[0][coordinate].identical(candidate[coordinate]):
                    raise RuntimeError(
                        f"Scientific sessions have incompatible {coordinate} grids; "
                        "refusing implicit alignment in the publication view."
                    )
        combined = xr.concat(
            materialized,
            dim="time",
            data_vars="all",
            coords="minimal",
            compat="override",
            join="outer",
            combine_attrs="drop_conflicts",
        )

    combined = combined.sortby("time")
    times = np.asarray(combined["time"].values).astype("datetime64[ns]")
    if times.size > 1:
        duplicated = times[1:] == times[:-1]
        if np.any(duplicated):
            # Never silently average/deduplicate two scientific sessions.  A
            # duplicate timestamp here needs upstream/session inspection.
            duplicate_time = str(times[1:][duplicated][0])
            raise RuntimeError(
                "Overlapping scientific sessions produced duplicate publication time "
                f"{duplicate_time}; refusing to merge them."
            )

    source_ids = []
    if "source_session_id" in combined.coords:
        source_values = np.asarray(combined["source_session_id"].values).astype(str)
        source_ids = sorted(set(source_values))
        # Distinct sessions may interleave without exact duplicate timestamps.
        # That is still an ambiguous scientific overlap, not a safe visual concat.
        spans = []
        for source_id in source_ids:
            source_times = times[source_values == source_id]
            spans.append((source_times.min(), source_times.max(), source_id))
        spans.sort()
        for before, after in zip(spans, spans[1:]):
            if after[0] <= before[1]:
                raise RuntimeError(
                    f"Overlapping scientific session intervals in "
                    f"{window.publication_window_id}: {before[2]} and {after[2]}"
                )

    combined.attrs = dict(combined.attrs)
    combined.attrs.update(
        {
            "publication_window_id": window.publication_window_id,
            "publication_window_timezone": window.timezone_name,
            "publication_window_start_local": window.start_local.isoformat(),
            "publication_window_end_local": window.end_local.isoformat(),
            "publication_window_start_utc": window.start_utc.isoformat(),
            "publication_window_end_utc": window.end_utc.isoformat(),
            "source_session_ids": json.dumps(source_ids),
            "visualization_only_session_concatenation": "true" if len(source_ids) > 1 else "false",
            "scientific_sessions_merged": "false",
        }
    )
    return combined


def build_level1_publication_view(
    sessions: Iterable[ScientificSessionSource],
    window: PublicationWindow,
) -> tuple[xr.Dataset | None, tuple[ScientificSessionSource, ...]]:
    api = _milgrau_render_api()
    validator = api["validate_level1_contract"]
    used: list[ScientificSessionSource] = []
    subsets: list[xr.Dataset] = []

    for session in sessions_intersecting_window(sessions, window):
        subset = _subset_level1_for_window(session, window, validator)
        if subset is None:
            continue
        used.append(session)
        subsets.append(subset)

    combined = concatenate_level1_publication_view(subsets, window=window)
    return combined, tuple(used)


def level2_intersection_summary(
    sessions: Iterable[ScientificSessionSource],
    window: PublicationWindow,
    logger: logging.Logger | None = None,
) -> tuple[dict[str, Any], ...]:
    """Read intersecting L2 products directly for provenance/availability.

    Current public pages do not render a windowed L2 optical profile because
    MILGRAU's existing optical renderer consumes session-aggregate variables.
    Recomputing those scientific aggregates in the website repository would
    violate the responsibility boundary.  We therefore record exact L2 block
    support now and leave L2 figure publication to a future generic MILGRAU
    window-view renderer.
    """
    api = _milgrau_render_api()
    validator = api["validate_level2_contract"]
    summaries: list[dict[str, Any]] = []

    for session in sessions_intersecting_window(sessions, window):
        path = session.level2_path
        if path is None or not path.is_file():
            continue
        try:
            with xr.open_dataset(path, cache=False) as ds:
                validator(ds)
                time_coord = "block_time" if "block_time" in ds.coords else ("time" if "time" in ds.coords else None)
                if time_coord is None:
                    count = None
                else:
                    values = np.asarray(ds[time_coord].values).astype("datetime64[ns]")
                    mask = (values >= _utc_naive_np(window.start_utc)) & (values < _utc_naive_np(window.end_utc))
                    count = int(np.count_nonzero(mask))
                    if count == 0:
                        continue
                summaries.append(
                    {
                        "session_id": session.session_id,
                        "file": path.name,
                        "time_coordinate": time_coord,
                        "intersecting_samples": count,
                    }
                )
        except Exception as exc:
            raise RuntimeError(f"Invalid Level 2 publication source {path}: {exc}") from exc
    return tuple(summaries)


def _sha256_file(path: Path) -> str:
    """Hash a bounded file stream (logos/renderers/assets), not scientific arrays."""
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_fingerprint(
    window: PublicationWindow,
    sessions: Iterable[ScientificSessionSource],
    milgrau_config_path: Path,
    publication_settings: Mapping[str, Any],
    milgrau_config: Mapping[str, Any],
    milgrau_root: Path,
) -> str:
    """Capture scientific sources, recipes, logos and actual rendering implementation."""
    digest = sha256()
    def add(label: str, value: str) -> None:
        digest.update(label.encode("utf-8") + b"\\0" + value.encode("utf-8") + b"\\n")

    add("publication_window_id", window.publication_window_id)
    add("start_utc", window.start_utc.isoformat())
    add("end_utc", window.end_utc.isoformat())
    add("publication_settings", json.dumps(dict(publication_settings), sort_keys=True, default=str))
    add("milgrau_config", json.dumps(dict(milgrau_config), sort_keys=True, default=str))

    for config_file in (
        milgrau_config_path,
        Path(str(milgrau_config.get("_station_config_path", ""))),
    ):
        if config_file.is_file():
            add(str(config_file), _sha256_file(config_file))

    # Version numbers are informational, not a substitute for hashing code.
    import milgrau
    add("milgrau_version", str(getattr(milgrau, "__version__", "unknown")))
    for module_name in (
        "milgrau.viz.quicklooks",
        "milgrau.viz.style",
        "milgrau.viz.config",
        "milgrau.io.contracts",
    ):
        module = importlib.import_module(module_name)
        module_file = Path(module.__file__)
        if not module_file.is_file():
            raise RuntimeError(f"Missing MILGRAU renderer dependency: {module_name}")
        add(module_name, _sha256_file(module_file))

    for module_path in (
        Path(__file__), Path(__file__).with_name("windows.py"),
    ):
        add(str(module_path.name), _sha256_file(module_path))

    from milgrau.viz.style import configured_logos, resolve_logo_path
    for logo_name, height in configured_logos(milgrau_config):
        logo_path = resolve_logo_path(milgrau_root, logo_name)
        if not logo_path.is_file():
            raise FileNotFoundError(
                f"Selected MILGRAU visualization logo does not exist: {logo_path}"
            )
        add(f"logo:{logo_name}:{height}", _sha256_file(logo_path))

    for session in sorted(sessions, key=lambda item: item.session_id):
        add("source_session_id", session.session_id)
        for source in (session.level1_path, session.level2_path):
            if source is None:
                continue
            if not source.is_file():
                raise FileNotFoundError(f"Scientific source disappeared: {source}")
            stat = source.stat()
            add(f"source:{source.resolve()}", f"{stat.st_size}:{stat.st_mtime_ns}")
    return digest.hexdigest()


def _sidecar_is_current(path: Path, fingerprint: str) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if payload.get("source_fingerprint") != fingerprint:
        return False
    if payload.get("scientific_sessions_merged") is not False:
        return False
    if payload.get("scientific_netcdf_split") is not False:
        return False
    assets = payload.get("assets")
    hashes = payload.get("asset_hashes")
    if not isinstance(assets, list) or not assets or not isinstance(hashes, dict):
        return False
    if len(assets) != len(set(assets)) or set(assets) != set(hashes):
        return False
    for name in assets:
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".webp"):
            return False
        asset = path.parent / name
        if not asset.is_file() or _sha256_file(asset) != hashes[name]:
            return False
    return True


def _render_config(milgrau_config: Mapping[str, Any], publication_settings: Mapping[str, Any]) -> dict:
    # Publication cannot override MILGRAU's visual recipe.
    from milgrau.viz.config import resolve_visualization_config
    config = deepcopy(dict(milgrau_config))
    if resolve_visualization_config(config).output_format != "webp":
        raise ValueError("Website publication requires MILGRAU visualization.output_format: webp.")
    return config


def _write_sidecar(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def render_publication_window(
    window: PublicationWindow,
    sessions: Iterable[ScientificSessionSource],
    *,
    site_products_root: str | Path,
    milgrau_config: Mapping[str, Any],
    milgrau_config_path: str | Path,
    publication_settings: Mapping[str, Any],
    milgrau_root: str | Path,
    logger: logging.Logger | None = None,
    force: bool = False,
    dry_run: bool = False,
    published_fingerprints: Mapping[str, str] | None = None,
) -> StageWindowResult:
    """Render one six-hour website view from intersecting scientific sessions."""
    candidates = sessions_intersecting_window(sessions, window)
    if not candidates:
        return StageWindowResult(window.publication_window_id, (), (), False, True, True)

    config_path = Path(milgrau_config_path)
    fingerprint = _source_fingerprint(
        window, candidates, config_path, publication_settings, milgrau_config, Path(milgrau_root)
    )
    sidecar = publication_sidecar_path(site_products_root, window)
    if (
        not force
        and not sidecar.exists()
        and published_fingerprints is not None
        and published_fingerprints.get(window.publication_window_id) == fingerprint
    ):
        if logger is not None:
            logger.info("Already published in Git state: %s (no cache required)", window.publication_window_id)
        return StageWindowResult(
            window.publication_window_id,
            tuple(session.session_id for session in candidates),
            (),
            False, True, False,
        )
    if not force and _sidecar_is_current(sidecar, fingerprint):
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assets = tuple(sidecar.parent / str(name) for name in payload.get("assets", []))
        return StageWindowResult(
            window.publication_window_id,
            tuple(str(value) for value in payload.get("source_sessions", [])),
            assets,
            False,
            True,
            False,
        )

    if dry_run:
        if logger is not None:
            logger.info(
                "[DRY-RUN] would render %s from %d candidate scientific session(s)",
                window.publication_window_id,
                len(candidates),
            )
        return StageWindowResult(
            window.publication_window_id,
            tuple(session.session_id for session in candidates),
            (),
            False,
            True,
            False,
        )

    view, used_sessions = build_level1_publication_view(candidates, window)
    if view is None or not used_sessions:
        if logger is not None:
            logger.info("No Level 1 profiles inside publication window %s", window.publication_window_id)
        return StageWindowResult(window.publication_window_id, (), (), False, True, True)

    api = _milgrau_render_api()
    render_config = _render_config(milgrau_config, publication_settings)
    resolved = api["resolve_visualization_config"](render_config)
    format_channel_name = api["format_channel_name"]
    plot_quicklook = api["plot_quicklook"]

    day_dir = publication_stage_dir(site_products_root, window)
    day_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = day_dir / f".{window.publication_window_id}.rendering"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    generated_temp: list[tuple[Path, Path]] = []
    available_channels = {str(value) for value in view["channel"].values}
    pbl_da = view["PBL_Height_km"] if "PBL_Height_km" in view else None
    cpt_km = np.nan
    lrt_km = np.nan
    if len(used_sessions) == 1:
        try:
            cpt_km = float(view.attrs.get("tropopause_cpt_km", np.nan))
            lrt_km = float(view.attrs.get("tropopause_lrt_km", np.nan))
        except (TypeError, ValueError):
            cpt_km = lrt_km = np.nan

    window_start_utc = pd.Timestamp(window.start_utc).tz_convert("UTC").tz_localize(None)
    window_end_utc = pd.Timestamp(window.end_utc).tz_convert("UTC").tz_localize(None)

    try:
        for channel_name in resolved.channels_to_plot:
            if channel_name not in available_channels:
                continue
            signal = view["range_corrected_signal"].sel(channel=channel_name)
            error = view["range_corrected_signal_error"].sel(channel=channel_name)
            public_channel = format_channel_name(channel_name).replace(" ", "_")

            for max_altitude in resolved.altitude_ranges_km:
                signal_slice = signal.sel(altitude=slice(0, max_altitude))
                error_slice = error.sel(altitude=slice(0, max_altitude))
                if signal_slice.size == 0:
                    continue
                generic_path = plot_quicklook(
                    data_slice=signal_slice,
                    error_slice=error_slice,
                    max_altitude=max_altitude,
                    channel_name=channel_name,
                    ds=view,
                    output_folder=temp_dir,
                    file_name_prefix=window.publication_window_id,
                    config=render_config,
                    root_dir=Path(milgrau_root),
                    session_id=None,
                    timezone_name=window.timezone_name,
                    pbl_da=pbl_da,
                    cpt_km=cpt_km,
                    lrt_km=lrt_km,
                    time_range_utc=(window_start_utc, window_end_utc),
                )
                target = day_dir / (
                    f"rcs_{window.publication_window_id}_{public_channel}_"
                    f"{float(max_altitude):g}km.webp"
                )
                generated_temp.append((Path(generic_path), target))

        if not generated_temp:
            raise RuntimeError(
                f"No compatible Level 1 website figure was generated for {window.publication_window_id}."
            )

        # Commit the publication assets only after all requested renders succeed.
        generated_targets: list[Path] = []
        for source, target in generated_temp:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            generated_targets.append(target)

        keep_names = {path.name for path in generated_targets}
        for stale in day_dir.glob(f"rcs_{window.publication_window_id}_*.webp"):
            if stale.name not in keep_names:
                stale.unlink()

        level2_summary = level2_intersection_summary(used_sessions, window, logger=logger)
        sidecar_payload = {
            "schema_version": 1,
            "publication_window_id": window.publication_window_id,
            "window_start": window.window_start,
            "window_label": window.label,
            "timezone": window.timezone_name,
            "start_local": window.start_local.isoformat(),
            "end_local": window.end_local.isoformat(),
            "start_utc": window.start_utc.isoformat(),
            "end_utc": window.end_utc.isoformat(),
            "source_sessions": [session.session_id for session in used_sessions],
            "source_level1_files": [session.level1_path.name for session in used_sessions],
            "source_level2_files": [item["file"] for item in level2_summary],
            "level2_intersections": list(level2_summary),
            "visualization_only_session_concatenation": len(used_sessions) > 1,
            "scientific_sessions_merged": False,
            "scientific_netcdf_split": False,
            "source_fingerprint": fingerprint,
            "renderer_owner": "milgrau.generic",
            "assets": sorted(path.name for path in generated_targets),
            "asset_hashes": {path.name: _sha256_file(path) for path in generated_targets},
        }
        _write_sidecar(sidecar, sidecar_payload)

        if logger is not None:
            logger.info(
                "Staged %s from %d scientific session(s): %d asset(s)",
                window.publication_window_id,
                len(used_sessions),
                len(generated_targets),
            )
        return StageWindowResult(
            window.publication_window_id,
            tuple(session.session_id for session in used_sessions),
            tuple(sorted(generated_targets)),
            True,
            False,
            False,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def stage_publication_products(
    sessions: Iterable[ScientificSessionSource],
    *,
    site_products_root: str | Path,
    station_id: str,
    timezone_name: str,
    milgrau_config: Mapping[str, Any],
    milgrau_config_path: str | Path,
    publication_settings: Mapping[str, Any],
    milgrau_root: str | Path,
    publish_year: str | None = None,
    publish_date: str | None = None,
    excluded_dates: set[str] | frozenset[str] = frozenset(),
    force: bool = False,
    dry_run: bool = False,
    logger: logging.Logger | None = None,
    published_fingerprints: Mapping[str, str] | None = None,
) -> tuple[StageWindowResult, ...]:
    session_tuple = tuple(sessions)
    results: list[StageWindowResult] = []
    for window in iter_candidate_windows(
        session_tuple,
        station_id=station_id,
        timezone_name=timezone_name,
        publish_year=publish_year,
        publish_date=publish_date,
    ):
        if window.date_key in excluded_dates:
            continue
        result = render_publication_window(
            window,
            session_tuple,
            site_products_root=site_products_root,
            milgrau_config=milgrau_config,
            milgrau_config_path=milgrau_config_path,
            publication_settings=publication_settings,
            milgrau_root=milgrau_root,
            logger=logger,
            force=force,
            dry_run=dry_run,
            published_fingerprints=published_fingerprints,
        )
        results.append(result)
    return tuple(results)
