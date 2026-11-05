import os
import sqlite3
import subprocess
import sys

import pytest


def migrate(path):
    env = os.environ | {"DATABASE_URL": f"sqlite:///{path}", "DATA_DIR": str(path.parent / "data")}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], env=env, capture_output=True, text=True
    )


def test_fresh_migration_repeatable(tmp_path):
    path = tmp_path / "fresh.db"
    assert migrate(path).returncode == 0
    assert migrate(path).returncode == 0
    with sqlite3.connect(path) as db:
        assert db.execute("select version_num from alembic_version").fetchone()[0] == "001_foundation"


def legacy(path, duplicate=False):
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE products(id INTEGER PRIMARY KEY, barcode VARCHAR(32) UNIQUE NOT NULL, code VARCHAR(80), name VARCHAR(400), brand VARCHAR(180), weight VARCHAR(80), unit VARCHAR(40), image VARCHAR(250)); CREATE TABLE promotions(id INTEGER PRIMARY KEY, product_id INTEGER, price NUMERIC(12,2), start_date DATE, end_date DATE);"
        )
        db.execute(
            "INSERT INTO products VALUES(1,'5900820005535','M001','Cheese','Mlekpol','150G','EACH','old.jpg')"
        )
        db.execute("INSERT INTO promotions VALUES(1,1,1.99,'2026-11-05','2026-11-15')")
        if duplicate:
            db.execute("INSERT INTO products VALUES(2,'12345678','M001','Other','','','','')")


def test_legacy_upgrade_preserves_records(tmp_path):
    path = tmp_path / "legacy.db"
    legacy(path)
    r = migrate(path)
    assert r.returncode == 0, r.stderr
    with sqlite3.connect(path) as db:
        assert db.execute("select image from products").fetchone()[0] == "old.jpg"
        assert db.execute("select value from barcodes").fetchone()[0] == "5900820005535"
        assert db.execute("select campaign_id from promotions").fetchone()[0] is not None
        assert db.execute("select count(*) from campaigns").fetchone()[0] == 1


def test_duplicate_legacy_code_stops_upgrade(tmp_path):
    path = tmp_path / "duplicates.db"
    legacy(path, True)
    r = migrate(path)
    assert r.returncode != 0
    assert "Duplicate legacy product codes" in r.stderr
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from products").fetchone()[0] == 2


@pytest.mark.parametrize(
    "code,valid", [("5900820005535", True), ("5900820005536", False), ("00001234", False), ("abc", False)]
)
def test_gtin_validation(code, valid):
    from app.schemas import valid_gtin

    assert valid_gtin(code) is valid
