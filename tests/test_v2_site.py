"""Site-v2 reuses the historical website layout, without legacy data or cached WEBPs."""
from __future__ import annotations

from hashlib import sha256
import json

import pytest

from publisher.published import STAGING_PREFIX, write_published_states
from publisher.site_v2 import build_unchanged_frontend

PUB = "20240913_spu_12"
SESSION = "spu_20240913-1549Z_20240913-1750Z"
ASSET = f"rcs_{PUB}_532nm_AN_15km.webp"


def site_fixture(tmp_path):
    repo = tmp_path / "measurements"
    repo.mkdir()
    (repo / "ql-measurement-calendar.html").write_text(
        '<!doctype html><style>body{color:blue}</style>'
        "<script>const siteBaseUrl='https://spulidar.github.io/measurements/';</script>"
    )
    hash_value = sha256(b"test-image").hexdigest()
    record = {
        "publication_window_id": PUB,
        "source_fingerprint": "e" * 64,
        "source_sessions": [SESSION],
        "source_level1_files": [f"{SESSION}_L1.nc"],
        "source_level2_files": [],
        "scientific_sessions_merged": False,
        "scientific_netcdf_split": False,
        "visualization_only_session_concatenation": False,
        "r2_bucket": "test-bucket",
        "assets": {
            ASSET: {
                "key": f"{STAGING_PREFIX}/spu/2024/09/20240913/{hash_value}/{ASSET}",
                "sha256": hash_value,
                "size": 10,
            }
        },
    }
    state = {
        "schema_version": 2,
        "station_id": "spu",
        "year": 2024,
        "days": {"20240913": {"publication_windows": {"12": record}}},
    }
    write_published_states(repo, "spu", {}, {"2024": state})
    return repo


def test_new_site_reuses_current_html_and_renders_from_git_without_cache_images(tmp_path):
    repo = site_fixture(tmp_path)
    cache = tmp_path / ".cache" / "measurements"
    # Cache is EMPTY, no local WEBPs or L1 needed.
    target, days = build_unchanged_frontend(
        repo, cache, "spu", r2_public_base="https://staging-r2.example.invalid"
    )
    assert days == 1
    assert target == cache / "_site-v2"
    manifest = json.loads((target / "measurements.json").read_text())
    day = manifest["days"]["20240913"]
    assert day["coverage"] == {"00": False, "06": False, "12": True, "18": False}
    assert day["slots"]["12"]["publication_window_id"] == PUB
    assert day["day_url"] == "2024/09/20240913/index.html"
    html = (target / day["day_url"]).read_text()
    assert "<style>" in html
    assert 'id="tab-quicklooks"' in html
    assert 'id="tab-mean"' not in html
    assert "Mean RCS</button>" not in html
    assert "15km.webp" in html
    assert f"{STAGING_PREFIX}/spu/2024/09/20240913/" in html
    calendar = (target / "ql-measurement-calendar.html").read_text()
    assert "body{color:blue}" in calendar
    assert 'const siteBaseUrl="./";' in calendar
    assert not (cache / "spu").exists()
    # Rebuild without product cache is also supported.
    assert build_unchanged_frontend(
        repo, cache, "spu", r2_public_base="https://staging-r2.example.invalid"
    )[1] == 1


def test_site_builder_refuses_wrong_state_and_non_https_r2_url(tmp_path):
    repo = site_fixture(tmp_path)
    cache = tmp_path / ".cache" / "measurements"
    with pytest.raises(ValueError, match="HTTPS"):
        build_unchanged_frontend(repo, cache, "spu", r2_public_base="http://localhost")
    state = repo / "publisher" / "state-v2" / "spu" / "2024.json"
    payload = json.loads(state.read_text())
    payload["days"]["20240913"]["publication_windows"]["12"]["assets"][ASSET]["key"] = "production/unsafe.webp"
    state.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="outside isolated staging namespace"):
        build_unchanged_frontend(
            repo, cache, "spu", r2_public_base="https://staging-r2.example.invalid"
        )
