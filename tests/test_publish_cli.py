"""Offline acceptance tests for the optional milgrau-publish interface."""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
import json

import pytest

from publisher import cli, history, pipeline
from publisher.pipeline import secrets as original_secrets
from publisher.published import load_published_states, write_published_states
from publisher.site_v2 import build_unchanged_frontend

PUB = "20240913_spu_12"
SESSION = "spu_20240913-1549Z_20240913-1750Z"
ASSET = f"rcs_{PUB}_532nm_AN_15km.webp"
BYTES = b"source-webp"


class Stream:
    def __init__(self, content):
        self.source = BytesIO(content)

    def iter_chunks(self, chunk_size=1048576):
        while piece := self.source.read(chunk_size):
            yield piece

    def close(self):
        self.source.close()


class NotFound(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class R2:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def get_object(self, *, Bucket, Key):
        try:
            data = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise NotFound() from exc
        return {"Body": Stream(data), "ContentLength": len(data)}

    def upload_file(self, filename, bucket, key, ExtraArgs):
        assert key.startswith("spu/")
        assert ExtraArgs["ContentType"] == "image/webp"
        self.objects[(bucket, key)] = Path(filename).read_bytes()

    def copy_object(self, *, Bucket, CopySource, Key, MetadataDirective):
        assert MetadataDirective == "COPY"
        self.objects[(Bucket, Key)] = self.objects[(CopySource["Bucket"], CopySource["Key"])]

    def delete_object(self, *, Bucket, Key):
        self.deleted.append((Bucket, Key))
        self.objects.pop((Bucket, Key), None)


@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = tmp_path / "measurements"
    repo.mkdir()
    (repo / "ql-measurement-calendar.html").write_text(
        '<script>const siteBaseUrl=\'https://spulidar.github.io/measurements/\';</script>'
    )
    workspace = tmp_path
    cache = workspace / ".cache" / "measurements"
    ctx = pipeline.Context(repo, workspace, workspace / "config.yaml", {}, "spu",
                           "America/Sao_Paulo", cache, workspace / "02-processed_data", workspace)
    monkeypatch.setattr(pipeline, "context", lambda: ctx)
    monkeypatch.setattr(pipeline, "checkout_ready", lambda _: None)
    monkeypatch.setattr(pipeline, "secrets", lambda *_args, **_kw: {
        "R2_BUCKET_NAME": "example-bucket",
        "R2_PUBLIC_URL": "https://r2.example.invalid",
    })
    return ctx


def staged_day(ctx, *, date="20240913", blob=BYTES):
    pub_id = f"{date}_spu_12"
    filename = f"rcs_{pub_id}_532nm_AN_15km.webp"
    session = f"spu_{date}-1549Z_{date}-1750Z"
    directory = ctx.cache / "spu" / date[:4] / date[4:6] / date
    directory.mkdir(parents=True, exist_ok=True)
    asset = directory / filename
    asset.write_bytes(blob)
    payload = {
        "publication_window_id": pub_id,
        "source_sessions": [session],
        "source_level1_files": [f"{session}_L1.nc"],
        "source_level2_files": [],
        "source_fingerprint": "a" * 64,
        "scientific_sessions_merged": False,
        "scientific_netcdf_split": False,
        "visualization_only_session_concatenation": False,
        "assets": [filename],
        "asset_hashes": {filename: sha256(blob).hexdigest()},
    }
    (directory / f"{pub_id}.json").write_text(json.dumps(payload))
    return directory, asset


def state_day(ctx, blob=BYTES):
    record = {
        "publication_window_id": PUB,
        "source_fingerprint": "a" * 64,
        "source_sessions": [SESSION],
        "source_level1_files": [f"{SESSION}_L1.nc"],
        "source_level2_files": [],
        "scientific_sessions_merged": False,
        "scientific_netcdf_split": False,
        "visualization_only_session_concatenation": False,
        "r2_bucket": "example-bucket",
        "assets": {ASSET: {
            "key": f"spu/2024/09/20240913/{ASSET}",
            "sha256": sha256(blob).hexdigest(),
            "size": len(blob),
        }},
    }
    state = {"schema_version": 2, "station_id": "spu", "year": 2024,
             "days": {"20240913": {"publication_windows": {"12": record}}}}
    write_published_states(ctx.repo, "spu", {}, {"2024": state})
    return record


def test_cli_is_small_and_dispatches_correctly(monkeypatch):
    requested = []
    monkeypatch.setattr(pipeline, "sync", lambda **kw: requested.append(("sync", kw)) or 0)
    monkeypatch.setattr(pipeline, "status", lambda: requested.append(("status",)) or 0)
    monkeypatch.setattr(pipeline, "images", lambda value: requested.append(("images", value)) or 0)
    monkeypatch.setattr(pipeline, "publish_html", lambda: requested.append(("html",)) or 0)
    monkeypatch.setattr(history, "operate", lambda *a, **kw: requested.append((a, kw)) or 0)
    assert cli.main([]) == 0
    assert cli.main(["--date", "20260731", "--dry-run"]) == 0
    assert cli.main(["status"]) == 0
    assert cli.main(["images", "20260731"]) == 0
    assert cli.main(["html"]) == 0
    assert cli.main(["hide", "20260731"]) == 0
    assert cli.main(["remove", "20260731"]) == 0
    assert cli.main(["remove", "20260731", "--confirm"]) == 0
    assert cli.main(["restore", "20260731"]) == 0
    assert requested == [
        ("sync", {"date": None, "dry_run": False}),
        ("sync", {"date": "20260731", "dry_run": True}),
        ("status",), ("images", "20260731"), ("html",),
        (("hide", "20260731"), {"confirm": False}),
        (("remove", "20260731"), {"confirm": False}),
        (("remove", "20260731"), {"confirm": True}),
        (("restore", "20260731"), {"confirm": False}),
    ]
    with pytest.raises(SystemExit):
        cli.main(["--date", "2026-07-31"])
    with pytest.raises(SystemExit):
        cli.main(["--dry-run", "remove", "20260731"])


def test_credentials_import_from_milgrau_checkout_no_new_yaml(project, monkeypatch):
    ctx = project
    source = ctx.workspace / "milgrau" / "credentials.py"
    source.parent.mkdir()
    source.write_text(
        "GITHUB_USER='spulidar'\nGITHUB_TOKEN='PRIVATE_TEST_TOKEN'\n"
        "R2_BUCKET_NAME='old-bucket'\nR2_ACCESS_KEY='ACCESS'\n"
        "R2_SECRET_KEY='SECRET'\nR2_ENDPOINT='https://r2.example.invalid'\n"
        "R2_PUBLIC_URL='https://public.example.invalid'\n"
    )
    for key in ("GITHUB_USER", "GITHUB_TOKEN", "R2_BUCKET_NAME", "R2_ACCESS_KEY",
                "R2_SECRET_KEY", "R2_ENDPOINT", "R2_PUBLIC_URL"):
        monkeypatch.delenv(key, raising=False)
    result = original_secrets(ctx, require_r2=True)
    assert result["GITHUB_TOKEN"] == "PRIVATE_TEST_TOKEN"
    assert result["R2_BUCKET_NAME"] == "old-bucket"
    monkeypatch.setenv("R2_BUCKET_NAME", "override-bucket")
    assert original_secrets(ctx, require_r2=True)["R2_BUCKET_NAME"] == "override-bucket"


def test_single_sync_publishes_git_state_after_r2_verification_and_cleans_cache(project, monkeypatch):
    ctx = project
    _, asset = staged_day(ctx)
    remote = R2()
    monkeypatch.setattr(pipeline, "_stage", lambda *_a, **_kw: {
        "sessions": 1, "invalid_l1": 0, "rendered": 1,
        "already_current": 0, "no_data": 0,
    })
    monkeypatch.setattr(pipeline, "r2_client", lambda _secrets: remote)
    commits = []
    def fake_commit(ctx_, settings):
        state = load_published_states(ctx_.repo, "spu")["2024"]
        assert state["days"]["20240913"]["publication_windows"]["12"]["assets"][ASSET]["key"] in {
            key for (_, key) in remote.objects
        }
        commits.append("commit")
    monkeypatch.setattr(pipeline, "commit_site", fake_commit)
    assert pipeline.sync() == 0
    assert commits == ["commit"]
    assert not asset.exists()
    assert json.loads((ctx.cache / "_publish-journal.json").read_text())["status"] == "complete"
    assert load_published_states(ctx.repo, "spu")["2024"]["days"]["20240913"]


def test_dry_run_has_no_r2_credentials_git_or_writes(project, monkeypatch):
    ctx = project
    _, asset = staged_day(ctx)
    monkeypatch.setattr(pipeline, "_stage", lambda *_a, **_kw: {
        "sessions": 1, "invalid_l1": 0, "rendered": 0,
        "already_current": 0, "no_data": 0,
    })
    monkeypatch.setattr(pipeline, "r2_client", lambda _x: pytest.fail("unexpected R2"))
    assert pipeline.sync(dry_run=True) == 0
    assert asset.exists()
    assert not (ctx.cache / "_publish-journal.json").exists()


def test_hide_and_unhide_preserve_r2_and_filter_generated_site(project, monkeypatch):
    ctx = project
    record = state_day(ctx)
    remote = R2()
    remote.objects[("example-bucket", record["assets"][ASSET]["key"])] = BYTES
    commits = []
    def fake_commit(ctx_, settings, *, allow_empty=False):
        site, n = build_unchanged_frontend(
            ctx_.repo, ctx_.cache, "spu", r2_public_base=settings["R2_PUBLIC_URL"]
        )
        commits.append(n)
    monkeypatch.setattr(pipeline, "commit_site", fake_commit)
    assert history.operate("hide", "20240913") == 0
    assert commits == [0]
    assert history.load_controls(ctx.repo, "spu")["hidden"] == ["20240913"]
    assert load_published_states(ctx.repo, "spu")["2024"]["days"]["20240913"]
    assert not remote.deleted
    assert history.operate("restore", "20240913") == 0
    assert commits == [0, 1]
    assert history.load_controls(ctx.repo, "spu")["hidden"] == []


def test_remove_is_preview_only_then_verifies_backup_before_git_and_deletion(project, monkeypatch):
    ctx = project
    record = state_day(ctx)
    remote = R2()
    key = record["assets"][ASSET]["key"]
    remote.objects[("example-bucket", key)] = BYTES
    monkeypatch.setattr(pipeline, "r2_client", lambda _settings: remote)
    calls = []
    def fake_commit(ctx_, settings, *, allow_empty=False):
        assert ("example-bucket", key) in remote.objects
        is_published = "20240913" in load_published_states(ctx_.repo, "spu")["2024"]["days"]
        assert is_published == bool(calls)  # first removal; second commit restores
        calls.append("Git site published")
    monkeypatch.setattr(pipeline, "commit_site", fake_commit)
    assert history.operate("remove", "20240913", confirm=False) == 0
    assert remote.objects[("example-bucket", key)] == BYTES
    assert not calls
    assert history.operate("remove", "20240913", confirm=True) == 0
    assert calls == ["Git site published"]
    assert ("example-bucket", key) not in remote.objects
    backup = f"_publication_v2_backups/{sha256(BYTES).hexdigest()}/{key}"
    assert remote.objects[("example-bucket", backup)] == BYTES
    assert "20240913" in history.load_controls(ctx.repo, "spu")["removed"]
    assert history.operate("remove", "20240913", confirm=True) == 0
    assert len(calls) == 1
    assert history.operate("restore", "20240913") == 0
    assert remote.objects[("example-bucket", key)] == BYTES
    assert load_published_states(ctx.repo, "spu")["2024"]["days"]["20240913"]
    assert not history.load_controls(ctx.repo, "spu")["removed"]


def test_remove_rejects_unverified_remote_object_without_mutating_history(project, monkeypatch):
    ctx = project
    record = state_day(ctx)
    key = record["assets"][ASSET]["key"]
    remote = R2()
    remote.objects[("example-bucket", key)] = b"untracked modification"
    monkeypatch.setattr(pipeline, "r2_client", lambda _settings: remote)
    with pytest.raises(RuntimeError, match="does not match"):
        history.operate("remove", "20240913", confirm=True)
    assert load_published_states(ctx.repo, "spu")["2024"]["days"]["20240913"]
    assert not history.load_controls(ctx.repo, "spu")["removed"]
    assert not remote.deleted


def test_tombstone_blocks_rediscovery_even_if_local_cached_webp_exists(project, monkeypatch):
    ctx = project
    _, asset = staged_day(ctx)
    ctl = history.load_controls(ctx.repo, "spu")
    ctl["hidden"].append("20240913")
    history.save_controls(ctx.repo, "spu", ctl)
    monkeypatch.setattr(pipeline, "_stage", lambda *_a, **_kw: {
        "sessions": 1, "invalid_l1": 0, "rendered": 0,
        "already_current": 0, "no_data": 0,
    })
    monkeypatch.setattr(pipeline, "r2_client", lambda _x: pytest.fail("R2 must not run"))
    assert pipeline.sync() == 0
    assert asset.is_file()
    assert not load_published_states(ctx.repo, "spu")



def test_images_regenerates_already_published_day_for_local_inspection(project, monkeypatch):
    ctx = project
    calls = []
    monkeypatch.setattr(pipeline, "_stage", lambda *_a, **kw: calls.append(kw) or {})
    from publisher import preview
    monkeypatch.setattr(preview, "build_preview", lambda *_a, **kw: (ctx.cache / "_preview-v2/index.html", 0))
    assert pipeline.images("20240913") == 0
    assert calls == [{"date": "20240913", "dry_run": False, "force": True}]


def _historical_scientific_source(ctx, date="20240913"):
    """Only metadata is needed: source content stays unread by the cache importer."""
    from datetime import datetime, timezone
    import os
    from publisher.windows import ScientificSessionSource

    sid = f"spu_{date}-1549Z_{date}-1750Z"
    source = ctx.scientific / "spu" / date[:4] / date[4:6] / sid / f"{sid}_L1.nc"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"scientific-original-L1-not-to-be-modified")
    os.utime(source, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    return ScientificSessionSource(
        session_id=sid, station_id="spu",
        start_utc=datetime(int(date[:4]), int(date[4:6]), int(date[6:]), 15, 49, tzinfo=timezone.utc),
        end_utc=datetime(int(date[:4]), int(date[4:6]), int(date[6:]), 17, 50, tzinfo=timezone.utc),
        level1_path=source,
    )


def test_cache_command_dispatch_is_explicit_and_not_the_normal_sync(monkeypatch):
    requested = []
    monkeypatch.setattr(pipeline, "publish_cached", lambda **kw: requested.append(kw) or 0)
    assert cli.main(["cache"]) == 0
    assert cli.main(["cache", "--date", "20240913"]) == 0
    assert cli.main(["cache", "--confirm"]) == 0
    assert requested == [
        {"date": None, "confirm": False},
        {"date": "20240913", "confirm": False},
        {"date": None, "confirm": True},
    ]


def test_historical_cache_is_audited_without_rendering_or_any_network_writes(project, monkeypatch):
    ctx = project
    _, image = staged_day(ctx)
    scientific = _historical_scientific_source(ctx)
    original_l1 = scientific.level1_path.read_bytes()
    from publisher import staging
    monkeypatch.setattr(pipeline, "discover_scientific_sessions",
                        lambda *_a, **_kw: (scientific,))
    monkeypatch.setattr(pipeline, "_stage",
                        lambda *_a, **_kw: pytest.fail("historical cache must never be rerendered"))
    monkeypatch.setattr(pipeline, "r2_client",
                        lambda *_a, **_kw: pytest.fail("audit must not contact R2"))
    assert pipeline.publish_cached() == 0
    assert image.is_file()
    assert scientific.level1_path.read_bytes() == original_l1
    assert not (ctx.cache / "_publish-journal.json").exists()
    assert not load_published_states(ctx.repo, ctx.station)


def test_historical_cache_confirm_uploads_old_fingerprint_without_rerender(project, monkeypatch):
    ctx = project
    _, image = staged_day(ctx)
    old_sidecar = json.loads((image.parent / f"{PUB}.json").read_text())
    scientific = _historical_scientific_source(ctx)
    original_l1 = scientific.level1_path.read_bytes()
    monkeypatch.setattr(pipeline, "discover_scientific_sessions",
                        lambda *_a, **_kw: (scientific,))
    monkeypatch.setattr(pipeline, "_stage",
                        lambda *_a, **_kw: pytest.fail("historical WEBPs must be reused"))
    remote = R2()
    monkeypatch.setattr(pipeline, "r2_client", lambda *_args: remote)
    commits = []

    def verify_commit(ctx_, settings):
        assert image.exists(), "cache must survive until Git confirms commit"
        state = load_published_states(ctx_.repo, "spu")["2024"]
        record = state["days"]["20240913"]["publication_windows"]["12"]
        assert record["source_fingerprint"] == old_sidecar["source_fingerprint"]
        assert record["assets"][ASSET]["key"] == f"spu/2024/09/20240913/{ASSET}"
        assert ("example-bucket", record["assets"][ASSET]["key"]) in remote.objects
        commits.append("verified")
    monkeypatch.setattr(pipeline, "commit_site", verify_commit)
    assert pipeline.publish_cached(confirm=True) == 0
    assert commits == ["verified"]
    assert not image.exists()
    assert scientific.level1_path.read_bytes() == original_l1
    assert load_published_states(ctx.repo, "spu")["2024"]


def test_historical_cache_rejects_missing_or_newer_l1_before_upload(project, monkeypatch):
    ctx = project
    _, image = staged_day(ctx)
    source = _historical_scientific_source(ctx)
    monkeypatch.setattr(pipeline, "discover_scientific_sessions",
                        lambda *_a, **_kw: (source,))
    monkeypatch.setattr(pipeline, "r2_client",
                        lambda *_args: pytest.fail("stale cache cannot upload"))
    source.level1_path.touch()
    with pytest.raises(RuntimeError, match="require regeneration"):
        pipeline.publish_cached(confirm=True)
    assert image.exists()
    source.level1_path.unlink()
    monkeypatch.setattr(pipeline, "discover_scientific_sessions",
                        lambda *_a, **_kw: ())
    with pytest.raises(RuntimeError, match="require regeneration"):
        pipeline.publish_cached(confirm=True)
    assert not load_published_states(ctx.repo, "spu")


def test_historical_cache_rejects_invalid_image_hash_and_blocked_days(project, monkeypatch):
    ctx = project
    _, image = staged_day(ctx)
    scientific = _historical_scientific_source(ctx)
    monkeypatch.setattr(pipeline, "discover_scientific_sessions",
                        lambda *_a, **_kw: (scientific,))
    image.write_bytes(b"tampered-image")
    with pytest.raises(ValueError, match="modified"):
        pipeline.publish_cached()
    image.write_bytes(BYTES)
    controls = history.load_controls(ctx.repo, "spu")
    controls["hidden"].append("20240913")
    history.save_controls(ctx.repo, "spu", controls)
    assert pipeline.publish_cached(confirm=False) == 0
    assert image.exists()  # hidden days are not consumed or deleted



def test_historical_cache_snapshot_rehashes_only_its_selected_batch(project, monkeypatch):
    from publisher.publication import publish_cache
    from publisher.v2 import collect_validated_staging

    ctx = project
    _, asset = staged_day(ctx)
    original = collect_validated_staging(ctx.cache, "spu")
    # A cached snapshot that is subsequently tampered must not publish.
    asset.write_bytes(b"later-corruption")
    remote = R2()
    with pytest.raises(ValueError, match="modified"):
        publish_cache(
            ctx.cache, "spu", ctx.repo, "example-bucket", remote,
            selected_window_ids={PUB}, validated_cache_snapshot=original,
            git_ready=lambda *_a: None,
            git_persist=lambda *_a: pytest.fail("cannot publish tampered cache"),
        )
    assert not remote.objects
    assert not load_published_states(ctx.repo, "spu")
