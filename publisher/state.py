"""Persistent publication state for the measurements site.

The on-disk schema remains version 1 for backward compatibility.  The legacy
JSON key ``canonical`` is retained as a storage field, but its entries now
represent website publication windows, never MILGRAU scientific sessions.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

from .catalog import PublicationWindowAssets, empty_year_state
from .windows import PUBLICATION_WINDOW_LABELS, PUBLICATION_WINDOW_STARTS

_STATE_RE = re.compile(r"^(?P<year>\d{4})\.json$")
_PUBLICATION_WINDOW_ID_RE = re.compile(
    r"^(?P<date>\d{8})_(?P<station>[a-z0-9][a-z0-9-]*)_(?P<window>00|06|12|18)$",
    flags=re.IGNORECASE,
)


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


def publication_window_payload(window: PublicationWindowAssets) -> dict:
    return {
        "publication_window_id": window.publication_window_id,
        "channels": {
            channel: {
                altitude: key
                for altitude, key in sorted(altitudes.items(), key=lambda item: float(item[0]))
            }
            for channel, altitudes in sorted(window.channels.items())
        },
        "mean": window.mean_key,
        "fingerprint": window.fingerprint(),
        "source_sessions": list(window.source_sessions),
        "source_level1_files": list(window.source_level1_files),
        "source_level2_files": list(window.source_level2_files),
    }


def publication_window_changed(states: dict[str, dict], window: PublicationWindowAssets) -> bool:
    day = get_day(states, window.date, create=False)
    if not day:
        return True
    existing = day.get("canonical", {}).get(window.window_start)
    if not existing:
        return True
    return existing.get("fingerprint") != window.fingerprint()


def commit_publication_window(states: dict[str, dict], window: PublicationWindowAssets) -> None:
    day = get_day(states, window.date, create=True)
    assert day is not None
    day.setdefault("canonical", {})[window.window_start] = publication_window_payload(window)


# Old names remain import-compatible for historical scripts, but the values are
# publication-window payloads rather than session/measurement identities.
canonical_period_payload = publication_window_payload
canonical_period_changed = publication_window_changed
commit_canonical_period = commit_publication_window


def _historical_active(day: dict, name: str) -> dict | None:
    entry = day.get("historical", {}).get(name)
    if not entry or entry.get("retired"):
        return None
    if not entry.get("channels") and not entry.get("mean"):
        return None
    return entry


def _public_asset_payload(entry: dict) -> dict:
    return {
        "channels": entry.get("channels", {}),
        "mean": entry.get("mean"),
        "publication_window_id": entry.get("publication_window_id"),
    }


def resolved_public_views(day: dict) -> list[dict]:
    """Resolve public selectors while keeping historical fallback behavior.

    New data always use one of the four publication windows.  The special
    ``night`` selector only exists to keep historical ``sant`` data accessible
    until both independent 00 and 18 publication windows replace it.
    """
    publication = day.get("canonical", {})
    w00 = publication.get("00")
    w06 = publication.get("06")
    w12 = publication.get("12")
    w18 = publication.get("18")

    night = _historical_active(day, "night")
    am = _historical_active(day, "am")
    pm = _historical_active(day, "pm")

    result: list[dict] = []

    if night and not (w00 and w18):
        result.append(
            {
                "selector_id": "night",
                "publication_window_id": None,
                "label": night.get("label", "00–06 + 18–24"),
                "channels": night.get("channels", {}),
                "mean": night.get("mean"),
            }
        )
    elif w00:
        result.append(
            {
                "selector_id": "00",
                "label": PUBLICATION_WINDOW_LABELS["00"],
                **_public_asset_payload(w00),
            }
        )

    if w06:
        result.append(
            {
                "selector_id": "06",
                "label": PUBLICATION_WINDOW_LABELS["06"],
                **_public_asset_payload(w06),
            }
        )
    elif am:
        result.append(
            {
                "selector_id": "06",
                "publication_window_id": None,
                "label": PUBLICATION_WINDOW_LABELS["06"],
                "channels": am.get("channels", {}),
                "mean": am.get("mean"),
            }
        )

    if w12:
        result.append(
            {
                "selector_id": "12",
                "label": PUBLICATION_WINDOW_LABELS["12"],
                **_public_asset_payload(w12),
            }
        )
    elif pm:
        result.append(
            {
                "selector_id": "12",
                "publication_window_id": None,
                "label": PUBLICATION_WINDOW_LABELS["12"],
                "channels": pm.get("channels", {}),
                "mean": pm.get("mean"),
            }
        )

    if not (night and not (w00 and w18)) and w18:
        result.append(
            {
                "selector_id": "18",
                "label": PUBLICATION_WINDOW_LABELS["18"],
                **_public_asset_payload(w18),
            }
        )

    if not night:
        ids = {item["selector_id"] for item in result}
        if w00 and "00" not in ids:
            result.insert(
                0,
                {
                    "selector_id": "00",
                    "label": PUBLICATION_WINDOW_LABELS["00"],
                    **_public_asset_payload(w00),
                },
            )
        if w18 and "18" not in ids:
            result.append(
                {
                    "selector_id": "18",
                    "label": PUBLICATION_WINDOW_LABELS["18"],
                    **_public_asset_payload(w18),
                }
            )

    return result


# Existing callers/tests may still use this old name.
resolved_periods = resolved_public_views


def coverage_for_day(day: dict) -> dict[str, bool]:
    publication = day.get("canonical", {})
    night = _historical_active(day, "night")
    am = _historical_active(day, "am")
    pm = _historical_active(day, "pm")
    return {
        "00": bool(publication.get("00") or night),
        "06": bool(publication.get("06") or am),
        "12": bool(publication.get("12") or pm),
        "18": bool(publication.get("18") or night),
    }


def all_dates(states: dict[str, dict]) -> list[str]:
    dates: list[str] = []
    for _year, state in sorted(states.items()):
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


def publication_keys(entry: dict) -> set[str]:
    return historical_keys(entry)


canonical_keys = publication_keys


def cleanup_candidates(states: dict[str, dict], year: str) -> list[dict]:
    """Historical R2 objects no longer required after equivalent windows exist."""
    state = states.get(str(year))
    if not state:
        return []

    candidates: list[dict] = []
    for date, day in sorted(state.get("days", {}).items()):
        publication = day.get("canonical", {})
        historical = day.get("historical", {})
        rules = {
            "am": bool(publication.get("06")),
            "pm": bool(publication.get("12")),
            "night": bool(publication.get("00") and publication.get("18")),
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
    publication = day.get("canonical", {})
    if product == "am":
        entry = publication.get("06")
        return publication_keys(entry) if entry else set()
    if product == "pm":
        entry = publication.get("12")
        return publication_keys(entry) if entry else set()
    if product == "night":
        keys: set[str] = set()
        for window_start in ("00", "18"):
            entry = publication.get(window_start)
            if entry:
                keys.update(publication_keys(entry))
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
        "canonical_periods": 0,  # retained output key for log/backward compatibility
        "publication_windows": 0,
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

            for window_start, entry in day.get("canonical", {}).items():
                if window_start not in PUBLICATION_WINDOW_STARTS:
                    raise RuntimeError(f"Invalid publication window {window_start} on {date}")
                publication_window_id = entry.get("publication_window_id")
                if publication_window_id:
                    match = _PUBLICATION_WINDOW_ID_RE.fullmatch(str(publication_window_id))
                    if (
                        match is None
                        or match.group("date") != date
                        or match.group("window") != window_start
                    ):
                        raise RuntimeError(
                            f"Invalid publication_window_id {publication_window_id!r} on {date}"
                        )
                counts["canonical_periods"] += 1
                counts["publication_windows"] += 1
                counts["asset_keys"] += len(publication_keys(entry))

    return counts
