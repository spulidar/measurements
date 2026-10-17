from datetime import datetime, timezone
from pathlib import Path

from publisher.windows import (
    PublicationWindow,
    ScientificSessionSource,
    publication_windows_for_date,
    windows_intersecting_session,
)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_publication_window_is_local_and_has_explicit_publication_identity():
    window = PublicationWindow.build("20250510", "spu", "18", "America/Sao_Paulo")

    assert window.publication_window_id == "20250510_spu_18"
    assert window.label == "18–24"
    assert window.start_local.isoformat() == "2025-05-10T18:00:00-03:00"
    assert window.end_local.isoformat() == "2025-05-11T00:00:00-03:00"
    assert window.start_utc == _utc("2025-05-10T21:00:00Z")
    assert window.end_utc == _utc("2025-05-11T03:00:00Z")


def test_canonical_cross_midnight_session_maps_to_two_website_windows():
    session = ScientificSessionSource(
        session_id="spu_20250511-0012Z_20250511-0737Z",
        station_id="spu",
        start_utc=_utc("2025-05-11T00:12:00Z"),
        end_utc=_utc("2025-05-11T07:37:00Z"),
        level1_path=Path("spu_20250511-0012Z_20250511-0737Z_L1.nc"),
    )

    windows = windows_intersecting_session(session, "America/Sao_Paulo")
    assert [window.publication_window_id for window in windows] == [
        "20250510_spu_18",
        "20250511_spu_00",
    ]


def test_four_windows_are_owned_by_measurements_per_local_day():
    windows = publication_windows_for_date("20250511", "spu", "America/Sao_Paulo")
    assert [window.window_start for window in windows] == ["00", "06", "12", "18"]
    assert [window.publication_window_id for window in windows] == [
        "20250511_spu_00",
        "20250511_spu_06",
        "20250511_spu_12",
        "20250511_spu_18",
    ]


def test_session_discovery_uses_exact_l1_profile_times_not_minute_id(tmp_path, monkeypatch):
    import numpy as np
    import xarray as xr
    import publisher.windows as windows_module

    station_root = tmp_path / "spu" / "2025" / "05"
    session_id = "spu_20250511-0259Z_20250511-0300Z"
    session_dir = station_root / session_id
    session_dir.mkdir(parents=True)
    l1 = session_dir / f"{session_id}_L1.nc"
    xr.Dataset(
        {"dummy": ("time", np.array([1.0, 2.0]))},
        coords={"time": np.array(["2025-05-11T02:59:50", "2025-05-11T03:00:30"], dtype="datetime64[ns]")},
    ).to_netcdf(l1)

    def product_session_id(_path):
        return session_id

    def session_id_parts(_session_id):
        # Deliberately truncated to the ID's minute precision.
        return "spu", _utc("2025-05-11T02:59:00Z"), _utc("2025-05-11T03:00:00Z")

    monkeypatch.setattr(
        windows_module,
        "_milgrau_session_api",
        lambda: (product_session_id, session_id_parts),
    )

    sessions = windows_module.discover_scientific_sessions(tmp_path, "spu")
    assert len(sessions) == 1
    assert sessions[0].start_utc == _utc("2025-05-11T02:59:50Z")
    assert sessions[0].end_utc > _utc("2025-05-11T03:00:30Z")

    mapped = windows_module.windows_intersecting_session(sessions[0], "America/Sao_Paulo")
    # 03:00Z is local midnight, so exact NetCDF timing contributes to both local days.
    assert [window.publication_window_id for window in mapped] == [
        "20250510_spu_18",
        "20250511_spu_00",
    ]
