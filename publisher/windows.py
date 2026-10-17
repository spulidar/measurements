"""Publication-window ownership and MILGRAU scientific-session discovery.

The public website owns the four station-local civil-time windows.  MILGRAU
owns scientific sessions.  These identities are deliberately distinct:

* scientific session: ``station_YYYYMMDD-HHMMZ_YYYYMMDD-HHMMZ``
* publication window: ``YYYYMMDD_station_HH`` where HH is 00, 06, 12 or 18

A scientific session may contribute to one or more publication windows and a
publication window may contain data from several scientific sessions.  Nothing
in this module splits or renames a scientific NetCDF product.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

import numpy as np
import pandas as pd
import xarray as xr
from zoneinfo import ZoneInfo

PUBLICATION_WINDOW_STARTS: tuple[str, ...] = ("00", "06", "12", "18")
PUBLICATION_WINDOW_LABELS: dict[str, str] = {
    "00": "00–06",
    "06": "06–12",
    "12": "12–18",
    "18": "18–24",
}


def _parse_local_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid local publication date {value!r}; expected YYYYMMDD.") from exc


def _normalize_window_start(value: str | int) -> str:
    raw = str(value).strip()
    if not raw.isdigit():
        raise ValueError(f"Invalid publication window start: {value!r}")
    normalized = f"{int(raw):02d}"
    if normalized not in PUBLICATION_WINDOW_STARTS:
        raise ValueError(
            f"Invalid publication window start {value!r}; expected one of "
            f"{', '.join(PUBLICATION_WINDOW_STARTS)}."
        )
    return normalized


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Scientific-session timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class PublicationWindow:
    """One website-local six-hour publication view."""

    local_date: date
    station_id: str
    window_start: str
    timezone_name: str

    @classmethod
    def build(
        cls,
        local_date: str | date,
        station_id: str,
        window_start: str | int,
        timezone_name: str,
    ) -> "PublicationWindow":
        station = str(station_id).strip().lower()
        if not station:
            raise ValueError("station_id must be non-empty.")
        # ZoneInfo construction is also the validation step for the IANA name.
        ZoneInfo(str(timezone_name))
        return cls(
            local_date=_parse_local_date(local_date),
            station_id=station,
            window_start=_normalize_window_start(window_start),
            timezone_name=str(timezone_name),
        )

    @property
    def date_key(self) -> str:
        return self.local_date.strftime("%Y%m%d")

    @property
    def publication_window_id(self) -> str:
        return f"{self.date_key}_{self.station_id}_{self.window_start}"

    @property
    def label(self) -> str:
        return PUBLICATION_WINDOW_LABELS[self.window_start]

    @property
    def start_local(self) -> datetime:
        zone = ZoneInfo(self.timezone_name)
        return datetime.combine(
            self.local_date,
            time(hour=int(self.window_start)),
            tzinfo=zone,
        )

    @property
    def end_local(self) -> datetime:
        return self.start_local + timedelta(hours=6)

    @property
    def start_utc(self) -> datetime:
        return self.start_local.astimezone(timezone.utc)

    @property
    def end_utc(self) -> datetime:
        return self.end_local.astimezone(timezone.utc)

    def intersects(self, start_utc: datetime, end_utc: datetime) -> bool:
        start = _aware_utc(start_utc)
        end = _aware_utc(end_utc)
        if end <= start:
            return False
        # Half-open intervals avoid double-counting a profile exactly on a
        # publication boundary.
        return start < self.end_utc and end > self.start_utc


@dataclass(frozen=True, slots=True)
class ScientificSessionSource:
    """Pointers to one unchanged MILGRAU scientific session."""

    session_id: str
    station_id: str
    start_utc: datetime
    end_utc: datetime
    level1_path: Path
    level2_path: Path | None = None

    def intersects(self, window: PublicationWindow) -> bool:
        return window.intersects(self.start_utc, self.end_utc)


def publication_windows_for_date(
    local_date: str | date,
    station_id: str,
    timezone_name: str,
) -> tuple[PublicationWindow, ...]:
    return tuple(
        PublicationWindow.build(local_date, station_id, start, timezone_name)
        for start in PUBLICATION_WINDOW_STARTS
    )


def windows_intersecting_interval(
    start_utc: datetime,
    end_utc: datetime,
    *,
    station_id: str,
    timezone_name: str,
) -> tuple[PublicationWindow, ...]:
    """Return all local publication windows intersecting a UTC interval."""
    start = _aware_utc(start_utc)
    end = _aware_utc(end_utc)
    if end <= start:
        return ()

    zone = ZoneInfo(timezone_name)
    first_local_date = start.astimezone(zone).date()
    # The interval is half-open; use one microsecond before the end to avoid
    # creating the next civil day when the session ends exactly at midnight.
    last_local_date = (end - timedelta(microseconds=1)).astimezone(zone).date()

    result: list[PublicationWindow] = []
    current = first_local_date
    while current <= last_local_date:
        for window in publication_windows_for_date(current, station_id, timezone_name):
            if window.intersects(start, end):
                result.append(window)
        current += timedelta(days=1)
    return tuple(result)


def windows_intersecting_session(
    session: ScientificSessionSource,
    timezone_name: str,
) -> tuple[PublicationWindow, ...]:
    return windows_intersecting_interval(
        session.start_utc,
        session.end_utc,
        station_id=session.station_id,
        timezone_name=timezone_name,
    )


def sessions_intersecting_window(
    sessions: Iterable[ScientificSessionSource],
    window: PublicationWindow,
) -> tuple[ScientificSessionSource, ...]:
    return tuple(session for session in sessions if session.intersects(window))


def _milgrau_session_api():
    """Import MILGRAU identity helpers lazily so historical-only tools still run."""
    try:
        from milgrau.io.paths import product_session_id, session_id_parts
    except ImportError as exc:  # pragma: no cover - depends on runtime install
        raise RuntimeError(
            "MILGRAU must be installed in the same environment to render website products. "
            "Install the session-refactor checkout with `pip install -e ../milgrau`."
        ) from exc
    return product_session_id, session_id_parts


def discover_scientific_sessions(
    processed_data_root: str | Path,
    station_id: str,
) -> tuple[ScientificSessionSource, ...]:
    """Discover canonical MILGRAU L1/L2 products, never scientific figure WEBPs."""
    root = Path(processed_data_root) / str(station_id).strip().lower()
    if not root.exists():
        return ()

    product_session_id, session_id_parts = _milgrau_session_api()
    sessions: list[ScientificSessionSource] = []
    seen: set[str] = set()

    for level1_path in sorted(root.rglob("*_L1.nc")):
        try:
            session_id = product_session_id(level1_path)
            parsed_station, start_utc, end_utc = session_id_parts(session_id)
        except ValueError:
            continue

        # Only the canonical complete-session L1 belongs in the publication
        # source catalog. SCC derivatives and explicit variants are separate
        # scientific products and must not masquerade as sessions here.
        if level1_path.name != f"{session_id}_L1.nc":
            continue
        if parsed_station != str(station_id).strip().lower():
            continue
        if session_id in seen:
            raise RuntimeError(f"Duplicate canonical Level 1 product for session {session_id}.")
        seen.add(session_id)

        # Session IDs intentionally have minute precision, while exact profile
        # times remain in the scientific NetCDF.  Publication-window routing
        # must use those exact profile times: using the ID end minute could
        # otherwise drop profiles that extend seconds beyond a six-hour
        # website boundary.  Read only the time coordinate here; scientific
        # signal arrays remain lazy/unloaded until a selected window is staged.
        try:
            with xr.open_dataset(level1_path, cache=False) as source:
                time_values = np.asarray(source["time"].values).astype("datetime64[ns]")
        except (KeyError, OSError, ValueError) as exc:
            raise RuntimeError(
                f"Cannot read exact Level 1 time coordinate for {session_id}: {level1_path}"
            ) from exc
        if not time_values.size:
            continue
        finite_times = time_values[~np.isnat(time_values)]
        if not finite_times.size:
            continue
        exact_start = pd.Timestamp(finite_times.min()).tz_localize("UTC").to_pydatetime()
        # Scientific profile timestamps are samples, not interval endpoints.
        # Add one microsecond so a final profile exactly on a publication
        # boundary still makes the session a candidate for that half-open view.
        exact_end = (pd.Timestamp(finite_times.max()) + pd.Timedelta(microseconds=1)).tz_localize(
            "UTC"
        ).to_pydatetime()

        level2_candidate = level1_path.with_name(f"{session_id}_L2.nc")
        sessions.append(
            ScientificSessionSource(
                session_id=session_id,
                station_id=parsed_station,
                start_utc=exact_start,
                end_utc=exact_end,
                level1_path=level1_path,
                level2_path=level2_candidate if level2_candidate.is_file() else None,
            )
        )

    return tuple(sorted(sessions, key=lambda item: (item.start_utc, item.end_utc, item.session_id)))


def iter_candidate_windows(
    sessions: Iterable[ScientificSessionSource],
    *,
    station_id: str,
    timezone_name: str,
    publish_year: str | None = None,
    publish_date: str | None = None,
) -> Iterator[PublicationWindow]:
    """Yield unique publication windows selected by optional local-date filters."""
    if publish_date is not None:
        for window in publication_windows_for_date(publish_date, station_id, timezone_name):
            yield window
        return

    unique: dict[str, PublicationWindow] = {}
    for session in sessions:
        for window in windows_intersecting_session(session, timezone_name):
            if publish_year is not None and window.date_key[:4] != publish_year:
                continue
            unique[window.publication_window_id] = window

    for key in sorted(unique):
        yield unique[key]
