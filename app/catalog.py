from fastapi import HTTPException
from sqlalchemy import false, func, or_, select

from app.models import Asset, Barcode, Product, ProductImage
from app.schemas import ProductInput, valid_gtin


def product_output(p, barcodes=None):
    return {
        k: getattr(p, k)
        for k in (
            "id",
            "code",
            "barcode",
            "name",
            "brand",
            "weight",
            "unit",
            "active",
            "created_at",
            "updated_at",
        )
    } | {
        "barcodes": barcodes or [p.barcode],
        "valid_gtin": valid_gtin(p.barcode),
        "primary_image_id": p.primary_image_id,
        "image_url": f"/api/assets/{p.primary_image_id}/thumbnail"
        if p.primary_image_id
        else (f"/api/legacy-images/{p.image}" if p.image else None),
    }


def save_product(s, data: ProductInput, p=None):
    values = set([data.barcode, *data.barcodes])
    other = s.scalar(
        select(Barcode.product_id).where(Barcode.value.in_(values), Barcode.product_id != (p.id if p else -1))
    )
    # Primary barcode check also protects upgrades of the prototype schema.
    primary = s.scalar(
        select(Product.id).where(
            or_(Product.barcode.in_(values), Product.code == data.code if data.code else false()),
            Product.id != (p.id if p else -1),
        )
    )
    if other or primary:
        raise HTTPException(409, "Barcode or internal code belongs to another product")
    if not p:
        p = Product()
        s.add(p)
    for key, value in data.model_dump(exclude={"barcodes"}).items():
        setattr(p, key, value)
    s.flush()
    # Preserve known alternate barcodes. Reassignment requires an explicit future reconciliation.
    existing = set(s.scalars(select(Barcode.value).where(Barcode.product_id == p.id)))
    for value in values - existing:
        s.add(Barcode(value=value, product_id=p.id))
    s.flush()
    return p


def product_query(q="", active="active", missing=False, brand=""):
    query = select(Product)
    if active != "all":
        query = query.where(Product.active == (active == "active"))
    if missing:
        query = query.where(Product.primary_image_id.is_(None), Product.image.is_(None))
    if brand:
        query = query.where(Product.brand == brand)
    if q.strip():
        value = q.strip()
        if value.isdigit():
            query = query.where(
                or_(
                    Product.id.in_(select(Barcode.product_id).where(Barcode.value == value)),
                    Product.code == value,
                )
            )
        else:
            escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.where(
                or_(
                    Product.name.ilike("%" + escaped + "%", escape="\\"),
                    Product.brand.ilike("%" + escaped + "%", escape="\\"),
                    Product.code == value,
                )
            )
    return query


def attach_image(s, p, asset):
    if not s.get(ProductImage, (p.id, asset.id)):
        s.add(ProductImage(product_id=p.id, asset_id=asset.id))
    p.primary_image_id = asset.id
    p.image = None


def product_images(s, p):
    return [
        {
            "id": a.id,
            "name": a.original_name,
            "url": f"/api/assets/{a.id}/original",
            "thumbnail": f"/api/assets/{a.id}/thumbnail",
            "primary": p.primary_image_id == a.id,
        }
        for a in s.scalars(select(Asset).join(ProductImage).where(ProductImage.product_id == p.id))
    ]


def count_query(s, query):
    return s.scalar(select(func.count()).select_from(query.subquery()))
