"""Image intake: validation, EXIF stripping, resizing and perceptual hashing.

Photos are the highest-risk user input this product accepts. Three separate
concerns, in order of importance:

  1. Privacy. Phone photos carry GPS coordinates, timestamps and device serials
     in EXIF. A tenant uploading evidence of a flooded street must not thereby
     publish their home address. Every image is decoded and re-encoded from
     pixels, which drops all metadata — the only reliable way to strip it.

  2. Safety. Content-Type headers and file extensions are attacker-controlled,
     so neither is trusted. An upload is an image only if Pillow can decode it,
     and what we store is our own re-encode, never the bytes we received.
     That also defuses polyglot files (a valid GIF that is also valid HTML).

  3. Identity. A perceptual hash lets the same building photographed from a
     different angle or in different light resolve to the same PropertyID
     (Section II, step 3). ``app.core.phash`` owns the comparison; this module
     produces the hash.
"""

from __future__ import annotations

import io
import math
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import get_settings

# Cap before we ever hand bytes to a decoder.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

# Pillow's own guard against decompression bombs: a small file that claims to
# be 50000x50000 and exhausts memory on decode.
Image.MAX_IMAGE_PIXELS = 40_000_000

# Longest edge we keep. Evidence photos are viewed on phones; storing 12MP
# originals costs bandwidth Lagos users pay for by the megabyte.
MAX_EDGE_PX = 1600
THUMB_EDGE_PX = 400
JPEG_QUALITY = 82

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "HEIF", "HEIC"}


class ImageRejected(ValueError):
    """The upload isn't a usable image, with a reason safe to show a user."""


@dataclass
class ProcessedImage:
    data: bytes
    thumb: bytes
    width: int
    height: int
    phash: str
    storage_key: str


@lru_cache(maxsize=4)
def _dct_basis(n: int) -> tuple[tuple[float, ...], ...]:
    """Cosine basis for an n-point type-II DCT, computed once per size.

    Recomputing ``cos`` inside the transform meant ~65,000 trig calls per hash.
    The basis only depends on n, so it is built once and reused — the transform
    then reduces to multiply-adds, which matters because this runs in a worker
    thread and pure-Python arithmetic holds the GIL.
    """
    factor = math.pi / n
    return tuple(
        tuple(math.cos((i + 0.5) * k * factor) for i in range(n)) for k in range(n)
    )


def _dct_1d(vector: list[float]) -> list[float]:
    """Type-II DCT over a short vector (n = 32 here).

    Plain accumulation rather than ``math.fsum``: exact summation costs more
    than it saves here, and the result only feeds a median threshold, so the
    last bits of floating-point precision don't change the hash.
    """
    basis = _dct_basis(len(vector))
    out = []
    for row in basis:
        total = 0.0
        for i, v in enumerate(vector):
            total += v * row[i]
        out.append(total)
    return out


def perceptual_hash(image: Image.Image, size: int = 32, low_freq: int = 8) -> str:
    """64-bit pHash as 16 hex chars, comparable via ``core.phash``.

    Standard construction: greyscale, downsample, 2-D DCT, keep the top-left
    low-frequency block (which carries structure rather than detail), and
    threshold each coefficient against the median. Robust to rescaling,
    compression and moderate brightness shifts — which is exactly the variation
    you get between two tenants photographing the same building.
    """
    img = image.convert("L").resize((size, size), Image.Resampling.LANCZOS)
    pixels = img.tobytes()  # one byte per pixel for mode "L"
    rows = [
        [float(pixels[y * size + x]) for x in range(size)] for y in range(size)
    ]

    dct_rows = [_dct_1d(row) for row in rows]
    dct = [
        _dct_1d([dct_rows[y][x] for y in range(size)])
        for x in range(size)
    ]
    # dct[x][y] after the column pass; take the low-frequency corner.
    block = [dct[x][y] for y in range(low_freq) for x in range(low_freq)]

    # The DC term encodes overall brightness, not structure — excluding it is
    # what makes the hash tolerant of lighting differences.
    without_dc = block[1:]
    ordered = sorted(without_dc)
    mid = len(ordered) // 2
    median = (
        ordered[mid]
        if len(ordered) % 2
        else (ordered[mid - 1] + ordered[mid]) / 2
    )

    bits = 0
    for i, value in enumerate(block):
        if value > median:
            bits |= 1 << (len(block) - 1 - i)
    return f"{bits:016x}"


