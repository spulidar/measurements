"""Real local Git integration: one commit, verified push and failure cleanup.

No GitHub API tokens, network or Cloudflare access are used.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import subprocess

import pytest

from publisher import pipeline
from publisher.published import write_published_states


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", *args], cwd=repo, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return p.stdout.strip()


def state_for(day: str):
    pub_id = f"{day}_spu_12"
    sid = f"spu_{day}-1549Z_{day}-1750Z"
    name = f"rcs_{pub_id}_532nm_AN_15km.webp"
    content = b"verified-image"
    digest = sha256(content).hexdigest()
    record = {
        "publication_window_id": pub_id,
        "source_fingerprint": "1" * 64,
        "source_sessions": [sid],
        "source_level1_files": [f"{sid}_L1.nc"],
        "source_level2_files": [],
        "scientific_sessions_merged": False,
        "scientific_netcdf_split": False,
        "visualization_only_session_concatenation": False,
        "r2_bucket": "test-bucket",
        "assets": {name: {
            "key": f"spu/{day[:4]}/{day[4:6]}/{day}/{name}",
            "sha256": digest,
            "size": len(content),
        }},
    }
    return {
        "schema_version": 2,
        "station_id": "spu",
        "year": int(day[:4]),
        "days": {day: {"publication_windows": {"12": record}}},
    }


@pytest.fixture
def local_git(tmp_path):
    repo = tmp_path / "measurements"
    repo.mkdir()
    git(repo, "init", "-b", "publication-v2")
    git(repo, "config", "user.name", "Publisher Test")
    git(repo, "config", "user.email", "publisher-test@example.invalid")
    (repo / "ql-measurement-calendar.html").write_text(
        "<script>const siteBaseUrl='https://spulidar.github.io/measurements/';</script>"
    )
    git(repo, "add", "--", "ql-measurement-calendar.html")
    git(repo, "commit", "-m", "Baseline")
    remote = tmp_path / "bare-origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True, text=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "HEAD:refs/heads/publication-v2")
    ctx = pipeline.Context(
        repo=repo, workspace=tmp_path, config_path=tmp_path / "config.yaml",
        config={}, station="spu", timezone="America/Sao_Paulo",
        cache=tmp_path / ".cache/measurements",
        scientific=tmp_path / "02-processed_data", visual_root=tmp_path,
    )
    return ctx


def test_real_local_git_commit_includes_state_pages_and_calendar(local_git):
    ctx = local_git
    baseline = git(ctx.repo, "rev-parse", "HEAD")
    write_published_states(ctx.repo, "spu", {}, {"2024": state_for("20240913")})
    assert pipeline.commit_site(ctx, {"R2_PUBLIC_URL": "https://r2.example.invalid"}) == 1
    published = git(ctx.repo, "rev-parse", "HEAD")
    assert published != baseline
    assert git(ctx.repo, "ls-remote", "--heads", "origin", "publication-v2").split()[0] == published
    changed = git(ctx.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()
    assert set(changed) == {
        "publisher/state-v2/spu/2024.json",
        "measurements.json",
        "2024/09/20240913/index.html",
    }
    assert git(ctx.repo, "status", "--porcelain") == ""
    assert pipeline.commit_site(ctx, {"R2_PUBLIC_URL": "https://r2.example.invalid"}) == 1
    assert git(ctx.repo, "rev-parse", "HEAD") == published


def test_failed_git_push_restores_clean_checkout_without_losing_cache(local_git, monkeypatch):
    ctx = local_git
    baseline = git(ctx.repo, "rev-parse", "HEAD")
    write_published_states(ctx.repo, "spu", {}, {"2024": state_for("20240913")})
    original = pipeline.git_push

    def fail_push(_repo, _settings):
        raise RuntimeError("Simulated network failure before Git push")

    monkeypatch.setattr(pipeline, "git_push", fail_push)
    with pytest.raises(RuntimeError, match="Simulated network failure"):
        pipeline.commit_site(ctx, {"R2_PUBLIC_URL": "https://r2.example.invalid"})
    monkeypatch.setattr(pipeline, "git_push", original)
    assert git(ctx.repo, "rev-parse", "HEAD") == baseline
    assert git(ctx.repo, "ls-remote", "--heads", "origin", "publication-v2").split()[0] == baseline
    assert git(ctx.repo, "status", "--porcelain") == ""
    assert not (ctx.repo / "publisher/state-v2/spu/2024.json").exists()
    assert not (ctx.repo / "measurements.json").exists()
    assert not (ctx.repo / "2024/09/20240913/index.html").exists()
