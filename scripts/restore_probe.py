"""Disposable Docker CI data-loss probe; never run against business data."""

import io
import os
import sys

from PIL import Image
from sqlalchemy import select

from app.catalog import attach_image, save_product
from app.config import settings
from app.images import store_image
from app.models import Product, SessionLocal
from app.schemas import ProductInput

if os.getenv("PROMO_TEST_MODE") != "1":
    raise SystemExit("Disposable test mode required")
mode = sys.argv[1]
marker = settings.data_dir / "prints" / "restore-probe.txt"
with SessionLocal() as s:
    p = s.scalar(select(Product).where(Product.code == "CI-RESTORE-PROBE"))
    if mode == "create":
        if p:
            raise SystemExit("Expected empty test database")
        p = save_product(
            s, ProductInput(code="CI-RESTORE-PROBE", barcode="5900820005535", name="Restore probe")
        )
        content = io.BytesIO()
        Image.new("RGB", (64, 64), "#cc3322").save(content, "PNG")
        a = store_image(s, content.getvalue(), "5900820005535.png")
        attach_image(s, p, a)
        marker.write_text("matching-files-and-database")
        s.commit()
    elif mode == "mutate":
        assert p
        p.name = "Must be rolled back by restore"
        marker.unlink()
        s.commit()
    elif mode == "verify":
        from app.models import Asset

        assert p and p.name == "Restore probe" and p.primary_image_id
        asset = s.get(Asset, p.primary_image_id)
        assert (settings.data_dir / "images" / asset.filename).is_file()
        assert (settings.data_dir / "thumbs" / (asset.digest + ".webp")).is_file()
        assert (settings.data_dir / "print_images" / (asset.digest + ".jpg")).is_file()
        assert marker.read_text() == "matching-files-and-database"
    else:
        raise SystemExit("Unknown probe mode")
print("Restore probe:", mode, "passed")