def process_upload(raw: bytes, *, filename: str | None = None) -> ProcessedImage:
    """Validate, strip metadata, resize and hash an uploaded image.

    Raises ``ImageRejected`` with a message suitable for display.
    """
    if not raw:
        raise ImageRejected("That file was empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ImageRejected(
            f"Images must be under {MAX_UPLOAD_BYTES // (1024 * 1024)}MB. "
            "Most phones can share a smaller copy."
        )

    try:
        probe = Image.open(io.BytesIO(raw))
        image_format = (probe.format or "").upper()
        probe.verify()  # structural check; consumes the object
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageRejected("That file isn't an image we can read.") from exc

    if image_format not in ALLOWED_FORMATS:
        raise ImageRejected(f"{image_format or 'That format'} isn't supported. Use JPEG, PNG or WebP.")

    # verify() leaves the image unusable, so reopen for the real decode.
    try:
        img = Image.open(io.BytesIO(raw))
        # Honour the EXIF orientation flag *before* dropping EXIF, otherwise
        # portrait photos come out sideways once the metadata is gone.
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except (OSError, ValueError) as exc:
        raise ImageRejected("That image couldn't be decoded.") from exc

    if img.width < 200 or img.height < 200:
        raise ImageRejected("That image is too small to be useful as evidence.")

    digest = perceptual_hash(img)

    full = ImageOps.contain(img, (MAX_EDGE_PX, MAX_EDGE_PX), Image.Resampling.LANCZOS)
    thumb = ImageOps.contain(img, (THUMB_EDGE_PX, THUMB_EDGE_PX), Image.Resampling.LANCZOS)

    def encode(im: Image.Image) -> bytes:
        buf = io.BytesIO()
        # A fresh save from pixel data carries no EXIF, ICC, XMP or GPS.
        im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue()

    # The stored name is ours, never the user's: an uploaded filename is a path
    # traversal waiting to happen.
    key = secrets.token_urlsafe(16)

    return ProcessedImage(
        data=encode(full),
        thumb=encode(thumb),
        width=full.width,
        height=full.height,
        phash=digest,
        storage_key=key,
    )


class MediaStore:
    """What the rest of the app needs from a place to keep images."""

    def save(self, image: ProcessedImage) -> None:
        raise NotImplementedError

    def load(self, key: str, *, thumb: bool = False) -> bytes | None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


