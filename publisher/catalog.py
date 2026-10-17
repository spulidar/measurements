"""Publication-asset discovery and historical R2 normalization.

Scientific NetCDFs and MILGRAU's own figures are *inputs* to the website
publisher; they are never upload candidates.  New website assets are accepted
only from the dedicated ``03-site-products`` staging tree.

Historical R2 objects
---------------------
YYYY/Quicklook_YYYYMMDD(saam|sapm|sant)_CHANNEL_ALTkm.webp
YYYY/GlobalMeanRCS_YYYYMMDD(saam|sapm|sant).webp

Current public/R2 objects (kept for compatibility)
--------------------------------------------------
YYYY/MM/YYYYMMDD/rcs_YYYYMMDD_spu_HH_CHANNEL_ALTkm.webp
YYYY/MM/YYYYMMDD/rcs_YYYYMMDD_spu_HH_mean.webp

Local staging adds a station directory but intentionally does not change the
public R2 key:
03-site-products/spu/YYYY/MM/YYYYMMDD/rcs_YYYYMMDD_spu_HH_....webp
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Iterable

from .windows import PUBLICATION_WINDOW_STARTS

HISTORICAL_PRODUCTS = {
    "saam": ("am", "06–12", ("06",)),
    "sapm": ("pm", "12–18", ("12",)),
    "sant": ("night", "00–06 + 18–24", ("00", "18")),
}

_PUBLIC_QUICKLOOK_NAME_RE = re.compile(
    r"^rcs_(?P<date>\d{8})_(?P<station>[a-z0-9][a-z0-9-]*)_"
    r"(?P<window>00|06|12|18)_(?P<channel>\d+nm_[A-Za-z0-9-]+)_"
    r"(?P<altitude>\d+(?:\.\d+)?)km\.webp$",
    flags=re.IGNORECASE,
)
_PUBLIC_MEAN_NAME_RE = re.compile(
    r"^rcs_(?P<date>\d{8})_(?P<station>[a-z0-9][a-z0-9-]*)_"
    r"(?P<window>00|06|12|18)_mean\.webp$",
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
_PUBLIC_KEY_RE = re.compile(
    r"^(?P<year>\d{4})/(?P<month>\d{2})/(?P<date>\d{8})/(?P<name>rcs_.+\.webp)$",
    flags=re.IGNORECASE,
)
_LEGACY_HTML_RE = re.compile(
    r"^(?P<date>\d{8})(?P<code>saam|sapm|sant)_(?P<kind>Dashboard|Gallery)\.html$",
    flags=re.IGNORECASE,
)


def _normalize_channel(channel: str) -> str:
    if "_" not in channel:
        return channel
    wavelength, mode = channel.split("_", 1)
    return f"{wavelength}_{mode.upper()}"


@dataclass(slots=True)
class StagedAsset:
    path: Path
    key: str
    channel: str | None = None
    altitude: str | None = None
    is_mean: bool = False


@dataclass(slots=True)
class PublicationWindowAssets:
    date: str
    window_start: str
    station_id: str
    publication_window_id: str
    assets: list[StagedAsset] = field(default_factory=list)
    channels: dict[str, dict[str, str]] = field(default_factory=dict)
    mean_key: str | None = None
    source_sessions: tuple[str, ...] = ()
    source_level1_files: tuple[str, ...] = ()
    source_level2_files: tuple[str, ...] = ()

    def add(self, asset: StagedAsset) -> None:
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
        parts.extend(f"session:{value}" for value in self.source_sessions)
        return sha256("\n".join(parts).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class StagedCatalog:
    windows: dict[tuple[str, str], PublicationWindowAssets] = field(default_factory=dict)

    @property
    def periods(self) -> dict[tuple[str, str], PublicationWindowAssets]:
        """Compatibility view for older callers/tests."""
        return self.windows

    def get_or_create(
        self,
        *,
        date: str,
        window_start: str,
        station_id: str,
    ) -> PublicationWindowAssets:
        key = (date, window_start)
        publication_window_id = f"{date}_{station_id}_{window_start}"
        return self.windows.setdefault(
            key,
            PublicationWindowAssets(
                date=date,
                window_start=window_start,
                station_id=station_id,
                publication_window_id=publication_window_id,
            ),
        )


def public_r2_key(date: str, filename: str) -> str:
    """Keep the established public/R2 hierarchy to avoid historical migration."""
    return f"{date[:4]}/{date[4:6]}/{date}/{filename}"


def _load_sidecar(day_dir: Path, publication_window_id: str) -> dict:
    path = day_dir / f"{publication_window_id}.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def collect_staged_publication_assets(
    site_products_root: str | Path,
    station_id: str,
) -> StagedCatalog:
    """Collect only validated WEBPs from the dedicated publication staging tree."""
    root = Path(site_products_root)
    station = str(station_id).strip().lower()
    station_root = root / station
    catalog = StagedCatalog()
    if not station_root.exists():
        return catalog

    for path in sorted(station_root.rglob("*.webp")):
        quicklook = _PUBLIC_QUICKLOOK_NAME_RE.fullmatch(path.name)
        mean = _PUBLIC_MEAN_NAME_RE.fullmatch(path.name)
        match = quicklook or mean
        if match is None:
            continue

        parsed_station = match.group("station").lower()
        if parsed_station != station:
            continue
        date = match.group("date")
        window_start = match.group("window")
        if window_start not in PUBLICATION_WINDOW_STARTS:
            continue

        # The staging structure is part of the publication contract.  A WEBP
        # elsewhere under 03-site-products is not silently treated as public.
        expected_day_dir = station_root / date[:4] / date[4:6] / date
        try:
            if path.parent.resolve() != expected_day_dir.resolve():
                continue
        except OSError:
            continue

        window = catalog.get_or_create(
            date=date,
            window_start=window_start,
            station_id=station,
        )
        asset = StagedAsset(path=path, key=public_r2_key(date, path.name))
        if quicklook is not None:
            asset.channel = _normalize_channel(quicklook.group("channel"))
            asset.altitude = quicklook.group("altitude")
        else:
            asset.is_mean = True
        window.add(asset)

    # Publication provenance lives in a sidecar and is never itself uploaded.
    for window in catalog.windows.values():
        day_dir = station_root / window.date[:4] / window.date[4:6] / window.date
        sidecar = _load_sidecar(day_dir, window.publication_window_id)
        if sidecar.get("publication_window_id") != window.publication_window_id:
            continue
        window.source_sessions = tuple(str(v) for v in sidecar.get("source_sessions", []))
        window.source_level1_files = tuple(str(v) for v in sidecar.get("source_level1_files", []))
        window.source_level2_files = tuple(str(v) for v in sidecar.get("source_level2_files", []))

    return catalog


# Backward-compatible name for code importing the old helper.  The semantics are
# intentionally stricter: the argument is now the staging root, not arbitrary
# paths inside 02-processed_data.
def collect_local_canonical_assets(
    site_products_root: str | Path,
    station_id: str = "spu",
) -> StagedCatalog:
    return collect_staged_publication_assets(site_products_root, station_id)


def empty_year_state(year: str) -> dict:
    return {"schema_version": 1, "year": year, "days": {}}


def _day(state: dict, date: str) -> dict:
    return state.setdefault("days", {}).setdefault(date, {"historical": {}, "canonical": {}})


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


def _public_entry(day: dict, date: str, station: str, window_start: str) -> dict:
    return day.setdefault("canonical", {}).setdefault(
        window_start,
        {
            "publication_window_id": f"{date}_{station}_{window_start}",
            "channels": {},
            "mean": None,
            "fingerprint": "r2-bootstrap",
        },
    )


def inventory_to_year_states(keys: Iterable[str], station_id: str = "spu") -> dict[str, dict]:
    """Build schema-v1 state from the actual R2 inventory.

    Schema v1 is retained deliberately so all previously committed state files
    remain readable.  New entries simply add ``publication_window_id`` metadata.
    """
    station_expected = str(station_id).strip().lower()
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
            entry.setdefault("channels", {}).setdefault(
                _normalize_channel(historical_ql.group("channel")), {}
            )[historical_ql.group("altitude")] = key
            continue

        historical_mean = _HISTORICAL_MEAN_KEY_RE.fullmatch(key)
        if historical_mean:
            year = historical_mean.group("year")
            date = historical_mean.group("date")
            if date[:4] != year:
                continue
            day = _day(state_for(year), date)
            _historical_entry(day, historical_mean.group("code").lower())["mean"] = key
            continue

        public_match = _PUBLIC_KEY_RE.fullmatch(key)
        if not public_match:
            continue

        year = public_match.group("year")
        date = public_match.group("date")
        month = public_match.group("month")
        if date[:4] != year or date[4:6] != month:
            continue
        name = public_match.group("name")
        quicklook = _PUBLIC_QUICKLOOK_NAME_RE.fullmatch(name)
        mean = _PUBLIC_MEAN_NAME_RE.fullmatch(name)
        match = quicklook or mean
        if match is None or match.group("date") != date:
            continue
        station = match.group("station").lower()
        if station != station_expected:
            continue

        window_start = match.group("window")
        day = _day(state_for(year), date)
        entry = _public_entry(day, date, station, window_start)
        if quicklook:
            entry.setdefault("channels", {}).setdefault(
                _normalize_channel(quicklook.group("channel")), {}
            )[quicklook.group("altitude")] = key
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


# Historical import compatibility.
CANONICAL_PERIODS = PUBLICATION_WINDOW_STARTS
LocalPeriod = PublicationWindowAssets
LocalAsset = StagedAsset
LocalCatalog = StagedCatalog
canonical_key = public_r2_key
