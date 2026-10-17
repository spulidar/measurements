from pathlib import Path

from publisher.catalog import inventory_to_year_states
from publisher.render import build_manifest, render_day_dashboard, render_legacy_redirect
from publisher.state import cleanup_candidates, get_day, resolved_public_views, validate_states


def _states(keys):
    return inventory_to_year_states(set(keys), "spu")


def test_historical_day_uses_unified_ui_without_public_migration_terms(tmp_path: Path):
    states = _states(
        {
            "2024/Quicklook_20241018saam_532nm_AN_15km.webp",
            "2024/Quicklook_20241018sapm_532nm_AN_15km.webp",
            "2024/Quicklook_20241018sant_532nm_AN_15km.webp",
        }
    )
    day = get_day(states, "20241018")
    views = resolved_public_views(day)

    assert [view["label"] for view in views] == ["00–06 + 18–24", "06–12", "12–18"]

    output = render_day_dashboard("20241018", day, tmp_path, "https://example.invalid")
    page = output.read_text(encoding="utf-8")
    assert "Quicklook_20241018saam_532nm_AN_15km.webp" in page
    assert "00–06 + 18–24" in page
    assert "canonical" not in page.lower()


def test_new_public_window_exposes_explicit_publication_window_id():
    states = _states({"2024/10/20241018/rcs_20241018_spu_06_532nm_AN_15km.webp"})
    day = get_day(states, "20241018")
    view = resolved_public_views(day)[0]
    assert view["selector_id"] == "06"
    assert view["publication_window_id"] == "20241018_spu_06"


def test_night_splits_only_after_both_new_night_windows_exist():
    keys = {
        "2024/Quicklook_20241018sant_532nm_AN_15km.webp",
        "2024/10/20241018/rcs_20241018_spu_00_532nm_AN_15km.webp",
    }
    states = _states(keys)
    day = get_day(states, "20241018")
    assert [view["label"] for view in resolved_public_views(day)] == ["00–06 + 18–24"]

    states = _states(keys | {"2024/10/20241018/rcs_20241018_spu_18_532nm_AN_15km.webp"})
    day = get_day(states, "20241018")
    assert [view["label"] for view in resolved_public_views(day)] == ["00–06", "18–24"]


def test_cleanup_candidates_only_when_equivalent_publication_coverage_exists():
    states = _states(
        {
            "2024/Quicklook_20241018saam_532nm_AN_15km.webp",
            "2024/Quicklook_20241018sant_532nm_AN_15km.webp",
            "2024/10/20241018/rcs_20241018_spu_06_532nm_AN_15km.webp",
            "2024/10/20241018/rcs_20241018_spu_00_532nm_AN_15km.webp",
        }
    )
    products = {item["product"] for item in cleanup_candidates(states, "2024")}
    assert products == {"am"}


def test_manifest_keeps_historical_compatibility_and_new_identity():
    states = _states({"2024/10/20241018/rcs_20241018_spu_06_532nm_AN_15km.webp"})
    manifest = build_manifest(states)
    day = manifest["days"]["20241018"]
    assert day["slots"]["06"] == {
        "available": True,
        "publication_window_id": "20241018_spu_06",
    }
    assert day["slots"]["00"] == {"available": False}
    assert manifest["schema_version"] == 4


def test_redirect_preserves_old_url_as_compatibility_stub(tmp_path: Path):
    source = tmp_path / "2024" / "20241018saam_Dashboard.html"
    source.parent.mkdir()
    source.write_text("old dashboard", encoding="utf-8")
    render_legacy_redirect(source, "10/20241018/index.html")
    page = source.read_text(encoding="utf-8")
    assert "10/20241018/index.html" in page
    assert "old dashboard" not in page


def test_state_validation_accepts_schema_v1_and_counts_publication_windows():
    states = _states(
        {
            "2024/Quicklook_20241018saam_532nm_AN_15km.webp",
            "2024/10/20241018/rcs_20241018_spu_06_532nm_AN_15km.webp",
        }
    )
    counts = validate_states(states)
    assert counts["years"] == 1
    assert counts["days"] == 1
    assert counts["historical_products"] == 1
    assert counts["publication_windows"] == 1
