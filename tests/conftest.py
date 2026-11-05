import os
import tempfile
from pathlib import Path

TEST_ROOT = Path(tempfile.mkdtemp(prefix="promo-tests-"))
os.environ.setdefault("DATA_DIR", str(TEST_ROOT))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TEST_ROOT}/test.db")
os.environ["COOKIE_SECURE"] = "false"
os.environ["ALLOWED_ORIGIN"] = "http://testserver"

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.auth import hasher
from app.main import app
from app.models import Base, SessionLocal, User, engine

command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture(autouse=True)
def clean_db():
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(delete(table))
    with SessionLocal() as s:
        s.add(User(username="admin", password_hash=hasher.hash("test-only-secret-123"), role="admin"))
        s.add(User(username="staff", password_hash=hasher.hash("test-only-secret-123"), role="staff"))
        s.commit()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_client(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "test-only-secret-123"})
    assert r.status_code == 200
    client.headers["X-CSRF-Token"] = r.json()["csrf"]
    return client


@pytest.fixture
def staff_client(client):
    r = client.post("/api/auth/login", json={"username": "staff", "password": "test-only-secret-123"})
    assert r.status_code == 200
    client.headers["X-CSRF-Token"] = r.json()["csrf"]
    return client


@pytest.fixture
def product(admin_client):
    response = admin_client.post(
        "/api/products",
        json={
            "code": "M001",
            "barcode": "5900820005535",
            "name": "Ser żółty Edamski plastry",
            "brand": "Mlekpol",
            "weight": "150G",
            "unit": "EACH",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()
