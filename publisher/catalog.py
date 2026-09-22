"""Discovery and normalization for SPU lidar publication assets.

This module understands two storage layouts:

Historical R2 objects
---------------------
YYYY/Quicklook_YYYYMMDD(saam|sapm|sant)_CHANNEL_ALTkm.webp
YYYY/GlobalMeanRCS_YYYYMMDD(saam|sapm|sant).webp

Canonical R2/local objects
--------------------------
YYYY/MM/YYYYMMDD/rcs_YYYYMMDD_spu_HH_CHANNEL_ALTkm.webp
YYYY/MM/YYYYMMDD/rcs_YYYYMMDD_spu_HH_mean.webp
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
HISTORICAL_PRODUCTS = {
    "saam": ("am", "06–12", ("06",)),
    "sapm": ("pm", "12–18", ("12",)),
    "sant": ("night", "00–06 + 18–24", ("00", "18")),
}

_CANONICAL_QUICKLOOK_NAME_RE = re.compile(
    r"^rcs_(?P<date>\d{8})_(?P<station>[a-z0-9][a-z0-9-]*)_"
    r"(?P<period>00|06|12|18)_(?P<channel>\d+nm_[A-Za-z0-9-]+)_"
    r"(?P<altitude>\d+(?:\.\d+)?)km\.webp$",
    flags=re.IGNORECASE,
)
_CANONICAL_MEAN_NAME_RE = re.compile(
    r"^rcs_(?P<date>\d{8})_(?P<station>[a-z0-9][a-z0-9-]*)_"
    r"(?P<period>00|06|12|18)_mean\.webp$",
    flags=re.IGNORECASE,
)
_HISTORICAL_QUICKLOOK_KEY_RE = re.compile(
    r"^(?P<year>\d{4})/Quicklook_(?P<date>\d{8})(?P<code>saam|sapm|sant)_"
    r"(?P<channel>\d+nm_[A-Za-z0-9-]+)_(?P<altitude>\d+(?:\.\d+)?)km\.webp$",
    flags=re.IGNORECASE,
)
_HISTORICAL_MEAN_KEY_RE = re.compile(
    r"^(?P<year>\d{4})/GlobalMeanRCS_(?P<date>\d{8})(?P<code>saam|sapm|sant)\.webp$",
    flags=re.IGNORECASE,
)
_CANONICAL_KEY_RE = re.compile(
    r"^(?P<year>\d{4})/(?P<month>\d{2})/(?P<date>\d{8})/(?P<name>rcs_.+\.webp)$",
    flags=re.IGNORECASE,
)
_LEGACY_HTML_RE = re.compile(
    r"^(?P<date>\d{8})(?P<code>saam|sapm|sant)_(?P<kind>Dashboard|Gallery)\.html$",
    flags=re.IGNORECASE,
)


@dataclass(slots=True)
class LocalAsset:
    path: Path
    key: str
    channel: str | None = None
    altitude: str | None = None
    is_mean: bool = False


@dataclass(slots=True)
class LocalPeriod:
    date: str
    period: str
    assets: list[LocalAsset] = field(default_factory=list)
    channels: dict[str, dict[str, str]] = field(default_factory=dict)
    mean_key: str | None = None

    def add(self, asset: LocalAsset) -> None:
        self.assets.append(asset)
        if asset.is_mean:
            self.mean_key = asset.key
        elif asset.channel and asset.altitude:
            self.channels.setdefault(asset.channel, {})[asset.altitude] = asset.key

    def fingerprint(self) -> str:
        parts: list[str] = []
        for asset in sorted(self.assets, key=lambda item: item.key):
            stat = asset.path.stat()
            parts.append(f"{asset.key}:{stat.st_size}:{stat.st_mtime_ns}")
        return sha256("\n".join(parts).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class LocalCatalog:
    periods: dict[tuple[str, str], LocalPeriod] = field(default_factory=dict)

    def get_or_create(self, date: str, period: str) -> LocalPeriod:
        return self.periods.setdefault((date, period), LocalPeriod(date=date, period=period))


def canonical_key(date: str, filename: str) -> str:
    return f"{date[:4]}/{date[4:6]}/{date}/{filename}"


def collect_local_canonical_assets(paths: Iterable[str | Path]) -> LocalCatalog:
    catalog = LocalCatalog()
    for raw_path in paths:
        path = Path(raw_path)
        name = path.name
        quicklook = _CANONICAL_QUICKLOOK_NAME_RE.fullmatch(name)
        mean = _CANONICAL_MEAN_NAME_RE.fullmatch(name)
        match = quicklook or mean
        if match is None:
            continue

        station = match.group("station").lower()
        if station != STATION_ID:
            continue

        date = match.group("date")
        period_id = match.group("period")
        period = catalog.get_or_create(date, period_id)
        key = canonical_key(date, name)

        if quicklook is not None:
            channel = quicklook.group("channel")
            # Normalize mode suffix to upper-case while preserving wavelength text.
            if "_" in channel:
                wavelength, mode = channel.split("_", 1)
                channel = f"{wavelength}_{mode.upper()}"
            period.add(
                LocalAsset(
                    path=path,
                    key=key,
                    channel=channel,
                    altitude=quicklook.group("altitude"),
                )
            )
        else:
            period.add(LocalAsset(path=path, key=key, is_mean=True))
    return catalog


def empty_year_state(year: str) -> dict:
    return {
        "schema_version": 1,
        "year": year,
        "days": {},
    }


def _day(state: dict, date: str) -> dict:
    return state.setdefault("days", {}).setdefault(
        date,
        {"historical": {}, "canonical": {}},
    )


def _historical_entry(day: dict, code: str) -> dict:
    internal, label, coverage = HISTORICAL_PRODUCTS[code]
    return day.setdefault("historical", {}).setdefault(
        internal,
        {
            "label": label,
            "coverage": list(coverage),
            "channels": {},
            "mean": None,
            "retired": False,
        },
    )


def _canonical_entry(day: dict, period: str) -> dict:
    return day.setdefault("canonical", {}).setdefault(
        period,
        {
            "channels": {},
            "mean": None,
            "fingerprint": "r2-bootstrap",
        },
    )


def inventory_to_year_states(keys: Iterable[str]) -> dict[str, dict]:
    """Build persistent publication state from the actual R2 object inventory."""
    states: dict[str, dict] = {}

    def state_for(year: str) -> dict:
        return states.setdefault(year, empty_year_state(year))

    for raw_key in sorted(set(keys)):
        key = raw_key.lstrip("/")

        historical_ql = _HISTORICAL_QUICKLOOK_KEY_RE.fullmatch(key)
        if historical_ql:
            year = historical_ql.group("year")
            date = historical_ql.group("date")
            if date[:4] != year:
                continue
            day = _day(state_for(year), date)
            entry = _historical_entry(day, historical_ql.group("code").lower())
            channel = historical_ql.group("channel")
            if "_" in channel:
                wavelength, mode = channel.split("_", 1)
                channel = f"{wavelength}_{mode.upper()}"
            entry.setdefault("channels", {}).setdefault(channel, {})[
                historical_ql.group("altitude")
            ] = key
            continue

        historical_mean = _HISTORICAL_MEAN_KEY_RE.fullmatch(key)
        if historical_mean:
            year = historical_mean.group("year")
            date = historical_mean.group("date")
            if date[:4] != year:
                continue
            day = _day(state_for(year), date)
            entry = _historical_entry(day, historical_mean.group("code").lower())
            entry["mean"] = key
            continue

        canonical_match = _CANONICAL_KEY_RE.fullmatch(key)
        if canonical_match:
            year = canonical_match.group("year")
            date = canonical_match.group("date")
            month = canonical_match.group("month")
            if date[:4] != year or date[4:6] != month:
                continue
            name = canonical_match.group("name")
            quicklook = _CANONICAL_QUICKLOOK_NAME_RE.fullmatch(name)
            mean = _CANONICAL_MEAN_NAME_RE.fullmatch(name)
            match = quicklook or mean
            if match is None or match.group("date") != date:
                continue
            if match.group("station").lower() != STATION_ID:
                continue

            period_id = match.group("period")
            day = _day(state_for(year), date)
            entry = _canonical_entry(day, period_id)
            if quicklook:
                channel = quicklook.group("channel")
                if "_" in channel:
                    wavelength, mode = channel.split("_", 1)
                    channel = f"{wavelength}_{mode.upper()}"
                entry.setdefault("channels", {}).setdefault(channel, {})[
                    quicklook.group("altitude")
                ] = key
            else:
                entry["mean"] = key

    return states


def discover_legacy_html_paths(site_root: str | Path) -> dict[str, list[str]]:
    """Return date -> old Dashboard/Gallery paths without reading their contents."""
    root = Path(site_root)
    result: dict[str, list[str]] = {}
    if not root.exists():
        return result

    for year_dir in sorted(root.iterdir()):
        if not year_dir.is_dir() or not re.fullmatch(r"\d{4}", year_dir.name):
            continue
        for path in sorted(year_dir.iterdir()):
            if not path.is_file():
                continue
            match = _LEGACY_HTML_RE.fullmatch(path.name)
            if match is None:
                continue
            date = match.group("date")
            if date[:4] != year_dir.name:
                continue
            result.setdefault(date, []).append(f"{year_dir.name}/{path.name}")
    return result
