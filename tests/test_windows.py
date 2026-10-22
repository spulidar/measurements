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


def _write_l1_with_times(root: Path, session_id: str, times: list[str]) -> Path:
    import numpy as np
    import xarray as xr

    year = session_id.split("_")[1][:4]
    month = session_id.split("_")[1][4:6]
    directory = root / "spu" / year / month / session_id
    directory.mkdir(parents=True)
    path = directory / f"{session_id}_L1.nc"
    xr.Dataset(
        {"dummy": ("time", np.arange(len(times), dtype=float))},
        coords={"time": np.array(times, dtype="datetime64[ns]")},
    ).to_netcdf(path)
    return path


def test_scoped_l1_discovery_ignores_unrelated_corrupt_2021_source(tmp_path):
    from publisher.windows import discover_scientific_sessions

    invalid = "spu_20210517-2018Z_20210517-2224Z"
    valid = "spu_20240913-1549Z_20240913-1750Z"
    _write_l1_with_times(
        tmp_path, invalid,
        ["2021-05-17T20:18:00", "2021-05-17T20:18:00", "2021-05-17T22:24:00"],
    )
    _write_l1_with_times(
        tmp_path, valid,
        ["2024-09-13T15:49:00", "2024-09-13T17:50:00"],
    )
    selected = discover_scientific_sessions(
        tmp_path, "spu",
        publish_date="20240913", timezone_name="America/Sao_Paulo",
    )
    assert [session.session_id for session in selected] == [valid]

    by_year = discover_scientific_sessions(
        tmp_path, "spu",
        publish_year="2024", timezone_name="America/Sao_Paulo",
    )
    assert [session.session_id for session in by_year] == [valid]


def test_l1_discovery_fails_on_duplicate_or_backward_timestamps_without_modifying_files(tmp_path):
    import pytest
    from publisher.windows import discover_scientific_sessions

    bad = "spu_20210517-2018Z_20210517-2224Z"
    path = _write_l1_with_times(
        tmp_path, bad,
        ["2021-05-17T20:18:00", "2021-05-17T20:19:00", "2021-05-17T20:19:00"],
    )
    original = path.read_bytes()
    with pytest.raises(RuntimeError, match="first duplicate at indices 1/2"):
        discover_scientific_sessions(tmp_path, "spu")
    assert path.read_bytes() == original

    _write_l1_with_times(
        tmp_path, bad,
        ["2021-05-17T20:18:00", "2021-05-17T20:19:00", "2021-05-17T20:18:30"],
    )
    with pytest.raises(RuntimeError, match="first backward at indices 1/2"):
        discover_scientific_sessions(tmp_path, "spu")


def test_opt_in_exclusion_reports_invalid_l1_and_keeps_valid_science(tmp_path):
    from publisher.windows import discover_scientific_sessions

    invalid = "spu_20210517-2018Z_20210517-2224Z"
    valid = "spu_20240913-1549Z_20240913-1750Z"
    path = _write_l1_with_times(
        tmp_path, invalid,
        ["2021-05-17T20:18:00", "2021-05-17T20:17:00"],
    )
    original = path.read_bytes()
    _write_l1_with_times(
        tmp_path, valid,
        ["2024-09-13T15:49:00", "2024-09-13T17:50:00"],
    )
    issues = []
    selected = discover_scientific_sessions(
        tmp_path, "spu", skip_invalid_l1=True,
        report_issue=lambda session_id, source, reason: issues.append((session_id, source, reason)),
    )
    assert [item.session_id for item in selected] == [valid]
    assert len(issues) == 1
    assert issues[0][0] == invalid
    assert issues[0][1] == path
    assert "backward" in issues[0][2]
    assert path.read_bytes() == original


def test_datetime_boundary_filter_keeps_cross_midnight_scientific_session(tmp_path):
    from publisher.windows import discover_scientific_sessions

    identifier = "spu_20250511-0012Z_20250511-0737Z"
    _write_l1_with_times(
        tmp_path, identifier,
        ["2025-05-11T00:12:00", "2025-05-11T03:00:00", "2025-05-11T07:37:00"],
    )
    for date in ["20250510", "20250511"]:
        sessions = discover_scientific_sessions(
            tmp_path, "spu",
            publish_date=date, timezone_name="America/Sao_Paulo",
        )
        assert [item.session_id for item in sessions] == [identifier]
