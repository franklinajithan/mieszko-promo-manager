"""Preserving upgrade from the starter schema and new installation."""

from alembic import op
from sqlalchemy import inspect, text

from migrations.schema_001 import Base

revision = "001_foundation"
down_revision = None


def upgrade():
    bind = op.get_bind()
    old = inspect(bind).has_table("products")
    if old:
        # No silent merging or deletion of legacy business records.
        duplicates = bind.execute(
            text(
                "SELECT code FROM products WHERE code IS NOT NULL AND code <> '' GROUP BY code HAVING COUNT(*) > 1"
            )
        ).fetchall()
        if duplicates:
            raise RuntimeError(
                "Duplicate legacy product codes: review and resolve before upgrade; no records were deleted"
            )
        bind.execute(text("UPDATE products SET code = NULL WHERE code = ''"))
        timestamp_default = "CURRENT_TIMESTAMP"
        if bind.dialect.name == "sqlite":
            from datetime import UTC, datetime

            timestamp_default = "'" + datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S") + "'"
        for sql in [
            "ALTER TABLE products ADD COLUMN primary_image_id INTEGER REFERENCES assets(id)",
            "ALTER TABLE products ADD COLUMN active BOOLEAN NOT NULL DEFAULT TRUE",
            f"ALTER TABLE products ADD COLUMN created_at TIMESTAMP NOT NULL DEFAULT {timestamp_default}",
            f"ALTER TABLE products ADD COLUMN updated_at TIMESTAMP NOT NULL DEFAULT {timestamp_default}",
        ]:
            # Assets must exist before adding the reference.
            if "primary_image_id" in sql:
                Base.metadata.tables["assets"].create(bind, checkfirst=True)
            bind.execute(text(sql))
        bind.execute(text("CREATE UNIQUE INDEX ux_products_code ON products(code)"))
        bind.execute(text("ALTER TABLE promotions ADD COLUMN campaign_id INTEGER"))
        bind.execute(text("ALTER TABLE promotions ADD COLUMN previous_price NUMERIC(12,2)"))
        bind.execute(text("ALTER TABLE promotions ADD COLUMN image_id INTEGER"))
    Base.metadata.create_all(bind)
    if old:
        bind.execute(text("INSERT INTO barcodes(value, product_id) SELECT barcode, id FROM products"))
        # Legacy promotions remain visible under one campaign per date period.
        rows = bind.execute(text("SELECT DISTINCT start_date, end_date FROM promotions")).fetchall()
        for start, end in rows:
            result = bind.execute(
                text(
                    "INSERT INTO campaigns(name,start_date,end_date,status,created_at) VALUES ('Imported promotion',:s,:e,'draft',CURRENT_TIMESTAMP) RETURNING id"
                ),
                {"s": start, "e": end},
            )
            cid = result.scalar_one()
            bind.execute(
                text("UPDATE promotions SET campaign_id=:c WHERE start_date=:s AND end_date=:e"),
                {"c": cid, "s": start, "e": end},
            )
        if bind.dialect.name == "postgresql":
            bind.execute(
                text(
                    "ALTER TABLE promotions ADD CONSTRAINT fk_campaign FOREIGN KEY(campaign_id) REFERENCES campaigns(id)"
                )
            )
            bind.execute(
                text(
                    "ALTER TABLE promotions ADD CONSTRAINT fk_image FOREIGN KEY(image_id) REFERENCES assets(id)"
                )
            )
    if bind.dialect.name == "postgresql":
        bind.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        for col in ("name", "brand"):
            bind.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_products_{col}_trgm ON products USING gin ({col} gin_trgm_ops)"
                )
            )
        bind.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_products_fts ON products USING gin (to_tsvector('simple', name))"
            )
        )
        bind.execute(text("CREATE INDEX IF NOT EXISTS ix_products_name_id ON products(name,id)"))

    if bind.dialect.name == "postgresql":
        if bind.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='promo_app')")):
            bind.execute(
                text("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO promo_app")
            )
            bind.execute(text("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO promo_app"))


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled. Restore the verified pre-upgrade backup instead.")
