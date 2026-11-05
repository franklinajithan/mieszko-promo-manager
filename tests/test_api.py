import io

import pytest
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import select

from app.models import Job, SessionLocal
from app.worker import process_one


def image_bytes():
    out = io.BytesIO()
    Image.new("RGB", (800, 600), "#e6c945").save(out, "PNG")
    return out.getvalue()


def campaign(c, product, price="1.99"):
    r = c.post(
        "/api/campaigns",
        json={
            "name": "Weekend offers",
            "start_date": "2026-11-05",
            "end_date": "2026-11-15",
            "status": "active",
            "items": [{"product_id": product["id"], "price": price}],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_auth_and_health(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    for route in (
        "products",
        "stats",
        "images",
        "jobs",
        "campaigns",
        "users",
        "history",
        "settings",
        "templates",
    ):
        assert client.get("/api/" + route).status_code == 401
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    assert "nosniff" == r.headers["x-content-type-options"]
    assert "connect-src 'self' blob:" in r.headers["content-security-policy"]


def test_csrf_logout_and_origin(admin_client):
    token = admin_client.headers.pop("X-CSRF-Token")
    assert admin_client.post("/api/auth/logout").status_code == 403
    admin_client.headers["X-CSRF-Token"] = token
    assert (
        admin_client.post("/api/auth/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    )
    assert admin_client.post("/api/auth/logout").status_code == 200
    assert admin_client.get("/api/auth/me").status_code == 401


def test_rate_limit(client):
    for _ in range(10):
        assert (
            client.post("/api/auth/login", json={"username": "missing", "password": "bad"}).status_code == 401
        )
    assert client.post("/api/auth/login", json={"username": "missing", "password": "bad"}).status_code == 429


def test_permissions(staff_client):
    assert staff_client.get("/api/users").status_code == 403
    assert staff_client.get("/api/history").status_code == 403
    assert staff_client.post("/api/imports", files={"file": ("test.xlsx", b"x")}).status_code == 403
    p = staff_client.post("/api/products", json={"barcode": "00001234", "name": "Milk"}).json()
    assert staff_client.post(f"/api/products/{p['id']}/archive").status_code == 403
    assert (
        staff_client.put(
            f"/api/products/{p['id']}", json={"barcode": "00001234", "name": "Milk", "active": False}
        ).status_code
        == 403
    )


def test_product_duplicate_search_archive(admin_client, product):
    data = {"barcode": product["barcode"], "name": "Duplicate"}
    assert admin_client.post("/api/products", json=data).status_code == 409
    assert (
        admin_client.post(
            "/api/products", json={"barcode": "12345678", "code": "M001", "name": "Duplicate code"}
        ).status_code
        == 409
    )
    p = admin_client.get("/api/products/" + str(product["id"])).json()
    allowed = {k: p[k] for k in ("code", "barcode", "name", "brand", "weight", "unit", "active")}
    allowed["barcodes"] = ["00001234"]
    assert admin_client.put("/api/products/" + str(p["id"]), json=allowed).status_code == 200
    assert admin_client.get("/api/products?q=00001234").json()["items"][0]["id"] == p["id"]
    assert admin_client.get("/api/products?q=Edamski").json()["total"] == 1
    assert admin_client.get("/api/products?q=%25").json()["total"] == 0
    assert admin_client.get("/api/products?page=0").status_code == 422
    assert admin_client.get("/api/products?sort=drop").status_code == 422
    assert admin_client.post(f"/api/products/{p['id']}/archive").status_code == 200
    assert admin_client.get("/api/products").json()["total"] == 0
    assert admin_client.get("/api/products?active=archived").json()["total"] == 1
    assert admin_client.post(f"/api/products/{p['id']}/restore").status_code == 200


def test_image_validation_dedupe_link(admin_client, product):
    url = "/api/images"
    r = admin_client.post(url, files=[("files", (product["barcode"] + ".png", image_bytes(), "image/png"))])
    assert r.status_code == 200, r.text
    asset = r.json()["items"][0]
    assert asset["product_id"] == product["id"]
    second = admin_client.post(url, files=[("files", ("again.png", image_bytes(), "image/png"))]).json()[
        "items"
    ][0]
    assert second["id"] == asset["id"]
    assert admin_client.get("/api/images").json()["total"] == 1
    assert admin_client.get(f"/api/assets/{asset['id']}/thumbnail").headers["content-type"] == "image/webp"
    assert admin_client.get(f"/api/assets/{asset['id']}/original").content == image_bytes()
    assert admin_client.get("/api/products?missing=true").json()["total"] == 0
    bad = admin_client.post(
        url, files=[("files", ("attack.png", b"<script>alert(1)</script>", "image/png"))]
    ).json()
    assert bad["items"][0]["error"].startswith("Invalid image")
    huge = admin_client.post(url, files=[("files", ("huge.jpg", b"x" * (8 * 1024 * 1024 + 1)))]).json()
    assert "8 MB" in huge["items"][0]["error"]
    assert admin_client.delete(f"/api/products/{product['id']}/images/{asset['id']}").status_code == 200
    assert admin_client.get("/api/products?missing=true").json()["total"] == 1
    assert admin_client.get(f"/api/assets/{asset['id']}/original").status_code == 200


@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
def test_campaign_pdf_snapshot_and_reprint(admin_client, product, orientation):
    c = campaign(admin_client, product)
    assert c["items"][0]["price"] == "1.99"
    body = {"promotion_ids": [c["items"][0]["id"]], "template": {"orientation": orientation}}
    response = admin_client.post("/api/preview", json=body)
    assert response.status_code == 200, response.text
    reader = PdfReader(io.BytesIO(response.content))
    assert len(reader.pages) == 1
    page = reader.pages[0]
    assert (float(page.mediabox.width) < float(page.mediabox.height)) == (orientation == "portrait")
    assert "1.99" in page.extract_text()
    assert "Ser żółty Edamski plastry" in page.extract_text()
    job = admin_client.post("/api/prints", json=body).json()
    assert process_one()
    result = admin_client.get("/api/jobs/" + str(job["id"])).json()
    assert result["status"] == "completed", result
    original = admin_client.get(f"/api/jobs/{job['id']}/download").content
    changed = {"name": "Renamed after printing", "barcode": product["barcode"], "code": "M001"}
    admin_client.put("/api/products/" + str(product["id"]), json=changed)
    admin_client.post("/api/campaigns/" + str(c["id"]) + "/archive")
    assert admin_client.get(f"/api/jobs/{job['id']}/download").content == original
    assert admin_client.post("/api/campaigns/" + str(c["id"]) + "/duplicate").status_code == 201
    assert len(admin_client.get("/api/history").json()["items"]) > 0


def test_invalid_campaigns_and_prints(admin_client, product):
    for price in ("-1", "0", "1.999", "NaN", "Infinity"):
        assert (
            admin_client.post(
                "/api/campaigns",
                json={
                    "name": "Invalid",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-11",
                    "items": [{"product_id": product["id"], "price": price}],
                },
            ).status_code
            == 422
        )
    assert admin_client.post("/api/prints", json={"promotion_ids": [999]}).status_code == 404
    assert admin_client.post("/api/preview", json={"promotion_ids": [1, 2]}).status_code == 422


def test_user_management_and_session_revocation(admin_client):
    r = admin_client.post(
        "/api/users", json={"username": "new", "password": "test-password-123", "role": "staff"}
    )
    assert r.status_code == 201
    assert (
        admin_client.post(
            "/api/users", json={"username": "new", "password": "test-password-123", "role": "staff"}
        ).status_code
        == 409
    )
    assert (
        admin_client.post(
            "/api/users", json={"username": "short", "password": "short", "role": "staff"}
        ).status_code
        == 422
    )
    me = admin_client.get("/api/auth/me").json()
    assert (
        admin_client.put("/api/users/" + str(me["id"]), json={"role": "staff", "active": False}).status_code
        == 409
    )


def test_cancel_queued_and_retry(admin_client, product):
    c = campaign(admin_client, product)
    j = admin_client.post("/api/prints", json={"promotion_ids": [c["items"][0]["id"]]}).json()
    assert admin_client.post(f"/api/jobs/{j['id']}/cancel").json()["status"] == "cancelled"
    assert not process_one()
    assert admin_client.post(f"/api/jobs/{j['id']}/retry").json()["status"] == "queued"
    assert process_one()
    assert admin_client.get(f"/api/jobs/{j['id']}").json()["status"] == "completed"
    with SessionLocal() as s:
        stored = s.scalar(select(Job).where(Job.id == j["id"]))
        assert stored.result["pages"] == 1


def test_login_throttle_does_not_lock_unrelated_staff(client):
    for _ in range(10):
        client.post("/api/auth/login", json={"username": "missing", "password": "bad"})
    assert (
        client.post(
            "/api/auth/login", json={"username": "admin", "password": "test-only-secret-123"}
        ).status_code
        == 200
    )
