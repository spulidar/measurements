import json
from pathlib import Path

from publisher.catalog import (
    collect_staged_publication_assets,
    discover_legacy_html_paths,
    inventory_to_year_states,
)


def test_inventory_bootstrap_parses_historical_and_existing_public_r2_keys():
    keys = {
        "2024/Quicklook_20241018saam_532nm_AN_15km.webp",
        "2024/GlobalMeanRCS_20241018saam.webp",
        "2024/Quicklook_20241018sant_355nm_PC_30km.webp",
        "2024/10/20241018/rcs_20241018_spu_06_532nm_AN_15km.webp",
        "2024/10/20241018/rcs_20241018_spu_06_mean.webp",
    }
    states = inventory_to_year_states(keys, "spu")
    day = states["2024"]["days"]["20241018"]

    assert day["historical"]["am"]["channels"]["532nm_AN"]["15"].endswith("15km.webp")
    assert day["historical"]["night"]["coverage"] == ["00", "18"]
    assert day["canonical"]["06"]["publication_window_id"] == "20241018_spu_06"
    assert day["canonical"]["06"]["channels"]["532nm_AN"]["15"].startswith("2024/10/")
    assert day["canonical"]["06"]["mean"].endswith("_06_mean.webp")


def test_staged_collection_only_accepts_dedicated_station_day_tree(tmp_path: Path):
    root = tmp_path / "03-site-products"
    day = root / "spu" / "2025" / "11" / "20251107"
    day.mkdir(parents=True)
    ql = day / "rcs_20251107_spu_06_532nm_AN_15km.webp"
    mean = day / "rcs_20251107_spu_06_mean.webp"
    ql.write_bytes(b"ql")
    mean.write_bytes(b"mean")
    (day / "20251107_spu_06.json").write_text(
        json.dumps(
            {
                "publication_window_id": "20251107_spu_06",
                "source_sessions": ["spu_20251107-0900Z_20251107-1300Z"],
                "source_level1_files": ["spu_20251107-0900Z_20251107-1300Z_L1.nc"],
                "source_level2_files": [],
            }
        ),
        encoding="utf-8",
    )

    # Same valid-looking filename in an arbitrary subtree must not become public.
    arbitrary = root / "spu" / "junk"
    arbitrary.mkdir(parents=True)
    (arbitrary / ql.name).write_bytes(b"wrong")

    catalog = collect_staged_publication_assets(root, "spu")
    window = catalog.windows[("20251107", "06")]

    assert window.publication_window_id == "20251107_spu_06"
    assert window.channels["532nm_AN"]["15"] == (
        "2025/11/20251107/rcs_20251107_spu_06_532nm_AN_15km.webp"
    )
    assert window.mean_key == "2025/11/20251107/rcs_20251107_spu_06_mean.webp"
    assert window.source_sessions == ("spu_20251107-0900Z_20251107-1300Z",)
    assert len(window.assets) == 2


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
