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
from typing import Callable, Iterable, Iterator

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
            "Install the current MILGRAU checkout using `pip install -e '.[web]'`."
        ) from exc
    return product_session_id, session_id_parts


def _discovery_utc_bounds(
    *, publish_date: str | None, publish_year: str | None, timezone_name: str | None,
) -> tuple[datetime, datetime] | None:
    """Conservative early filter only; exact routing always reads NetCDF timestamps.

    Session identifiers use minute precision. A two-day guard around the
    selected station-local interval prevents discarding any normal boundary
    case before reading the exact L1 time coordinate.
    """
    if publish_date is None and publish_year is None:
        return None
    if not timezone_name:
        raise ValueError("Station timezone is required for scoped L1 discovery.")
    zone = ZoneInfo(timezone_name)
    if publish_date is not None:
        beginning = _parse_local_date(publish_date)
        ending = beginning + timedelta(days=1)
    else:
        if publish_year is None or len(publish_year) != 4 or not publish_year.isdigit():
            raise ValueError("publish_year must be YYYY")
        beginning = date(int(publish_year), 1, 1)
        ending = date(int(publish_year) + 1, 1, 1)
    lower = datetime.combine(beginning, time.min, tzinfo=zone).astimezone(timezone.utc)
    upper = datetime.combine(ending, time.min, tzinfo=zone).astimezone(timezone.utc)
    guard = timedelta(days=2)
    return lower - guard, upper + guard


def discover_scientific_sessions(
    processed_data_root: str | Path,
    station_id: str,
    *,
    publish_date: str | None = None,
    publish_year: str | None = None,
    timezone_name: str | None = None,
    skip_invalid_l1: bool = False,
    report_issue: Callable[[str, Path, str], None] | None = None,
) -> tuple[ScientificSessionSource, ...]:
    """Discover canonical L1/L2, failing on time corruption unless opted out.

    The scope filter is conservative and based only on canonical session IDs.
    Exact publication windows still use the complete NetCDF time coordinate.
    Never sort or deduplicate scientific samples.
    """
    if publish_date is not None and publish_year is not None:
        raise ValueError("publish_date and publish_year are mutually exclusive")
    utc_bounds = _discovery_utc_bounds(
        publish_date=publish_date, publish_year=publish_year, timezone_name=timezone_name
    )
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
        except ValueError as exc:
            raise RuntimeError(f"Invalid Level 1 source name: {level1_path}") from exc

        # Only canonical scientific sessions: no SCC derivative or local variant.
        if level1_path.name != f"{session_id}_L1.nc":
            continue
        if parsed_station != str(station_id).strip().lower():
            raise RuntimeError(
                f"Station mismatch in scientific L1 source {level1_path}: {parsed_station}."
            )
        if utc_bounds is not None:
            lower, upper = utc_bounds
            if end_utc <= lower or start_utc >= upper:
                continue
        if session_id in seen:
            raise RuntimeError(f"Duplicate canonical Level 1 product for session {session_id}.")
        seen.add(session_id)

        # Exact sampling (not the minute-precision session identifier) decides
        # public-window membership. Read no signal arrays here.
        try:
            with xr.open_dataset(level1_path, cache=False) as source:
                time_values = np.asarray(source["time"].values).astype("datetime64[ns]")
            if not time_values.size or np.isnat(time_values).any():
                raise ValueError("Missing or invalid L1 time samples")
            bad_indices = np.flatnonzero(time_values[1:] <= time_values[:-1])
            if bad_indices.size:
                first = int(bad_indices[0])
                earlier, later = time_values[first], time_values[first + 1]
                kind = "duplicate" if earlier == later else "backward"
                raise ValueError(
                    f"{bad_indices.size} non-increasing adjacent time pair(s); "
                    f"first {kind} at indices {first}/{first + 1}: "
                    f"{earlier} -> {later}"
                )
        except (KeyError, OSError, ValueError, TypeError, OverflowError) as exc:
            detail = f"{exc} — {level1_path}"
            if skip_invalid_l1:
                if report_issue is not None:
                    report_issue(session_id, level1_path, detail)
                continue
            raise RuntimeError(
                f"Invalid L1 time coordinate for {session_id}: {detail}. "
                "Inspect/regenerate this scientific L1; or explicitly use "
                "--skip-invalid-l1 to quarantine it from this publication run."
            ) from exc

        exact_start = pd.Timestamp(time_values[0]).tz_localize("UTC").to_pydatetime()
        exact_end = (pd.Timestamp(time_values[-1]) + pd.Timedelta(microseconds=1)).tz_localize(
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
