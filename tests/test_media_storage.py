"""Photos must survive a redeploy.

Media lived on the container filesystem, which is ephemeral: every deploy
silently destroyed every uploaded photo. Nothing errored — it just came back
empty, which is the worst shape for data loss because no alarm fires.

These cover the storage seam: the local store still behaves, an S3-compatible
store round-trips, and production refuses to boot on the lossy path.
"""

from __future__ import annotations

import pytest

from app.services import media


class FakeClientError(Exception):
    """Shaped like botocore's ClientError, which is what boto3 actually raises.

    The distinction matters: botocore does not raise a class *named* NoSuchKey,
    it raises ClientError with the code buried in `response`. A fake that
    raised a helpfully-named exception would have let a real bug ship.
    """

    def __init__(self, code: str) -> None:
        super().__init__(f"An error occurred ({code})")
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """Minimal stand-in for the boto3 client surface S3MediaStore uses."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.acls: list[str] = []

    def put_object(self, *, Bucket, Key, Body, ContentType, ACL):
        self.objects[Key] = Body
        self.acls.append(ACL)

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey")
        return {"Body": _Body(self.objects[Key])}

    def delete_objects(self, *, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def _image(key: str = "abc123") -> media.ProcessedImage:
    return media.ProcessedImage(
        data=b"full-image-bytes",
        thumb=b"thumb-bytes",
        width=800,
        height=600,
        phash="0" * 16,
        storage_key=key,
    )


def _s3_store(monkeypatch) -> tuple[media.S3MediaStore, FakeS3]:
    fake = FakeS3()
    store = media.S3MediaStore.__new__(media.S3MediaStore)
    store.bucket = "rentsafe-media"
    store.prefix = "media"
    store._client = fake
    return store, fake


def test_local_store_round_trips(tmp_path):
    store = media.LocalMediaStore(tmp_path)
    store.save(_image())
    assert store.load("abc123") == b"full-image-bytes"
    assert store.load("abc123", thumb=True) == b"thumb-bytes"
    store.delete("abc123")
    assert store.load("abc123") is None


def test_s3_store_round_trips(monkeypatch):
    store, fake = _s3_store(monkeypatch)
    store.save(_image())

    assert fake.objects["media/abc123.jpg"] == b"full-image-bytes"
    assert fake.objects["media/abc123_thumb.jpg"] == b"thumb-bytes"
    assert store.load("abc123") == b"full-image-bytes"
    assert store.load("abc123", thumb=True) == b"thumb-bytes"

    store.delete("abc123")
    assert store.load("abc123") is None


def test_s3_objects_are_never_public(monkeypatch):
    """A public CDN URL would route around moderation.

    Photos are held until approved. If the object were readable directly from
    the bucket, a rejected image would still be fetchable by anyone holding the
    URL, and the approval check in photos.py would be decorative.
    """
    store, fake = _s3_store(monkeypatch)
    store.save(_image())
    assert set(fake.acls) == {"private"}


def test_missing_object_reads_as_absent_not_an_error(monkeypatch):
    """A property with no photo is normal; it must not 500."""
    store, _ = _s3_store(monkeypatch)
    assert store.load("never-uploaded") is None


def test_a_real_s3_failure_still_propagates(monkeypatch):
    """"Absent" must mean absent — not "the bucket is misconfigured".

    Swallowing every error here would turn an outage or a credentials problem
    into every photo silently vanishing, which is exactly the failure mode this
    whole task exists to remove.
    """
    store, fake = _s3_store(monkeypatch)

    def broken(**kw):
        raise FakeClientError("AccessDenied")

    fake.get_object = broken
    with pytest.raises(FakeClientError):
        store.load("abc123")


def test_s3_store_rejects_keys_that_escape_the_prefix(monkeypatch):
    """Keys are ours, but path traversal into another tenant's prefix is fatal."""
    store, _ = _s3_store(monkeypatch)
    for bad in ("../secrets", "a/b", "..\\windows"):
        with pytest.raises(ValueError):
            store._key(bad)


def test_local_store_rejects_keys_that_escape_the_root(tmp_path):
    store = media.LocalMediaStore(tmp_path)
    for bad in ("../secrets", "a/b", "..\\windows"):
        with pytest.raises(ValueError):
            store._path(bad)


def test_production_without_a_bucket_refuses_to_boot(monkeypatch):
    """Silent data loss on first redeploy is worse than a loud failure at boot."""
    settings = media.get_settings()
    monkeypatch.setattr(settings, "media_bucket", "")
    monkeypatch.setattr(settings, "debug", False)

    with pytest.raises(RuntimeError, match="MEDIA_BUCKET"):
        media.build_store()


def test_development_without_a_bucket_uses_local_disk(monkeypatch):
    settings = media.get_settings()
    monkeypatch.setattr(settings, "media_bucket", "")
    monkeypatch.setattr(settings, "debug", True)
    assert isinstance(media.build_store(), media.LocalMediaStore)


def test_a_configured_bucket_wins_in_any_environment(monkeypatch):
    settings = media.get_settings()
    monkeypatch.setattr(settings, "media_bucket", "rentsafe-media")
    monkeypatch.setattr(settings, "debug", True)

    built = {}

    class FakeStore(media.S3MediaStore):
        def __init__(self, bucket, *, endpoint_url="", prefix="media"):
            built["bucket"] = bucket
            built["endpoint"] = endpoint_url

    monkeypatch.setattr(media, "S3MediaStore", FakeStore)
    media.build_store()
    assert built["bucket"] == "rentsafe-media"
