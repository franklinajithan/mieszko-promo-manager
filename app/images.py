import hashlib
import io
import warnings
from pathlib import Path

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select

from app.config import settings
from app.models import Asset

Image.MAX_IMAGE_PIXELS = 24_000_000


def atomic_write(path, content):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(content)
    temp.replace(path)


def store_image(s, content: bytes, filename: str):
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(413, "Image exceeds 8 MB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(content))
            if img.format not in ("JPEG", "PNG", "WEBP"):
                raise ValueError("Unsupported image content")
            ext = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[img.format]
            img.verify()
            img = ImageOps.exif_transpose(Image.open(io.BytesIO(content)))
            img.load()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise HTTPException(400, "Invalid image; use JPG, PNG or WebP up to 24 megapixels") from None
    digest = hashlib.sha256(content).hexdigest()
    asset = s.scalar(select(Asset).where(Asset.digest == digest))
    if asset:
        return asset
    stored = digest + ext
    atomic_write(settings.data_dir / "images" / stored, content)
    thumb = img.convert("RGBA") if "A" in img.getbands() else img.convert("RGB")
    thumb.thumbnail((480, 480))
    stream = io.BytesIO()
    thumb.save(stream, "WEBP", quality=85)
    atomic_write(settings.data_dir / "thumbs" / (digest + ".webp"), stream.getvalue())
    asset = Asset(
        digest=digest,
        filename=stored,
        original_name=Path(filename).name[:250],
        width=img.width,
        height=img.height,
    )
    s.add(asset)
    s.flush()
    return asset
