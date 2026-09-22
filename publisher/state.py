"""Persistent publication state for the measurements site."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Iterable

from .catalog import CANONICAL_PERIODS, LocalPeriod, empty_year_state

_STATE_RE = re.compile(r"^(?P<year>\d{4})\.json$")


def state_dir(site_root: str | Path) -> Path:
    return Path(site_root) / "publisher" / "state"


def load_states(site_root: str | Path) -> dict[str, dict]:
    directory = state_dir(site_root)
    states: dict[str, dict] = {}
    if not directory.exists():
        return states

    for path in sorted(directory.glob("*.json")):
        match = _STATE_RE.fullmatch(path.name)
        if not match:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        year = match.group("year")
        if str(payload.get("year")) != year:
            raise RuntimeError(f"State year mismatch in {path}")
        if payload.get("schema_version") != 1:
            raise RuntimeError(f"Unsupported state schema in {path}")
        states[year] = payload
    return states


def save_states(site_root: str | Path, states: dict[str, dict]) -> list[Path]:
    directory = state_dir(site_root)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for year, state in sorted(states.items()):
        path = directory / f"{year}.json"
        path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written


def clone_states(states: dict[str, dict]) -> dict[str, dict]:
    return deepcopy(states)


def get_day(states: dict[str, dict], date: str, create: bool = False) -> dict | None:
    year = date[:4]
    state = states.get(year)
    if state is None:
        if not create:
            return None
        state = states[year] = empty_year_state(year)
    days = state.setdefault("days", {})
    if create:
        return days.setdefault(date, {"historical": {}, "canonical": {}})
    return days.get(date)


def canonical_period_payload(period: LocalPeriod) -> dict:
    return {
        "channels": {
            channel: {
                altitude: key
                for altitude, key in sorted(altitudes.items(), key=lambda item: float(item[0]))
            }
            for channel, altitudes in sorted(period.channels.items())
        },
        "mean": period.mean_key,
        "fingerprint": period.fingerprint(),
    }


def canonical_period_changed(states: dict[str, dict], period: LocalPeriod) -> bool:
    day = get_day(states, period.date, create=False)
    if not day:
        return True
    existing = day.get("canonical", {}).get(period.period)
    if not existing:
        return True
    return existing.get("fingerprint") != period.fingerprint()


def commit_canonical_period(states: dict[str, dict], period: LocalPeriod) -> None:
    day = get_day(states, period.date, create=True)
    assert day is not None
    day.setdefault("canonical", {})[period.period] = canonical_period_payload(period)


def _historical_active(day: dict, name: str) -> dict | None:
    entry = day.get("historical", {}).get(name)
    if not entry or entry.get("retired"):
        return None
    if not entry.get("channels") and not entry.get("mean"):
        return None
    return entry


def resolved_periods(day: dict) -> list[dict]:
    """Resolve the public period selector without exposing migration internals."""
    canonical = day.get("canonical", {})
    c00 = canonical.get("00")
    c06 = canonical.get("06")
    c12 = canonical.get("12")
    c18 = canonical.get("18")

    night = _historical_active(day, "night")
    am = _historical_active(day, "am")
    pm = _historical_active(day, "pm")

    result: list[dict] = []

    # The old night product is one combined product. Until both independent
    # canonical periods exist, retain one honest combined selector.
    if night and not (c00 and c18):
        result.append({
            "id": "night",
            "label": night.get("label", "00–06 + 18–24"),
            "channels": night.get("channels", {}),
            "mean": night.get("mean"),
        })
    elif c00:
        result.append({"id": "00", "label": "00–06", **_public_asset_payload(c00)})

    if c06:
        result.append({"id": "06", "label": "06–12", **_public_asset_payload(c06)})
    elif am:
        result.append({
            "id": "06",
            "label": "06–12",
            "channels": am.get("channels", {}),
            "mean": am.get("mean"),
        })

    if c12:
        result.append({"id": "12", "label": "12–18", **_public_asset_payload(c12)})
    elif pm:
        result.append({
            "id": "12",
            "label": "12–18",
            "channels": pm.get("channels", {}),
            "mean": pm.get("mean"),
        })

    if not (night and not (c00 and c18)) and c18:
        result.append({"id": "18", "label": "18–24", **_public_asset_payload(c18)})

    # If no historical night exists, expose any canonical side that exists.
    if not night:
        ids = {item["id"] for item in result}
        if c00 and "00" not in ids:
            result.insert(0, {"id": "00", "label": "00–06", **_public_asset_payload(c00)})
        if c18 and "18" not in ids:
            result.append({"id": "18", "label": "18–24", **_public_asset_payload(c18)})

    return result


def _public_asset_payload(entry: dict) -> dict:
    return {
        "channels": entry.get("channels", {}),
        "mean": entry.get("mean"),
    }


def coverage_for_day(day: dict) -> dict[str, bool]:
    canonical = day.get("canonical", {})
    historical = day.get("historical", {})
    night = _historical_active(day, "night")
    am = _historical_active(day, "am")
    pm = _historical_active(day, "pm")
    return {
        "00": bool(canonical.get("00") or night),
        "06": bool(canonical.get("06") or am),
        "12": bool(canonical.get("12") or pm),
        "18": bool(canonical.get("18") or night),
    }


def all_dates(states: dict[str, dict]) -> list[str]:
    dates: list[str] = []
    for year, state in sorted(states.items()):
        dates.extend(sorted(state.get("days", {})))
    return dates


def historical_keys(entry: dict) -> set[str]:
    keys: set[str] = set()
    for altitudes in entry.get("channels", {}).values():
        keys.update(str(value) for value in altitudes.values())
    mean = entry.get("mean")
    if mean:
        keys.add(str(mean))
    return keys


def canonical_keys(entry: dict) -> set[str]:
    return historical_keys(entry)


def cleanup_candidates(states: dict[str, dict], year: str) -> list[dict]:
    """Historical objects that are no longer required by the public site."""
    state = states.get(str(year))
    if not state:
        return []

    candidates: list[dict] = []
    for date, day in sorted(state.get("days", {}).items()):
        canonical = day.get("canonical", {})
        historical = day.get("historical", {})
        rules = {
            "am": bool(canonical.get("06")),
            "pm": bool(canonical.get("12")),
            "night": bool(canonical.get("00") and canonical.get("18")),
        }
        for name, replaced in rules.items():
            entry = historical.get(name)
            if not entry or entry.get("retired") or not replaced:
                continue
            keys = sorted(historical_keys(entry))
            if not keys:
                continue
            candidates.append(
                {
                    "date": date,
                    "product": name,
                    "label": entry.get("label", name),
                    "coverage": entry.get("coverage", []),
                    "keys": keys,
                }
            )
    return candidates


def required_canonical_keys_for_cleanup(day: dict, product: str) -> set[str]:
    canonical = day.get("canonical", {})
    if product == "am":
        entry = canonical.get("06")
        return canonical_keys(entry) if entry else set()
    if product == "pm":
        entry = canonical.get("12")
        return canonical_keys(entry) if entry else set()
    if product == "night":
        keys: set[str] = set()
        for period in ("00", "18"):
            entry = canonical.get(period)
            if entry:
                keys.update(canonical_keys(entry))
        return keys
    return set()


def mark_historical_retired(states: dict[str, dict], date: str, product: str) -> None:
    day = get_day(states, date, create=False)
    if not day:
        return
    entry = day.get("historical", {}).get(product)
    if entry:
        entry["retired"] = True


def validate_states(states: dict[str, dict]) -> dict[str, int]:
    counts = {
        "years": 0,
        "days": 0,
        "historical_products": 0,
        "canonical_periods": 0,
        "asset_keys": 0,
    }

    for year, state in sorted(states.items()):
        if not re.fullmatch(r"\d{4}", year):
            raise RuntimeError(f"Invalid state year: {year}")
        if state.get("schema_version") != 1 or str(state.get("year")) != year:
            raise RuntimeError(f"Invalid state header for {year}")
        counts["years"] += 1

        for date, day in sorted(state.get("days", {}).items()):
            if not re.fullmatch(r"\d{8}", date) or date[:4] != year:
                raise RuntimeError(f"Invalid date {date} in state {year}")
            counts["days"] += 1

            for name, entry in day.get("historical", {}).items():
                if name not in {"am", "pm", "night"}:
                    raise RuntimeError(f"Unknown historical product {name} on {date}")
                counts["historical_products"] += 1
                counts["asset_keys"] += len(historical_keys(entry))

            for period, entry in day.get("canonical", {}).items():
                if period not in CANONICAL_PERIODS:
                    raise RuntimeError(f"Invalid canonical period {period} on {date}")
                counts["canonical_periods"] += 1
                counts["asset_keys"] += len(canonical_keys(entry))

    return counts