class LocalMediaStore(MediaStore):
    """Filesystem-backed store. Correct for development, lossy in production.

    A container filesystem is ephemeral: every redeploy takes the photos with
    it. That is data loss wearing an infrastructure costume — a tenant's
    evidence photo of a flooded compound is not something to lose on a deploy —
    so anything but local development should be on :class:`S3MediaStore`.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str, *, thumb: bool = False) -> Path:
        # Keys are generated by us, but this is defence in depth: reject
        # anything that could escape the media root.
        if "/" in key or "\\" in key or ".." in key:
            raise ValueError("invalid storage key")
        return self.root / f"{key}{'_thumb' if thumb else ''}.jpg"

    def save(self, image: ProcessedImage) -> None:
        self._path(image.storage_key).write_bytes(image.data)
        self._path(image.storage_key, thumb=True).write_bytes(image.thumb)

    def load(self, key: str, *, thumb: bool = False) -> bytes | None:
        path = self._path(key, thumb=thumb)
        return path.read_bytes() if path.exists() else None

    def delete(self, key: str) -> None:
        for thumb in (False, True):
            path = self._path(key, thumb=thumb)
            if path.exists():
                path.unlink()


class S3MediaStore(MediaStore):
    """Any S3-compatible object store: AWS, Cloudflare R2, Spaces, MinIO.

    One implementation covers all of them because they share an API, which is
    why this was preferred over the Cloudinary SDK that pyproject declared and
    nothing imported. Cloudinary would also have meant image *serving* moving
    to a third party, and these photos are moderated — an unlisted-but-public
    CDN URL would route around the approval check in ``photos.py``.

    Objects stay private. The app streams bytes through its own authenticated
    endpoint, so a held or rejected photo is never publicly reachable.
    """

    def __init__(self, bucket: str, *, endpoint_url: str = "", prefix: str = "media") -> None:
        import boto3  # imported lazily: local dev needn't install it

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        # Region and credentials come from the standard environment variables,
        # so the same code works with an instance role or explicit keys.
        self._client = boto3.client(
            "s3", endpoint_url=endpoint_url or None
        )

    def _key(self, key: str, *, thumb: bool = False) -> str:
        if "/" in key or "\\" in key or ".." in key:
            raise ValueError("invalid storage key")
        return f"{self.prefix}/{key}{'_thumb' if thumb else ''}.jpg"

    def save(self, image: ProcessedImage) -> None:
        for thumb, payload in ((False, image.data), (True, image.thumb)):
            self._client.put_object(
                Bucket=self.bucket,
                Key=self._key(image.storage_key, thumb=thumb),
                Body=payload,
                ContentType="image/jpeg",
                # Explicit even though bucket policy should also enforce it:
                # a public object would bypass moderation.
                ACL="private",
            )

    def load(self, key: str, *, thumb: bool = False) -> bytes | None:
        try:
            obj = self._client.get_object(
                Bucket=self.bucket, Key=self._key(key, thumb=thumb)
            )
        except Exception as exc:
            # botocore signals a missing object with a generic ``ClientError``
            # carrying the real code in ``response``, so matching on the
            # exception *class* name never fires — an absent photo would raise
            # a 500 instead of rendering as "no photo".
            code = ""
            response = getattr(exc, "response", None)
            if isinstance(response, dict):
                code = str(response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "NoSuchBucket", "404"}:
                return None
            raise
        return obj["Body"].read()

    def delete(self, key: str) -> None:
        self._client.delete_objects(
            Bucket=self.bucket,
            Delete={
                "Objects": [
                    {"Key": self._key(key, thumb=False)},
                    {"Key": self._key(key, thumb=True)},
                ]
            },
        )


MEDIA_ROOT = Path(__file__).resolve().parents[2] / "data" / "media"


def build_store() -> MediaStore:
    """Object storage when a bucket is configured, local disk otherwise.

    Production without a bucket is refused rather than quietly served from
    local disk. The failure mode of getting this wrong is silent: everything
    works until the first redeploy, and then every tenant photo is gone with no
    error anywhere. Better to fail at boot.
    """
    settings = get_settings()
    if settings.media_bucket:
        return S3MediaStore(
            settings.media_bucket,
            endpoint_url=settings.media_endpoint_url,
            prefix=settings.media_prefix,
        )
    if settings.debug:
        return LocalMediaStore(MEDIA_ROOT)
    raise RuntimeError(
        "MEDIA_BUCKET is required outside development. Uploaded photos on a "
        "container filesystem are lost on every redeploy."
    )


_store: MediaStore | None = None


def get_store() -> MediaStore:
    """Built on first use so importing this module never needs credentials."""
    global _store
    if _store is None:
        _store = build_store()
    return _store


class _StoreProxy:
    """Keeps the module-level ``media.store`` name working.

    Call sites and tests already reference ``media.store``; routing it through
    the lazy builder avoids touching every one of them while still deferring
    construction until configuration is known.
    """

    def __getattr__(self, name: str):
        return getattr(get_store(), name)


store = _StoreProxy()
