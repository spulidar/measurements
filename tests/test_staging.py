import numpy as np
import pytest
import xarray as xr

from publisher.staging import concatenate_level1_publication_view
from publisher.windows import PublicationWindow


def _subset(times, session_id):
    altitude = np.array([0.0, 1.0])
    channels = np.array(["532.AN"])
    shape = (len(times), 1, 2)
    values = np.arange(np.prod(shape), dtype=float).reshape(shape) + 1.0
    ds = xr.Dataset(
        {
            "range_corrected_signal": (("time", "channel", "altitude"), values),
            "range_corrected_signal_error": (("time", "channel", "altitude"), values * 0.1),
        },
        coords={
            "time": np.asarray(times, dtype="datetime64[ns]"),
            "channel": channels,
            "altitude": altitude,
        },
    )
    return ds.assign_coords(
        source_session_id=("time", np.asarray([session_id] * len(times), dtype=str))
    )


def test_distinct_sessions_are_only_concatenated_for_visualization_and_gap_remains():
    window = PublicationWindow.build("20250510", "spu", "18", "America/Sao_Paulo")
    first = _subset(
        ["2025-05-11T00:12:00", "2025-05-11T00:13:00"],
        "spu_20250511-0012Z_20250511-0013Z",
    )
    second = _subset(
        ["2025-05-11T02:10:00", "2025-05-11T02:11:00"],
        "spu_20250511-0210Z_20250511-0211Z",
    )

    combined = concatenate_level1_publication_view([first, second], window=window)

    assert combined.sizes["time"] == 4
    assert list(combined["source_session_id"].values) == [
        "spu_20250511-0012Z_20250511-0013Z",
        "spu_20250511-0012Z_20250511-0013Z",
        "spu_20250511-0210Z_20250511-0211Z",
        "spu_20250511-0210Z_20250511-0211Z",
    ]
    # No resampling/filling is performed: the 117-minute gap is still present.
    times = combined["time"].values.astype("datetime64[m]")
    assert int((times[2] - times[1]) / np.timedelta64(1, "m")) == 117
    assert combined.attrs["scientific_sessions_merged"] == "false"
    assert combined.attrs["visualization_only_session_concatenation"] == "true"


def test_overlapping_session_timestamps_are_not_silently_merged():
    window = PublicationWindow.build("20250510", "spu", "18", "America/Sao_Paulo")
    first = _subset(["2025-05-11T00:12:00"], "session-a")
    second = _subset(["2025-05-11T00:12:00"], "session-b")

    with pytest.raises(RuntimeError, match="duplicate publication time"):
        concatenate_level1_publication_view([first, second], window=window)


def test_interleaved_session_ranges_fail_even_without_equal_timestamps():
    window = PublicationWindow.build("20250510", "spu", "18", "America/Sao_Paulo")
    first = _subset(
        ["2025-05-11T00:12:00", "2025-05-11T00:15:00"], "session-a"
    )
    second = _subset(
        ["2025-05-11T00:13:00", "2025-05-11T00:14:00"], "session-b"
    )
    with pytest.raises(RuntimeError, match="Overlapping scientific session intervals"):
        concatenate_level1_publication_view([first, second], window=window)
