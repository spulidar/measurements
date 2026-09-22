from pathlib import Path

from publisher.catalog import (
    collect_local_canonical_assets,
    discover_legacy_html_paths,
    inventory_to_year_states,
)


def test_inventory_bootstrap_parses_historical_and_canonical_r2_keys():
    keys = {
        "2024/Quicklook_20241018saam_532nm_AN_15km.webp",
        "2024/GlobalMeanRCS_20241018saam.webp",
        "2024/Quicklook_20241018sant_355nm_PC_30km.webp",
        "2024/10/20241018/rcs_20241018_spu_06_532nm_AN_15km.webp",
        "2024/10/20241018/rcs_20241018_spu_06_mean.webp",
    }
    states = inventory_to_year_states(keys)
    day = states["2024"]["days"]["20241018"]

    assert day["historical"]["am"]["channels"]["532nm_AN"]["15"].endswith("15km.webp")
    assert day["historical"]["am"]["mean"].endswith("20241018saam.webp")
    assert day["historical"]["night"]["coverage"] == ["00", "18"]
    assert day["canonical"]["06"]["channels"]["532nm_AN"]["15"].startswith("2024/10/")
    assert day["canonical"]["06"]["mean"].endswith("_06_mean.webp")


def test_local_canonical_collection_uses_new_r2_hierarchy(tmp_path: Path):
    ql = tmp_path / "rcs_20251107_spu_06_532nm_AN_15km.webp"
    mean = tmp_path / "rcs_20251107_spu_06_mean.webp"
    ql.write_bytes(b"ql")
    mean.write_bytes(b"mean")

    catalog = collect_local_canonical_assets([ql, mean])
    period = catalog.periods[("20251107", "06")]

    assert period.channels["532nm_AN"]["15"] == (
        "2025/11/20251107/rcs_20251107_spu_06_532nm_AN_15km.webp"
    )
    assert period.mean_key == "2025/11/20251107/rcs_20251107_spu_06_mean.webp"


def test_discovers_old_html_paths_without_reading_contents(tmp_path: Path):
    year = tmp_path / "2024"
    year.mkdir()
    (year / "20241018saam_Dashboard.html").write_text("anything", encoding="utf-8")
    (year / "20241018sant_Gallery.html").write_text("anything", encoding="utf-8")
    (year / "other.html").write_text("ignored", encoding="utf-8")

    found = discover_legacy_html_paths(tmp_path)
    assert found["20241018"] == [
        "2024/20241018saam_Dashboard.html",
        "2024/20241018sant_Gallery.html",
    ]
