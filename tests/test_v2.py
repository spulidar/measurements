"""Isolated publication-v2 catalog/state safety tests."""
import hashlib
import json

import pytest

from publisher.v2 import build_state_v2, collect_validated_staging


SESSION = "spu_20250511-0012Z_20250511-0737Z"
PUB = "20250510_spu_18"
ASSET = f"rcs_{PUB}_532nm_AN_15km.webp"
FP = "a" * 64


def _stage(root, *, asset_name=ASSET):
    day = root / "spu" / "2025" / "05" / "20250510"
    day.mkdir(parents=True)
    webp = day / asset_name
    webp.write_bytes(b"fake-webp-fixture")
    payload = {
        "publication_window_id": PUB,
        "source_sessions": [SESSION],
        "source_level1_files": [f"{SESSION}_L1.nc"],
        "source_level2_files": [],
        "source_fingerprint": FP,
        "scientific_sessions_merged": False,
        "scientific_netcdf_split": False,
        "visualization_only_session_concatenation": False,
        "assets": [asset_name],
        "asset_hashes": {asset_name: hashlib.sha256(webp.read_bytes()).hexdigest()},
    }
    sidecar = day / f"{PUB}.json"
    sidecar.write_text(json.dumps(payload))
    return webp, sidecar, payload


def test_orphan_webp_never_enters_catalog(tmp_path):
    day = tmp_path / "spu" / "2025" / "05" / "20250510"
    day.mkdir(parents=True)
    (day / ASSET).write_bytes(b"unattested")
    assert collect_validated_staging(tmp_path, "spu") == {}


def test_attested_asset_enters_new_station_prefixed_state(tmp_path):
    _stage(tmp_path)
    catalog = collect_validated_staging(tmp_path, "spu")
    state = build_state_v2(catalog, "spu")["2025"]
    record = state["days"]["20250510"]["publication_windows"]["18"]
    assert record["publication_window_id"] == PUB
    assert record["source_sessions"] == [SESSION]
    assert record["assets"][ASSET] == f"spu/2025/05/20250510/{ASSET}"
    # Preview state is derived from the attested cache, not duplicated on disk.
    assert not (tmp_path / "_state-v2").exists()


def test_missing_sidecar_asset_fails(tmp_path):
    file, _sidecar, _payload = _stage(tmp_path)
    file.unlink()
    with pytest.raises(ValueError, match="missing/outside"):
        collect_validated_staging(tmp_path, "spu")


def test_modified_staged_asset_fails(tmp_path):
    file, _sidecar, _payload = _stage(tmp_path)
    file.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="modified"):
        collect_validated_staging(tmp_path, "spu")


def test_unlisted_extra_webp_not_collected(tmp_path):
    _file, _sidecar, _payload = _stage(tmp_path)
    (_file.parent / f"rcs_{PUB}_355nm_AN_15km.webp").write_bytes(b"orphan")
    catalog = collect_validated_staging(tmp_path, "spu")
    state = build_state_v2(catalog, "spu")["2025"]
    assert list(state["days"]["20250510"]["publication_windows"]["18"]["assets"]) == [ASSET]


@pytest.mark.parametrize("field,value,pattern", [
    ("scientific_sessions_merged", True, "scientific_sessions_merged"),
    ("scientific_netcdf_split", True, "scientific_netcdf_split"),
    ("source_fingerprint", "bad", "fingerprint"),
    ("visualization_only_session_concatenation", True, "concatenation"),
    ("source_level1_files", ["wrong.nc"], "provenance"),
])
def test_incorrect_scientific_provenance_rejected(tmp_path, field, value, pattern):
    _file, sidecar, payload = _stage(tmp_path)
    payload[field] = value
    sidecar.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=pattern):
        collect_validated_staging(tmp_path, "spu")


def test_wrong_filename_is_rejected(tmp_path):
    _file, sidecar, payload = _stage(tmp_path)
    payload["assets"] = [f"rcs_{PUB}_arbitrary.webp"]
    payload["asset_hashes"] = {payload["assets"][0]: "a" * 64}
    sidecar.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Noncanonical"):
        collect_validated_staging(tmp_path, "spu")
