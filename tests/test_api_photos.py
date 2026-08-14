"""Photo upload and saved-property tests.

The photo assertions are mostly about the three things that can go wrong with
user-supplied images: leaking the uploader's location via EXIF, accepting
something that isn't an image, and publishing before a human has looked.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from app.core import geohash, phash
from app.db.models import LGA, Neighbourhood, Property, PropertyPhoto
from app.services import media
from tests.conftest import ADMIN_PHONE, OTHER_PHONE, TENANT_PHONE, auth, login

PROPERTY_ID = "ETI-LEK-7F3A2B-0041"


@pytest.fixture(autouse=True)
def media_root(tmp_path, monkeypatch):
    """Never write into the real media directory from a test."""
    monkeypatch.setattr(media, "store", media.LocalMediaStore(tmp_path))


@pytest.fixture
async def property_row(session_factory):
    async with session_factory() as s:
        s.add(LGA(code="ETI", name="Eti-Osa"))
        s.add(Neighbourhood(code="LEK", name="Lekki Phase 1", lga_code="ETI"))
        gh8, gh7 = geohash.encode_pair(6.4474, 3.4736)
        prop = Property(
            property_id=PROPERTY_ID, geohash_7=gh7, geohash_8=gh8,
            lat=6.4474, lng=3.4736, address_local="12A Admiralty Way",
            lga_code="ETI", neighbourhood_code="LEK",
        )
        s.add(prop)
        await s.commit()
        await s.refresh(prop)
        return prop


def make_image(
    *, size=(900, 700), colour=(60, 120, 90), fmt="JPEG", with_gps=True, layout=0
) -> bytes:
    """A JPEG carrying GPS EXIF, like a real phone photo.

    Deliberately photo-shaped — a sky gradient with a few large blocks for
    windows. A fine checkerboard would be the pathological input for any
    perceptual hash: it aliases differently at every downsample size, so it
    would test the resampler rather than the hash.
    """
    img = Image.new("RGB", size, colour)
    w, h = size
    draw = ImageDraw.Draw(img)
    # Vertical gradient: sky above, building below.
    for y in range(h):
        t = y / h
        draw.line(
            [(0, y), (w, y)],
            fill=(
                int(colour[0] * (1 - t) + 210 * t),
                int(colour[1] * (1 - t) + 190 * t),
                int(colour[2] * (1 - t) + 160 * t),
            ),
        )
    # A handful of large rectangles — low-frequency structure, like windows.
    # `layout` changes where they sit, which is what actually distinguishes one
    # building from another to a perceptual hash. Colour deliberately does not:
    # pHash works on greyscale with the brightness term dropped, so the same
    # building at noon and at dusk is meant to hash the same.
    for i in range(3):
        for j in range(2):
            x0 = int(w * (0.12 + i * 0.28)) if layout == 0 else int(w * (0.05 + j * 0.5))
            y0 = int(h * (0.35 + j * 0.28)) if layout == 0 else int(h * (0.1 + i * 0.26))
            draw.rectangle(
                [x0, y0, x0 + int(w * 0.18), y0 + int(h * 0.18)],
                fill=(40, 45, 60),
            )
    buf = io.BytesIO()
    if with_gps and fmt == "JPEG":
        exif = Image.Exif()
        exif[0x8825] = {1: "N", 2: (6.0, 26.0, 50.0)}  # GPSInfo
        exif[0x010F] = "TestPhone"                      # Make
        img.save(buf, format=fmt, exif=exif)
    else:
        img.save(buf, format=fmt)
    return buf.getvalue()


# --------------------------------------------------------------- processing

def test_exif_is_stripped():
    """A tenant photographing their street must not publish their address."""
    raw = make_image()
    assert Image.open(io.BytesIO(raw)).getexif(), "fixture should carry EXIF"

    processed = media.process_upload(raw)

    out = Image.open(io.BytesIO(processed.data))
    assert not dict(out.getexif()), "EXIF survived processing"


def test_non_image_is_rejected():
    with pytest.raises(media.ImageRejected):
        media.process_upload(b"<html><script>alert(1)</script></html>")


def test_oversized_upload_is_rejected():
    with pytest.raises(media.ImageRejected, match="under"):
        media.process_upload(b"\xff" * (media.MAX_UPLOAD_BYTES + 1))


def test_tiny_image_is_rejected():
    with pytest.raises(media.ImageRejected, match="too small"):
        media.process_upload(make_image(size=(50, 50), with_gps=False))


def test_large_image_is_resized():
    processed = media.process_upload(make_image(size=(3000, 2000), with_gps=False))
    assert max(processed.width, processed.height) <= media.MAX_EDGE_PX


def test_phash_is_stable_across_rescaling_and_recompression():
    """The same photo, resized and re-compressed, must hash to the same building.

    Rescale the *same* pixels rather than regenerating the pattern at another
    size — a fixed-cell checkerboard drawn at two sizes is genuinely a
    different image, which is a property of the fixture, not the hash.
    """
    original = make_image(size=(900, 700), with_gps=False)

    img = Image.open(io.BytesIO(original)).resize((600, 467), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=55)  # heavier compression than we store

    a = media.process_upload(original).phash
    b = media.process_upload(buf.getvalue()).phash
    assert phash.is_same_building(a, b), phash.hamming_distance(a, b)


def test_phash_separates_different_buildings():
    """Different structure must not collide."""
    a = media.process_upload(make_image(with_gps=False, layout=0)).phash
    b = media.process_upload(make_image(with_gps=False, layout=1)).phash
    assert not phash.is_same_building(a, b), phash.hamming_distance(a, b)


def test_phash_ignores_lighting_and_colour():
    """The same building at noon and at dusk is still the same building."""
    a = media.process_upload(make_image(colour=(20, 20, 30), with_gps=False)).phash
    b = media.process_upload(make_image(colour=(200, 170, 140), with_gps=False)).phash
    assert phash.is_same_building(a, b), phash.hamming_distance(a, b)


def test_storage_key_rejects_traversal(tmp_path):
    store = media.LocalMediaStore(tmp_path)
    with pytest.raises(ValueError, match="invalid storage key"):
        store.load("../../etc/passwd")


# ------------------------------------------------------------------- routes

async def test_upload_requires_sign_in(client, property_row):
    r = await client.post(
        f"/properties/{PROPERTY_ID}/photos",
        files={"file": ("x.jpg", make_image(), "image/jpeg")},
    )
    assert r.status_code == 401


async def test_uploaded_photo_is_held_for_moderation(client, users, property_row):
    token = await login(client, TENANT_PHONE)
    r = await client.post(
        f"/properties/{PROPERTY_ID}/photos",
        files={"file": ("street.jpg", make_image(), "image/jpeg")},
        data={"caption": "Street after October rain"},
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    assert r.json()["photo"]["moderation_status"] == "pending"

    # Not public...
    assert (await client.get(f"/properties/{PROPERTY_ID}/photos")).json() == []
    # ...but visible to whoever uploaded it.
    mine = await client.get(f"/properties/{PROPERTY_ID}/photos", headers=auth(token))
    assert len(mine.json()) == 1


async def test_pending_image_bytes_are_not_served_to_strangers(
    client, users, property_row
):
    """Hiding it from the listing is pointless if the bytes are still fetchable."""
    token = await login(client, TENANT_PHONE)
    up = await client.post(
        f"/properties/{PROPERTY_ID}/photos",
        files={"file": ("a.jpg", make_image(), "image/jpeg")},
        headers=auth(token),
    )
    url = up.json()["photo"]["url"].replace("/api/v1", "")

    assert (await client.get(url)).status_code == 404          # anonymous
    other = await login(client, OTHER_PHONE)
    assert (await client.get(url, headers=auth(other))).status_code == 404
    assert (await client.get(url, headers=auth(token))).status_code == 200  # owner


async def test_served_image_forbids_content_sniffing(client, users, property_row):
    token = await login(client, TENANT_PHONE)
    up = await client.post(
        f"/properties/{PROPERTY_ID}/photos",
        files={"file": ("a.jpg", make_image(), "image/jpeg")},
        headers=auth(token),
    )
    url = up.json()["photo"]["url"].replace("/api/v1", "")
    r = await client.get(url, headers=auth(token))
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_approved_photo_becomes_public(
    client, users, property_row, session_factory
):
    token = await login(client, TENANT_PHONE)
    await client.post(
        f"/properties/{PROPERTY_ID}/photos",
        files={"file": ("a.jpg", make_image(), "image/jpeg")},
        headers=auth(token),
    )
    async with session_factory() as s:
        photo = (await s.execute(select(PropertyPhoto))).scalars().one()
        photo.moderation_status = "approved"
        await s.commit()

    public = await client.get(f"/properties/{PROPERTY_ID}/photos")
    assert len(public.json()) == 1


async def test_duplicate_photo_is_flagged_for_the_moderator(
    client, users, property_row
):
    token = await login(client, TENANT_PHONE)
    image = make_image(with_gps=False)
    first = await client.post(
        f"/properties/{PROPERTY_ID}/photos",
        files={"file": ("a.jpg", image, "image/jpeg")},
        headers=auth(token),
    )
    second = await client.post(
        f"/properties/{PROPERTY_ID}/photos",
        files={"file": ("b.jpg", image, "image/jpeg")},
        headers=auth(token),
    )
    assert second.json()["duplicate_of_photo_id"] == first.json()["photo"]["id"]


async def test_rubbish_upload_gets_a_useful_error(client, users, property_row):
    token = await login(client, TENANT_PHONE)
    r = await client.post(
        f"/properties/{PROPERTY_ID}/photos",
        files={"file": ("evil.jpg", b"not an image at all", "image/jpeg")},
        headers=auth(token),
    )
    assert r.status_code == 422
    assert "image" in r.json()["detail"].lower()


async def test_only_the_owner_can_delete_a_photo(client, users, property_row):
    token = await login(client, TENANT_PHONE)
    up = await client.post(
        f"/properties/{PROPERTY_ID}/photos",
        files={"file": ("a.jpg", make_image(), "image/jpeg")},
        headers=auth(token),
    )
    photo_id = up.json()["photo"]["id"]

    other = await login(client, OTHER_PHONE)
    assert (await client.delete(f"/photos/{photo_id}", headers=auth(other))).status_code == 403
    # Admins can, for takedown requests.
    admin = await login(client, ADMIN_PHONE)
    assert (await client.delete(f"/photos/{photo_id}", headers=auth(admin))).status_code == 204


# ------------------------------------------------------------ saved properties

async def test_save_and_unsave_a_property(client, users, property_row):
    token = await login(client, TENANT_PHONE)
    assert (await client.get("/users/me/saved", headers=auth(token))).json() == []

    r = await client.put(
        f"/properties/{PROPERTY_ID}/save",
        json={"note": "Viewing on Saturday"},
        headers=auth(token),
    )
    assert r.status_code == 204

    saved = (await client.get("/users/me/saved", headers=auth(token))).json()
    assert len(saved) == 1
    assert saved[0]["property_id"] == PROPERTY_ID
    assert saved[0]["note"] == "Viewing on Saturday"
    assert saved[0]["area_name"] == "Lekki Phase 1"

    assert (
        await client.delete(f"/properties/{PROPERTY_ID}/save", headers=auth(token))
    ).status_code == 204
    assert (await client.get("/users/me/saved", headers=auth(token))).json() == []


async def test_saving_twice_is_not_an_error(client, users, property_row):
    """Double-tapping a bookmark shouldn't 500 on a unique constraint."""
    token = await login(client, TENANT_PHONE)
    for _ in range(3):
        r = await client.put(f"/properties/{PROPERTY_ID}/save", headers=auth(token))
        assert r.status_code == 204
    assert len((await client.get("/users/me/saved", headers=auth(token))).json()) == 1


async def test_saves_are_private_to_each_user(client, users, property_row):
    mine = await login(client, TENANT_PHONE)
    theirs = await login(client, OTHER_PHONE)

    await client.put(f"/properties/{PROPERTY_ID}/save", headers=auth(mine))

    assert len((await client.get("/users/me/saved", headers=auth(mine))).json()) == 1
    assert (await client.get("/users/me/saved", headers=auth(theirs))).json() == []


async def test_saving_requires_sign_in(client, property_row):
    assert (await client.put(f"/properties/{PROPERTY_ID}/save")).status_code == 401


async def test_unsaving_something_never_saved_is_fine(client, users, property_row):
    token = await login(client, TENANT_PHONE)
    r = await client.delete(f"/properties/{PROPERTY_ID}/save", headers=auth(token))
    assert r.status_code == 204
