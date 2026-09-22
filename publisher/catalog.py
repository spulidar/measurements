"""Discovery and normalization for SPU lidar web-publication assets."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import re
from typing import Iterable

STATION_ID = "spu"
CANONICAL_PERIODS = ("00", "06", "12", "18")
PERIOD_LABELS = {
    "00": "00–06",
    "06": "06–12",
    "12": "12–18",
    "18": "18–24",
}
LEGACY_PERIODS = {
    "saam": ("legacy_am", "06–12", ("06",)),
    "sapm": ("legacy_pm", "12–18", ("12",)),
    "sant": ("legacy_nt", "00–06 + 18–24", ("00", "18")),
}

_CANONICAL_QUICKLOOK_RE = re.compile(
    r"^rcs_(?P<date>\d{8})_(?P<station>[a-z0-9][a-z0-9-]*)_"
    r"(?P<period>00|06|12|18)_(?P<wavelength>\d+nm)_(?P<mode>[A-Za-z0-9-]+)_"
    r"(?P<altitude>\d+(?:\.\d+)?)km\.webp$",
    flags=re.IGNORECASE,
)
_CANONICAL_MEAN_RE = re.compile(
    r"^rcs_(?P<date>\d{8})_(?P<station>[a-z0-9][a-z0-9-]*)_"
    r"(?P<period>00|06|12|18)_mean\.webp$",
    flags=re.IGNORECASE,
)
_LEGACY_DASHBOARD_RE = re.compile(
    r"^(?P<date>\d{8})(?P<period>saam|sapm|sant)_(?:Dashboard|Gallery)\.html$",
    flags=re.IGNORECASE,
)


@dataclass(slots=True)
class Asset:
    """One canonical published image."""

    path: Path
    filename: str
    kind: str
    channel: str | None = None
    altitude: str | None = None


@dataclass(slots=True)
class CanonicalPeriod:
    """Assets for one canonical six-hour period."""

    period: str
    station: str
    assets: list[Asset] = field(default_factory=list)
    channels: dict[str, dict[str, str]] = field(default_factory=dict)
    mean_filename: str | None = None

    def add_asset(self, asset: Asset) -> None:
        self.assets.append(asset)
        if asset.kind == "quicklook" and asset.channel and asset.altitude:
            self.channels.setdefault(asset.channel, {})[asset.altitude] = asset.filename
        elif asset.kind == "mean":
            self.mean_filename = asset.filename


@dataclass(frozen=True, slots=True)
class LegacyPeriod:
    """One existing pre-6-hour dashboard retained during migration."""

    key: str
    label: str
    coverage_slots: tuple[str, ...]
    url: str


@dataclass(slots=True)
class DayData:
    """All canonical and legacy representations associated with one civil day."""

    date: str
    canonical: dict[str, CanonicalPeriod] = field(default_factory=dict)
    legacy: dict[str, LegacyPeriod] = field(default_factory=dict)

    @property
    def year(self) -> str:
        return self.date[:4]

    @property
    def month(self) -> str:
        return self.date[4:6]

    @property
    def day_url(self) -> str:
        """Return the stable daily dashboard URL for canonical or legacy-only days."""
        return f"{self.year}/{self.month}/{self.date}/index.html"

    def fallback_for_slot(self, slot: str) -> LegacyPeriod | None:
        """Return a legacy representation covering a slot not yet canonicalized."""
        if slot in self.canonical:
            return None
        direct_key = {"06": "legacy_am", "12": "legacy_pm"}.get(slot)
        if direct_key is not None:
            return self.legacy.get(direct_key)
        if slot in {"00", "18"}:
            return self.legacy.get("legacy_nt")
        return None

    def slot_source(self, slot: str) -> str | None:
        if slot in self.canonical:
            return "canonical"
        legacy = self.fallback_for_slot(slot)
        return legacy.key if legacy else None

    def coverage(self) -> dict[str, bool]:
        return {slot: self.slot_source(slot) is not None for slot in CANONICAL_PERIODS}

    def fingerprint(self) -> str:
        """Fingerprint canonical local inputs so incremental updates notice changed days."""
        parts: list[str] = []
        for slot, period in sorted(self.canonical.items()):
            for asset in sorted(period.assets, key=lambda item: item.filename):
                try:
                    stat = asset.path.stat()
                    marker = f"{stat.st_size}:{stat.st_mtime_ns}"
                except OSError:
                    marker = "missing"
                parts.append(f"{slot}:{asset.filename}:{marker}")
        return sha256("\n".join(parts).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Catalog:
    days: dict[str, DayData] = field(default_factory=dict)

    def day(self, date: str) -> DayData:
        return self.days.setdefault(date, DayData(date=date))

    @property
    def canonical_days(self) -> list[DayData]:
        return [day for _, day in sorted(self.days.items()) if day.canonical]

    @property
    def site_days(self) -> list[DayData]:
        """Return every civil day represented by canonical or retained legacy products."""
        return [day for _, day in sorted(self.days.items())]


def _valid_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return False
    return True


def collect_canonical_assets(paths: Iterable[str | Path]) -> Catalog:
    """Collect canonical ``rcs_YYYYMMDD_station_HH_*`` webp files."""
    catalog = Catalog()
    for raw_path in paths:
        path = Path(raw_path)
        name = path.name
        quicklook = _CANONICAL_QUICKLOOK_RE.fullmatch(name)
        mean = _CANONICAL_MEAN_RE.fullmatch(name)
        match = quicklook or mean
        if match is None:
            continue

        date = match.group("date")
        if not _valid_date(date):
            continue
        station = match.group("station").lower()
        if station != STATION_ID:
            continue
        period_id = match.group("period")
        day = catalog.day(date)
        period = day.canonical.setdefault(
            period_id,
            CanonicalPeriod(period=period_id, station=station),
        )

        if quicklook is not None:
            channel = f"{quicklook.group('wavelength')}_{quicklook.group('mode').upper()}"
            asset = Asset(
                path=path,
                filename=name,
                kind="quicklook",
                channel=channel,
                altitude=quicklook.group("altitude"),
            )
        else:
            asset = Asset(path=path, filename=name, kind="mean")
        period.add_asset(asset)
    return catalog


def discover_legacy_dashboards(base_site_folder: str | Path) -> Catalog:
    """Index existing legacy dashboards without opening their HTML contents."""
    base = Path(base_site_folder)
    catalog = Catalog()
    if not base.exists():
        return catalog

    for year_dir in sorted(base.iterdir()):
        if not year_dir.is_dir() or not (year_dir.name.isdigit() and len(year_dir.name) == 4):
            continue
        for dashboard in sorted(year_dir.iterdir()):
            match = _LEGACY_DASHBOARD_RE.fullmatch(dashboard.name)
            if match is None:
                continue
            date = match.group("date")
            if not _valid_date(date):
                continue
            legacy_code = match.group("period").lower()
            key, label, slots = LEGACY_PERIODS[legacy_code]
            catalog.day(date).legacy[key] = LegacyPeriod(
                key=key,
                label=label,
                coverage_slots=slots,
                url=f"{year_dir.name}/{dashboard.name}",
            )
    return catalog


def merge_catalogs(*catalogs: Catalog) -> Catalog:
    merged = Catalog()
    for catalog in catalogs:
        for date, source_day in catalog.days.items():
            target = merged.day(date)
            target.canonical.update(source_day.canonical)
            target.legacy.update(source_day.legacy)
    return merged


def sort_altitudes(values: Iterable[str]) -> list[str]:
    def key(value: str) -> tuple[float, str]:
        try:
            return float(value), value
        except ValueError:
            return float("inf"), value

    return sorted(set(values), key=key)
