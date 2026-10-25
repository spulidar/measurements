"""Safe branch export of HTML and manifest from permanent R2-verified state."""
from __future__ import annotations

import json
from hashlib import sha256

import pytest

from publisher.site_v2 import build_unchanged_frontend
import publisher.site_export as exporting
from publisher.published import PUBLICATION_PREFIX, write_published_states


def site_fixture(tmp_path):
    repo = tmp_path / "measurements"
    repo.mkdir()
    (repo / "ql-measurement-calendar.html").write_text(
        "<script>const siteBaseUrl='https://spulidar.github.io/measurements/';</script>"
    )
    date = "20240913"
    pub_id = "20240913_spu_12"
    filename = f"rcs_{pub_id}_532nm_AN_15km.webp"
    digest = sha256(b"test-image").hexdigest()
    state = {
        "schema_version": 2,
        "station_id": "spu",
        "year": 2024,
        "days": {date: {"publication_windows": {"12": {
            "publication_window_id": pub_id,
            "source_fingerprint": "a" * 64,
            "source_sessions": ["spu_20240913-1549Z_20240913-1750Z"],
            "source_level1_files": ["spu_20240913-1549Z_20240913-1750Z_L1.nc"],
            "source_level2_files": [],
            "scientific_sessions_merged": False,
            "scientific_netcdf_split": False,
            "visualization_only_session_concatenation": False,
            "r2_bucket": "test-bucket",
            "assets": {filename: {
                "key": f"{PUBLICATION_PREFIX}/2024/09/20240913/{filename}",
                "sha256": digest,
                "size": 10,
            }},
        }}}},
    }
    write_published_states(repo, "spu", {}, {"2024": state})
    return repo


@pytest.fixture
def example(tmp_path, monkeypatch):
    repo = site_fixture(tmp_path)
    site, days = build_unchanged_frontend(
        repo, tmp_path / ".cache" / "measurements", "spu",
        r2_public_base="https://staging-r2.example.invalid",
    )
    assert days == 1
    monkeypatch.setattr(exporting, "ensure_git_ready", lambda _: None)
    monkeypatch.setattr(exporting, "_git", lambda _repo, *args: "")
    return repo, site


def test_export_populates_branch_without_pushing_or_touching_calendar(example):
    repo, site = example
    previous_calendar = (repo / "ql-measurement-calendar.html").read_bytes()
    total, retired = exporting.export_site_to_branch(repo, site, "spu")
    assert (total, retired) == (1, 0)
    assert (repo / "measurements.json").read_bytes() == (site / "measurements.json").read_bytes()
    html = (repo / "2024/09/20240913/index.html").read_text()
    assert '<meta name="publisher-fingerprint"' in html
    assert "Mean RCS</button>" not in html
    assert "spu/2024/09/20240913/" in html
    assert "staging/publication-v2" not in html
    assert (repo / "ql-measurement-calendar.html").read_bytes() == previous_calendar
    assert exporting.export_site_to_branch(repo, site, "spu") == (1, 0)


def test_export_refuses_empty_published_catalog(tmp_path, monkeypatch):
    repo = tmp_path / "measurements"
    repo.mkdir()
    (repo / "ql-measurement-calendar.html").write_text(
        "<script>const siteBaseUrl='https://spulidar.github.io/measurements/';</script>"
    )
    site, count = build_unchanged_frontend(
        repo, tmp_path / ".cache", "spu",
        r2_public_base="https://staging-r2.example.invalid",
    )
    assert count == 0
    monkeypatch.setattr(exporting, "ensure_git_ready", lambda _: None)
    monkeypatch.setattr(exporting, "_git", lambda _repo, *args: "")
    with pytest.raises(RuntimeError, match="empty public catalog"):
        exporting.export_site_to_branch(repo, site, "spu")
    assert not (repo / "measurements.json").exists()


def test_export_refuses_dirty_repo_before_copying(example, monkeypatch):
    repo, site = example
    monkeypatch.setattr(exporting, "_git", lambda _repo, *args: " M README.md")
    with pytest.raises(RuntimeError, match="uncommitted"):
        exporting.export_site_to_branch(repo, site, "spu")
    assert not (repo / "measurements.json").exists()


def test_export_cleans_only_previous_generated_orphan_pages(example):
    repo, site = example
    assert exporting.export_site_to_branch(repo, site, "spu") == (1, 0)
    old_dir = repo / "2023/01/20230101"
    old_dir.mkdir(parents=True)
    source = repo / "2024/09/20240913/index.html"
    (old_dir / "index.html").write_bytes(source.read_bytes())
    assert exporting.export_site_to_branch(repo, site, "spu") == (1, 1)
    assert not (repo / "2023").exists()
    assert source.is_file()


def test_export_refuses_unrelated_page_and_leaves_branch_untouched(example):
    repo, site = example
    unrelated = repo / "2023/01/20230101/index.html"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("<html>unrelated website</html>")
    with pytest.raises(RuntimeError, match="unrelated daily HTML"):
        exporting.export_site_to_branch(repo, site, "spu")
    assert unrelated.read_text() == "<html>unrelated website</html>"
    assert not (repo / "measurements.json").exists()


def test_export_refuses_modified_generated_manifest(example):
    repo, site = example
    manifest = site / "measurements.json"
    raw = json.loads(manifest.read_text())
    raw["days"].pop("20240913")
    manifest.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="does not match"):
        exporting.export_site_to_branch(repo, site, "spu")
    assert not (repo / "measurements.json").exists()


def test_export_refuses_symlink_year(example, tmp_path):
    repo, site = example
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "2024").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        exporting.export_site_to_branch(repo, site, "spu")
    assert not (repo / "measurements.json").exists()


def test_export_branch_identity_check_is_mandatory(example, monkeypatch):
    repo, site = example

    def reject(_repo):
        raise RuntimeError("Not on publication-v2")
    monkeypatch.setattr(exporting, "ensure_git_ready", reject)
    with pytest.raises(RuntimeError, match="publication-v2"):
        exporting.export_site_to_branch(repo, site, "spu")
    assert not (repo / "measurements.json").exists()
