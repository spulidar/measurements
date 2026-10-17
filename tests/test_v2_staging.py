"""Cross-midnight publication staging with synthetic scientific-like L1.

No R2, HTML, Git push or scientific NetCDF mutation is involved.
"""
from datetime import datetime, timezone
from pathlib import Path
import json
import os

import numpy as np
import pandas as pd
import pytest
import xarray as xr

import publisher.staging as staging
from publisher.v2 import build_state_v2, collect_validated_staging
from publisher.windows import ScientificSessionSource
from milgrau.viz.config import resolve_visualization_config


SESSION = "spu_20250511-0012Z_20250511-0737Z"


def _config():
    return {
        "visualization": {
            "output_format": "webp", "dpi": 120,
            "logos": [], "altitude_ranges_km": [5],
            "channels_to_plot": ["532.AN"],
            "quicklook": {
                "show_pbl": False, "show_tropopause": False,
                "mean_profile_smooth_bins": 3, "mean_chunk_profiles": 2,
                "max_time_gap_minutes": 10.0, "max_time_samples": 100,
                "max_altitude_bins": 100, "missing_data_color": "lightgray",
                "colormap": "jet",
            },
        },
    }


def _fake_render_api():
    def render_quicklook(**kwargs):
        path = Path(kwargs["output_folder"]) / "dummy-quicklook.webp"
        path.write_bytes(b"rendered-quicklook")
        return path

    def render_mean(ds, output_folder, name, config, root):
        path = Path(output_folder) / "dummy-mean.webp"
        path.write_bytes(b"rendered-mean")
        return path

    return {
        "validate_level1_contract": lambda ds: None,
        "validate_level2_contract": lambda ds: None,
        "resolve_visualization_config": resolve_visualization_config,
        "format_channel_name": lambda ch: "532nm AN",
        "plot_quicklook": render_quicklook,
        "plot_global_mean_rcs": render_mean,
    }


def _synthetic_session(tmp_path):
    session_dir = tmp_path / "scientific" / SESSION
    session_dir.mkdir(parents=True)
    l1 = session_dir / f"{SESSION}_L1.nc"
    times = np.array([
        "2025-05-11T00:12:00", "2025-05-11T02:59:59",
        "2025-05-11T03:00:00", "2025-05-11T07:37:00",
    ], dtype="datetime64[ns]")
    values = np.ones((len(times), 1, 2))
    xr.Dataset(
        {
            "range_corrected_signal": (("time", "channel", "altitude"), values),
            "range_corrected_signal_error": (
                ("time", "channel", "altitude"), 0.1 * values
            ),
        },
        coords={"time": times, "channel": ["532.AN"], "altitude": [0.5, 1.0]},
    ).to_netcdf(l1)
    return ScientificSessionSource(
        session_id=SESSION, station_id="spu",
        start_utc=datetime(2025, 5, 11, 0, 12, tzinfo=timezone.utc),
        end_utc=datetime(2025, 5, 11, 7, 37, 0, 1, tzinfo=timezone.utc),
        level1_path=l1,
    )


def _stage(tmp_path, monkeypatch, *, config=None):
    monkeypatch.setattr(staging, "_milgrau_render_api", _fake_render_api)
    session = _synthetic_session(tmp_path)
    cfg = config or _config()
    return session, cfg


def _run(tmp_path, session, cfg):
    root = tmp_path / "03-site-products"
    return staging.stage_publication_products(
        (session,), site_products_root=root, station_id="spu",
        timezone_name="America/Sao_Paulo", milgrau_config=cfg,
        milgrau_config_path=tmp_path / "config.yaml",
        publication_settings={"schema_version": 2, "mode": "stage-only"},
        milgrau_root=tmp_path,
    )


def test_cross_midnight_stage_produces_two_views_and_no_netcdf_split(tmp_path, monkeypatch):
    session, cfg = _stage(tmp_path, monkeypatch)
    old_bytes = session.level1_path.read_bytes()
    results = _run(tmp_path, session, cfg)
    assert [r.publication_window_id for r in results] == [
        "20250510_spu_18", "20250511_spu_00",
    ]
    assert all(r.rendered for r in results)
    root = tmp_path / "03-site-products"
    for date, hour in [("20250510", "18"), ("20250511", "00")]:
        day = root / "spu" / "2025" / "05" / date
        sidecar = json.loads((day / f"{date}_spu_{hour}.json").read_text())
        assert sidecar["source_sessions"] == [SESSION]
        assert sidecar["source_level1_files"] == [session.level1_path.name]
        assert sidecar["scientific_sessions_merged"] is False
        assert sidecar["scientific_netcdf_split"] is False
        assert sidecar["visualization_only_session_concatenation"] is False
        assert all((day / name).is_file() for name in sidecar["assets"])
    assert session.level1_path.read_bytes() == old_bytes
    catalog = collect_validated_staging(root, "spu")
    state = build_state_v2(catalog, "spu")["2025"]
    assert set(state["days"]) == {"20250510", "20250511"}


def test_valid_cache_skips_but_config_and_l1_changes_invalidate(tmp_path, monkeypatch):
    session, cfg = _stage(tmp_path, monkeypatch)
    assert sum(r.rendered for r in _run(tmp_path, session, cfg)) == 2
    assert all(not r.rendered for r in _run(tmp_path, session, cfg))
    changed = _config()
    changed["visualization"]["quicklook"]["colormap"] = "viridis"
    assert sum(r.rendered for r in _run(tmp_path, session, changed)) == 2
    stat = session.level1_path.stat()
    os.utime(session.level1_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 100_000_000))
    assert sum(r.rendered for r in _run(tmp_path, session, changed)) == 2


def test_renderer_and_logo_file_changes_invalidate(tmp_path, monkeypatch):
    session, cfg = _stage(tmp_path, monkeypatch)
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"first-logo")
    cfg["visualization"]["logos"] = [{"path": str(logo), "height": 0.04}]
    assert sum(r.rendered for r in _run(tmp_path, session, cfg)) == 2
    assert all(not r.rendered for r in _run(tmp_path, session, cfg))
    logo.write_bytes(b"second-logo")
    assert sum(r.rendered for r in _run(tmp_path, session, cfg)) == 2

    original = staging._sha256_file
    def fake_source_hash(path):
        if str(path).endswith("milgrau/viz/quicklooks.py"):
            return "f" * 64
        return original(path)
    monkeypatch.setattr(staging, "_sha256_file", fake_source_hash)
    assert sum(r.rendered for r in _run(tmp_path, session, cfg)) == 2
