"""Preview-v2 tests: only attested state and products may reach the browser."""
from __future__ import annotations

from hashlib import sha256
import json

import pytest

from publisher.preview import build_preview, preview_manifest
from publisher.v2 import collect_validated_staging


SESSION = "spu_20240913-1549Z_20240913-1750Z"
PUB = "20240913_spu_12"
ASSET = f"rcs_{PUB}_532nm_AN_15km.webp"


def make_staged_day(root):
    day = root / "spu" / "2024" / "09" / "20240913"
    day.mkdir(parents=True, exist_ok=True)
    target = day / ASSET
    target.write_bytes(b"synthetic-test-webp")
    record = {
        "publication_window_id": PUB,
        "source_sessions": [SESSION],
        "source_level1_files": [f"{SESSION}_L1.nc"],
        "source_level2_files": [],
        "source_fingerprint": "b" * 64,
        "scientific_sessions_merged": False,
        "scientific_netcdf_split": False,
        "visualization_only_session_concatenation": False,
        "assets": [ASSET],
        "asset_hashes": {ASSET: sha256(target.read_bytes()).hexdigest()},
    }
    sidecar = day / f"{PUB}.json"
    sidecar.write_text(json.dumps(record), encoding="utf-8")
    return target, sidecar


def test_local_preview_reads_verified_state_v2_and_links_existing_asset(tmp_path):
    product, _ = make_staged_day(tmp_path)
    before = product.read_bytes()
    index, count = build_preview(tmp_path, "spu")
    assert count == 1
    assert index == tmp_path / "_preview-v2" / "index.html"
    assert index.is_file()
    assert (index.parent / "app.js").is_file()
    assert (index.parent / "styles.css").is_file()
    manifest = json.loads((index.parent / "manifest.json").read_text())
    assert manifest["schema_version"] == 2
    assert manifest["station_id"] == "spu"
    assert manifest["years"] == ["2024"]
    record = manifest["days"]["20240913"]["publication_windows"]["12"]
    assert record["publication_window_id"] == PUB
    assert record["assets"][ASSET] == "spu/2024/09/20240913/" + ASSET
    assert record["source_sessions"] == [SESSION]
    assert product.read_bytes() == before


def test_preview_does_not_require_or_trust_legacy_local_state(tmp_path):
    make_staged_day(tmp_path)
    # An abandoned _state-v2 from the old CLI must not control new previews.
    stale_dir = tmp_path / "_state-v2" / "spu"
    stale_dir.mkdir(parents=True)
    (stale_dir / "2024.json").write_text('{"old":"stale"}')
    index, count = build_preview(tmp_path, "spu")
    assert count == 1 and index.is_file()
    assert preview_manifest(tmp_path, "spu")["days"]["20240913"]
    # No duplicated state is rewritten by milgrau-publish images.
    assert (stale_dir / "2024.json").read_text() == '{"old":"stale"}'


def test_preview_refuses_modified_sidecar_hash(tmp_path):
    _asset, sidecar = make_staged_day(tmp_path)
    bad = json.loads(sidecar.read_text())
    bad["asset_hashes"][ASSET] = "0" * 64
    sidecar.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="modified"):
        build_preview(tmp_path, "spu")


def test_preview_refuses_unattested_asset_modification(tmp_path):
    asset, _ = make_staged_day(tmp_path)
    asset.write_bytes(b"modified-after-approval")
    with pytest.raises(ValueError, match="modified"):
        build_preview(tmp_path, "spu")


def test_preview_refuses_symlink_output(tmp_path):
    make_staged_day(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "_preview-v2").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        build_preview(tmp_path, "spu")
    assert list(outside.iterdir()) == []


def test_empty_staging_builds_honest_empty_preview(tmp_path):
    index, count = build_preview(tmp_path, "spu")
    assert count == 0
    manifest = json.loads((index.parent / "manifest.json").read_text())
    assert manifest["days"] == {}
    assert manifest["years"] == []
