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


def test_cached_lookup_formula_is_read_without_evaluation(admin_client):
    import zipfile
    from xml.etree import ElementTree as ET

    content = workbook(
        [("=VLOOKUP(B1,[2]Product!$A:$B,2,0)", "5900820005535", "Mlekpol", "EACH", "150G", "Cheese")]
    )
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(result, "w") as target:
        for entry in source.infolist():
            raw = source.read(entry.filename)
            if entry.filename == "xl/worksheets/sheet1.xml":
                tree = ET.fromstring(raw)
                ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                cell = tree.find('.//s:c[@r="A1"]', ns)
                cell.set("t", "str")
                cell.find("s:v", ns).text = "M001"
                raw = ET.tostring(tree)
            target.writestr(entry, raw)
    job = upload(admin_client, result.getvalue())
    assert job["result"]["valid"] == 1, job
    assert job["result"]["sample"][0]["code"] == "M001"


def test_cancel_committed_batch_and_retry_without_duplicates(admin_client, monkeypatch):
    from app import imports
    from app.models import Job, SessionLocal

    job = upload(
        admin_client,
        workbook([(f"M{i}", f"{10000000 + i}", "", "EACH", "", f"Product {i}") for i in range(501)]),
    )
    key = job["id"]
    admin_client.post(f"/api/jobs/{key}/commit")
    original = imports.progress

    def cancel_after_batch(job_id, count, total=0, result=None):
        if count == 250:
            with SessionLocal() as s:
                stored = s.get(Job, job_id)
                stored.cancel_requested = True
                s.commit()
        return original(job_id, count, total, result)

    monkeypatch.setattr(imports, "progress", cancel_after_batch)
    process_one()
    assert admin_client.get(f"/api/jobs/{key}").json()["status"] == "cancelled"
    assert admin_client.get("/api/products").json()["total"] == 250
    monkeypatch.setattr(imports, "progress", original)
    admin_client.post(f"/api/jobs/{key}/retry")
    process_one()
    finished = admin_client.get(f"/api/jobs/{key}").json()
    assert finished["status"] == "completed", finished
    assert finished["result"]["updated"] == 250
    assert finished["result"]["inserted"] == 251
    assert admin_client.get("/api/products").json()["total"] == 501


def test_workbook_xml_entities_are_disabled():
    from openpyxl.xml import DEFUSEDXML

    assert DEFUSEDXML is True
