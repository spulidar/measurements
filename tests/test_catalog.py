from pathlib import Path

from publisher.catalog import collect_canonical_assets, discover_legacy_dashboards, merge_catalogs
from publisher.publish import cloud_key
from publisher.render import build_manifest, render_day_dashboard


def test_collects_canonical_assets_by_day_period_channel_and_altitude(tmp_path: Path):
    ql = tmp_path / "rcs_20251107_spu_06_532nm_AN_15km.webp"
    mean = tmp_path / "rcs_20251107_spu_06_mean.webp"
    ql.write_bytes(b"x")
    mean.write_bytes(b"y")

    catalog = collect_canonical_assets([ql, mean])
    period = catalog.days["20251107"].canonical["06"]

    assert period.channels["532nm_AN"]["15"] == ql.name
    assert period.mean_filename == mean.name


def test_legacy_dashboard_maps_to_fixed_slots_without_claiming_nt_is_continuous(tmp_path: Path):
    year = tmp_path / "2024"
    year.mkdir()
    for code in ("saam", "sapm", "sant"):
        (year / f"20241018{code}_Dashboard.html").write_text("legacy", encoding="utf-8")

    catalog = discover_legacy_dashboards(tmp_path)
    day = catalog.days["20241018"]

    assert day.coverage() == {"00": True, "06": True, "12": True, "18": True}
    assert day.fallback_for_slot("00").key == "legacy_nt"
    assert day.fallback_for_slot("18").key == "legacy_nt"
    assert day.legacy["legacy_nt"].label == "00–06 + 18–24"


def test_canonical_slot_supersedes_legacy_fallback_during_reprocessing(tmp_path: Path):
    year = tmp_path / "2024"
    year.mkdir()
    (year / "20241018saam_Dashboard.html").write_text("legacy", encoding="utf-8")
    legacy = discover_legacy_dashboards(tmp_path)

    ql = tmp_path / "rcs_20241018_spu_06_532nm_AN_15km.webp"
    ql.write_bytes(b"new")
    canonical = collect_canonical_assets([ql])
    day = merge_catalogs(legacy, canonical).days["20241018"]

    assert day.slot_source("06") == "canonical"
    assert day.fallback_for_slot("06") is None


def test_manifest_and_day_dashboard_use_hierarchical_daily_path(tmp_path: Path):
    site = tmp_path / "site"
    legacy_year = site / "2025"
    legacy_year.mkdir(parents=True)
    (legacy_year / "20251107sant_Dashboard.html").write_text("legacy", encoding="utf-8")
    legacy = discover_legacy_dashboards(site)

    ql = tmp_path / "rcs_20251107_spu_06_532nm_AN_15km.webp"
    ql.write_bytes(b"x")
    catalog = merge_catalogs(legacy, collect_canonical_assets([ql]))
    day = catalog.days["20251107"]

    manifest = build_manifest(catalog)
    assert manifest["days"]["20251107"]["day_url"] == "2025/11/20251107/index.html"
    assert manifest["days"]["20251107"]["slots"]["00"]["source"] == "legacy_nt"
    assert manifest["days"]["20251107"]["slots"]["06"]["source"] == "canonical"

    output = render_day_dashboard(day, site, "https://example.invalid", dry_run=False)
    assert output == site / "2025" / "11" / "20251107" / "index.html"
    page = output.read_text(encoding="utf-8")
    assert "00–06" in page and "18–24" in page
    assert "Legacy period" in page


def test_cloud_key_uses_year_month_day_without_station_directory():
    assert cloud_key("20251107", "rcs_20251107_spu_06_mean.webp") == "2025/11/20251107/rcs_20251107_spu_06_mean.webp"
