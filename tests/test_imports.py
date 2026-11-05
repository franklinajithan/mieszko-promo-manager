import io

from openpyxl import Workbook

from app.worker import process_one


def workbook(rows, header=False):
    book = Workbook()
    sheet = book.active
    sheet.title = "Product List"
    if header:
        sheet.append(["Internal product code", "EAN", "Brand", "Unit", "Weight", "Product description"])
    for row in rows:
        sheet.append(row)
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def upload(c, content):
    r = c.post("/api/imports", files={"file": ("products.xlsx", content)})
    assert r.status_code == 202, r.text
    j = r.json()
    assert process_one()
    return c.get("/api/jobs/" + str(j["id"])).json()


def test_headerless_reference_preview_retry_idempotent(admin_client):
    rows = [
        ("M1", 5900820005535, "Mlekpol", "EACH", "150G", "Cheese"),
        ("#N/A", "00001234", "", "EACH", "", "Milk"),
        ("M1", "12345678", "", "", "", "Duplicate code"),
        ("M4", "not-a-barcode", "", "", "", "Bad"),
        ("M5", "12341234", "", "", "", ""),
    ]
    content = workbook(rows)
    j = upload(admin_client, content)
    assert j["status"] == "preview", j
    assert j["result"]["valid"] == 2
    assert j["result"]["invalid"] == 3
    assert admin_client.get("/api/products").json()["total"] == 0
    assert admin_client.post(f"/api/jobs/{j['id']}/commit").status_code == 200
    assert admin_client.post(f"/api/jobs/{j['id']}/commit").status_code == 409
    assert process_one()
    finished = admin_client.get("/api/jobs/" + str(j["id"])).json()
    assert finished["result"]["inserted"] == 2, finished
    assert admin_client.get("/api/products?q=00001234").json()["total"] == 1
    repeat = upload(admin_client, content)
    assert repeat["result"]["updates"] == 2
    admin_client.post(f"/api/jobs/{repeat['id']}/commit")
    process_one()
    assert admin_client.get("/api/products").json()["total"] == 2


def test_header_mapping_and_preserve_blank(admin_client, product):
    j = upload(
        admin_client, workbook([("M001", product["barcode"], "", "", "", "Updated description")], header=True)
    )
    assert j["status"] == "preview"
    admin_client.post(f"/api/jobs/{j['id']}/commit")
    process_one()
    p = admin_client.get("/api/products/" + str(product["id"])).json()
    assert p["name"] == "Updated description"
    assert p["brand"] == "Mlekpol"
    assert p["weight"] == "150G"


def test_identity_conflict_and_invalid_zip(admin_client, product):
    other = admin_client.post(
        "/api/products", json={"code": "OTHER", "barcode": "12345678", "name": "Other"}
    ).json()
    j = upload(admin_client, workbook([("OTHER", product["barcode"], "", "", "", "Conflict")]))
    assert j["result"]["invalid"] == 1
    assert j["result"]["valid"] == 0
    assert "different existing products" in admin_client.get(f"/api/jobs/{j['id']}/download").text
    broken = upload(admin_client, b"not zip")
    assert broken["status"] == "failed"
    assert other["id"] != product["id"]


def test_formula_cells_rejected(admin_client):
    j = upload(admin_client, workbook([('=HYPERLINK("http://evil")', "12345678", "", "", "", "Test")]))
    assert j["result"]["invalid"] == 1
    report = admin_client.get(f"/api/jobs/{j['id']}/download").text
    assert "'=HYPERLINK" in report
