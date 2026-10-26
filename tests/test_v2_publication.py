"""R2 staging and persistent state safety tests; no real Git/R2/cloud access."""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json

import pytest

from publisher.publication import publish_cache, ensure_git_ready
from publisher.published import PUBLICATION_PREFIX, load_published_states
from publisher.r2_staging import ensure_uploaded

SESSION = "spu_20240913-1549Z_20240913-1750Z"
PUB = "20240913_spu_12"
ASSET = f"rcs_{PUB}_532nm_AN_15km.webp"


class FakeStream:
    def __init__(self, data):
        self.bio = BytesIO(data)

    def iter_chunks(self, chunk_size=1048576):
        while chunk := self.bio.read(chunk_size):
            yield chunk

    def close(self):
        self.bio.close()


class MissingObject(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class R2Fake:
    def __init__(self):
        self.objects = {}
        self.uploads = []

    def get_object(self, *, Bucket, Key):
        key = (Bucket, Key)
        if key not in self.objects:
            raise MissingObject()
        data = self.objects[key]
        return {"Body": FakeStream(data), "ContentLength": len(data)}

    def upload_file(self, filename, bucket, key, ExtraArgs):
        assert key.startswith(PUBLICATION_PREFIX + "/")
        self.objects[(bucket, key)] = open(filename, "rb").read()
        self.uploads.append((bucket, key))

    def copy_object(self, *, Bucket, CopySource, Key, MetadataDirective):
        assert MetadataDirective == "COPY"
        original = self.objects[(CopySource["Bucket"], CopySource["Key"])]
        self.objects[(Bucket, Key)] = original

    def corrupt(self):
        first = next(iter(self.objects))
        self.objects[first] = b"corrupt"


def sample_cache(tmp_path):
    root = tmp_path / ".cache" / "measurements"
    day = root / "spu" / "2024" / "09" / "20240913"
    day.mkdir(parents=True)
    asset = day / ASSET
    asset.write_bytes(b"verified-test-webp")
    data = {
        "publication_window_id": PUB,
        "source_sessions": [SESSION],
        "source_level1_files": [f"{SESSION}_L1.nc"],
        "source_level2_files": [],
        "source_fingerprint": "a" * 64,
        "scientific_sessions_merged": False,
        "scientific_netcdf_split": False,
        "visualization_only_session_concatenation": False,
        "assets": [ASSET],
        "asset_hashes": {ASSET: sha256(asset.read_bytes()).hexdigest()},
    }
    sidecar = day / (PUB + ".json")
    sidecar.write_text(json.dumps(data))
    return root, asset, sidecar


def simulate_git_save(repo, paths):
    assert all(str(p).startswith(str(repo)) for p in paths)
    assert all(p.is_file() for p in paths)


def publish(root, repo, remote, *, keep_cache=False, git_persist=simulate_git_save,
            replace_existing=False):
    return publish_cache(
        root, "spu", repo, "staging-test-bucket", remote,
        git_ready=lambda _: None,
        git_persist=git_persist,
        keep_cache=keep_cache,
        replace_existing=replace_existing,
    )


def test_success_upload_state_and_cleanup_without_other_cache_modification(tmp_path):
    root, asset, sidecar = sample_cache(tmp_path)
    weather = tmp_path / ".cache" / "weather" / "hourly.json"
    weather.parent.mkdir(parents=True)
    weather.write_bytes(b"independent-milgrau-cache")
    remote = R2Fake()
    repo = tmp_path / "measurements"
    result = publish(root, repo, remote)
    assert result == {
        "windows": 1, "uploaded_objects": 1,
        "changed_years": 1, "cleaned_windows": 1,
    }
    assert not asset.exists() and not sidecar.exists()
    assert weather.read_bytes() == b"independent-milgrau-cache"
    state = load_published_states(repo, "spu")["2024"]
    record = state["days"]["20240913"]["publication_windows"]["12"]
    info = record["assets"][ASSET]
    assert info["sha256"] == sha256(b"verified-test-webp").hexdigest()
    assert info["key"] == f"{PUBLICATION_PREFIX}/2024/09/20240913/{ASSET}"
    assert remote.objects[("staging-test-bucket", info["key"])] == b"verified-test-webp"
    assert record["scientific_netcdf_split"] is False
    # Published state survives an empty local image cache without replaying.
    assert publish(root, repo, remote)["windows"] == 0
    assert load_published_states(repo, "spu")["2024"] == state


def test_failed_git_persistence_preserves_all_cached_products(tmp_path):
    root, asset, sidecar = sample_cache(tmp_path)
    remote = R2Fake()

    def fail_git(repo, paths):
        raise RuntimeError("git push failed")

    with pytest.raises(RuntimeError, match="git push failed"):
        publish(root, tmp_path / "repo", remote, git_persist=fail_git)
    assert asset.is_file() and sidecar.is_file()
    assert len(remote.uploads) == 1


def test_failed_remote_verification_preserves_cache_and_no_state(tmp_path):
    root, asset, sidecar = sample_cache(tmp_path)
    remote = R2Fake()
    remote.upload_file = lambda filename, bucket, key, ExtraArgs: remote.objects.update(
        {(bucket, key): b"wrong-remote-bytes"}
    )
    with pytest.raises(RuntimeError, match="verification failed"):
        publish(root, tmp_path / "repo", remote)
    assert asset.is_file() and sidecar.is_file()
    assert load_published_states(tmp_path / "repo", "spu") == {}


def test_keep_cache_and_idempotent_remote_publish(tmp_path):
    root, asset, sidecar = sample_cache(tmp_path)
    repo = tmp_path / "repo"
    remote = R2Fake()
    first = publish(root, repo, remote, keep_cache=True)
    assert first["cleaned_windows"] == 0
    assert asset.is_file() and sidecar.is_file()
    second = publish(root, repo, remote)
    assert second["uploaded_objects"] == 0
    assert second["changed_years"] == 0
    assert not asset.exists() and not sidecar.exists()
    assert len(remote.uploads) == 1


def test_tampered_cached_webp_refused_before_any_remote_calls(tmp_path):
    root, asset, sidecar = sample_cache(tmp_path)
    asset.write_bytes(b"tampered")
    remote = R2Fake()
    with pytest.raises(ValueError, match="modified"):
        publish(root, tmp_path / "repo", remote)
    assert not remote.uploads and asset.exists() and sidecar.exists()


def test_refuse_git_branch_other_than_publication_v2(monkeypatch, tmp_path):
    from publisher import publication
    monkeypatch.setattr(publication, "_git", lambda repo, *args: "main")
    with pytest.raises(RuntimeError, match="publication-v2"):
        ensure_git_ready(tmp_path)


def test_remote_key_outside_staging_namespace_is_rejected(tmp_path):
    local = tmp_path / "bad.webp"
    local.write_bytes(b"test")
    remote = R2Fake()
    with pytest.raises(ValueError, match="outside canonical spu"):
        ensure_uploaded(remote, "bucket", local, {
            "key": "2024/09/20240913/bad.webp",
            "size": 4, "sha256": sha256(b"test").hexdigest(),
        })


def test_cache_cleanup_rejects_symlink_parent(tmp_path):
    from publisher.publication import clean_verified_window
    root, asset, sidecar = sample_cache(tmp_path)
    data = json.loads(sidecar.read_text())
    destination = tmp_path / "symlink-destination"
    destination.mkdir()
    # Re-point station folder to a symlink: do not delete anything through it.
    station = root / "spu"
    new_station = destination / "spu"
    station.rename(new_station)
    station.symlink_to(new_station, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked"):
        clean_verified_window(root, PUB, data)
    assert (new_station / "2024/09/20240913" / ASSET).exists()


def test_r2_write_rejects_legacy_year_root_even_with_valid_metadata(tmp_path):
    local = tmp_path / ASSET
    local.write_bytes(b"test")
    remote = R2Fake()
    with pytest.raises(ValueError, match="outside canonical spu"):
        ensure_uploaded(remote, "bucket", local, {
            "key": f"2024/09/20240913/{ASSET}",
            "size": 4, "sha256": sha256(b"test").hexdigest(),
        })
    assert not remote.objects and not remote.uploads


def test_r2_write_rejects_wrong_date_or_digest_under_spu(tmp_path):
    local = tmp_path / ASSET
    local.write_bytes(b"test")
    remote = R2Fake()
    digest = sha256(b"test").hexdigest()
    for key in (
        f"spu/2024/10/20240913/{ASSET}",
        f"spu/2024/09/20240914/{ASSET}",
        f"staging/publication-v2/spu/2024/09/20240913/{ASSET}",
    ):
        with pytest.raises(ValueError, match="outside canonical spu"):
            ensure_uploaded(remote, "bucket", local, {
                "key": key, "size": 4, "sha256": digest,
            })
    assert not remote.objects and not remote.uploads


def _change_cached_asset(asset, sidecar, payload: bytes):
    asset.write_bytes(payload)
    sidecar_data = json.loads(sidecar.read_text())
    sidecar_data["asset_hashes"][ASSET] = sha256(payload).hexdigest()
    sidecar_data["source_fingerprint"] = "b" * 64
    sidecar.write_text(json.dumps(sidecar_data))


def test_no_silent_overwrite_even_after_previous_verified_publication(tmp_path):
    root, asset, sidecar = sample_cache(tmp_path)
    remote = R2Fake()
    repo = tmp_path / "repo"
    publish(root, repo, remote, keep_cache=True)
    old_state = load_published_states(repo, "spu")["2024"]
    _change_cached_asset(asset, sidecar, b"updated-webp")
    with pytest.raises(RuntimeError, match="--replace-existing"):
        publish(root, repo, remote)
    assert remote.objects[("staging-test-bucket", f"spu/2024/09/20240913/{ASSET}")] == b"verified-test-webp"
    assert asset.is_file() and sidecar.is_file()
    assert load_published_states(repo, "spu")["2024"] == old_state


def test_explicit_replacement_makes_verified_backup_and_updates_state(tmp_path):
    from publisher.r2_staging import previous_backup_key

    root, asset, sidecar = sample_cache(tmp_path)
    remote = R2Fake()
    repo = tmp_path / "repo"
    publish(root, repo, remote, keep_cache=True)
    old_state = load_published_states(repo, "spu")["2024"]
    old = old_state["days"]["20240913"]["publication_windows"]["12"]["assets"][ASSET]
    _change_cached_asset(asset, sidecar, b"updated-webp")
    result = publish(root, repo, remote, replace_existing=True)
    assert result["windows"] == result["cleaned_windows"] == 1
    key = old["key"]
    backup = previous_backup_key(key, old["sha256"])
    assert remote.objects[("staging-test-bucket", key)] == b"updated-webp"
    assert remote.objects[("staging-test-bucket", backup)] == b"verified-test-webp"
    new = load_published_states(repo, "spu")["2024"]["days"]["20240913"]["publication_windows"]["12"]["assets"][ASSET]
    assert new["key"] == old["key"]
    assert new["sha256"] == sha256(b"updated-webp").hexdigest()
    assert not asset.exists() and not sidecar.exists()


def test_rollback_after_failed_git_push_reinstates_previous_remote_bytes(tmp_path):
    from publisher.r2_staging import previous_backup_key

    root, asset, sidecar = sample_cache(tmp_path)
    remote = R2Fake()
    repo = tmp_path / "repo"
    publish(root, repo, remote, keep_cache=True)
    old = load_published_states(repo, "spu")["2024"]["days"]["20240913"]["publication_windows"]["12"]["assets"][ASSET]
    _change_cached_asset(asset, sidecar, b"updated-webp")

    def fail_git(_repo, _paths):
        raise RuntimeError("git push failed")

    with pytest.raises(RuntimeError, match="git push failed"):
        publish(root, repo, remote, replace_existing=True, git_persist=fail_git)
    assert remote.objects[("staging-test-bucket", old["key"])] == b"verified-test-webp"
    assert remote.objects[("staging-test-bucket", previous_backup_key(old["key"], old["sha256"]))] == b"verified-test-webp"
    assert asset.is_file() and sidecar.is_file()


def test_refuse_replacing_untracked_or_altered_r2_objects(tmp_path):
    root, asset, sidecar = sample_cache(tmp_path)
    remote = R2Fake()
    key = f"spu/2024/09/20240913/{ASSET}"
    remote.objects[("staging-test-bucket", key)] = b"other-owner"
    with pytest.raises(RuntimeError, match="untracked"):
        publish(root, tmp_path / "repo", remote, replace_existing=True)
    assert remote.objects[("staging-test-bucket", key)] == b"other-owner"
    assert asset.is_file() and sidecar.is_file()


def test_r2_read_timeout_resumes_from_new_stream(tmp_path, monkeypatch):
    from botocore.exceptions import ReadTimeoutError
    from publisher import r2_staging

    root, asset, _ = sample_cache(tmp_path)
    remote = R2Fake()
    key = f"spu/2024/09/20240913/{ASSET}"
    data = asset.read_bytes()
    remote.objects[("bucket", key)] = data
    real_get = remote.get_object
    calls = []
    class BrokenStream:
        def iter_chunks(self, chunk_size):
            yield b"partial"
            raise ReadTimeoutError(endpoint_url="https://example.invalid", error="timeout")
        def close(self):
            pass
    def get(*, Bucket, Key):
        calls.append(Key)
        return ({"Body": BrokenStream(), "ContentLength": len(data)}
                if len(calls) == 1 else real_get(Bucket=Bucket, Key=Key))
    remote.get_object = get
    monkeypatch.setattr(r2_staging.time, "sleep", lambda _: None)
    metadata = {"key": key, "size": len(data), "sha256": sha256(data).hexdigest()}
    assert r2_staging.ensure_uploaded(remote, "bucket", asset, metadata) is False
    assert len(calls) == 2
    assert not remote.uploads


def test_persistent_r2_stream_timeout_keeps_cache(tmp_path, monkeypatch):
    from botocore.exceptions import ReadTimeoutError
    from publisher import r2_staging

    root, asset, sidecar = sample_cache(tmp_path)
    remote = R2Fake()
    calls = []
    class BrokenStream:
        def iter_chunks(self, chunk_size):
            raise ReadTimeoutError(endpoint_url="https://example.invalid", error="timeout")
            yield b""  # pragma: no cover
        def close(self):
            calls.append("closed")
    remote.get_object = lambda **_: {
        "Body": BrokenStream(), "ContentLength": asset.stat().st_size
    }
    monkeypatch.setattr(r2_staging.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="after 5 attempts"):
        publish(root, tmp_path / "repo", remote)
    assert len(calls) == 5
    assert asset.exists() and sidecar.exists()
    assert load_published_states(tmp_path / "repo", "spu") == {}
