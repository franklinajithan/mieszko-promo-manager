import csv
import json
import zipfile

from fastapi import HTTPException
from openpyxl import load_workbook
from pydantic import ValidationError
from sqlalchemy import false, or_, select
from sqlalchemy.exc import IntegrityError

from app.catalog import save_product
from app.config import settings
from app.models import Barcode, Job, Product, SessionLocal
from app.schemas import ProductInput, normal_barcode, valid_gtin

ALIASES = {
    "code": {"code", "internalcode", "internalproductcode", "productcode", "itemcode", "sku"},
    "barcode": {"barcode", "ean", "ean13", "gtin", "eanbarcode"},
    "name": {"name", "description", "productdescription", "productname"},
    "brand": {"brand", "manufacturer"},
    "weight": {"weight", "size", "netweight"},
    "unit": {"unit", "uom", "sellingunit"},
}


def clean(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value or "").strip()


def mapped(row):
    tokens = ["".join(c for c in clean(v).lower() if c.isalnum()) for v in row]
    return {
        field: i for field, aliases in ALIASES.items() for i, token in enumerate(tokens) if token in aliases
    }


def workbook_rows(path):
    with zipfile.ZipFile(path) as archive:
        if len(archive.infolist()) > 2000 or sum(x.file_size for x in archive.infolist()) > 300 * 1024 * 1024:
            raise ValueError("Workbook expanded size exceeds 300 MB or 2,000 parts")
    workbook = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    cached = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    try:
        sheet = workbook["Product List"] if "Product List" in workbook.sheetnames else workbook.active
        if sheet is None:
            raise ValueError("Workbook has no worksheet")
        cached_sheet = cached[sheet.title]

        def resolved_rows():
            for raw, values in zip(
                sheet.iter_rows(values_only=True), cached_sheet.iter_rows(values_only=True), strict=True
            ):
                # Read stored Excel results, never evaluate formulas or external links.
                # Uncached formulas remain text and fail identity validation.
                yield tuple(
                    cached_value
                    if isinstance(value, str) and value.startswith("=") and cached_value is not None
                    else value
                    for value, cached_value in zip(raw, values, strict=True)
                )

        rows = resolved_rows()
        first = next(rows, ())
        columns = mapped(first)
        header = {"barcode", "name"}.issubset(columns)
        if not header:
            if len(first) < 6:
                raise ValueError(
                    "Cannot detect columns. Supply code, barcode, brand, unit, weight, description headers"
                )
            columns = dict(zip(("code", "barcode", "brand", "unit", "weight", "name"), range(6)))
            yield 1, first, columns
        for index, row in enumerate(rows, start=2):
            if index > 250001:
                raise ValueError("Workbook exceeds 250,000 rows")
            if any(v is not None for v in row):
                yield index, row, columns
    finally:
        workbook.close()
        cached.close()


def get_job(job_id):
    with SessionLocal() as s:
        return s.get(Job, job_id)


def progress(job_id, count, total=0, result=None):
    (settings.data_dir / "worker-heartbeat").touch()
    with SessionLocal() as s:
        job = s.get(Job, job_id)
        assert job is not None
        job.progress = count
        if total:
            job.total = total
        if result is not None:
            job.result = result
        s.commit()
        return job.cancel_requested


def write_error(writer, row, code, barcode, error):
    # Neutralise CSV spreadsheet formulas, including user-provided codes.
    def safe(v):
        value = str(v)
        return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value

    writer.writerow([safe(v) for v in (row, code, barcode, error)])


def preview_import(job):
    source = settings.data_dir / "uploads" / job.payload["source"]
    target = settings.data_dir / "uploads" / f"{job.id}.jsonl"
    errors = settings.data_dir / "uploads" / f"{job.id}-errors.csv"
    counts = dict(valid=0, invalid=0, warnings=0, new=0, updates=0)
    sample: list[dict] = []
    with SessionLocal() as s:
        barcodes = dict(s.execute(select(Barcode.value, Barcode.product_id)).all())
        codes = dict(s.execute(select(Product.code, Product.id).where(Product.code.is_not(None))).all())
    seen_barcodes, seen_codes = set(), set()
    count = 0
    with target.open("w") as out, errors.open("w", newline="") as err:
        writer = csv.writer(err)
        writer.writerow(["Row", "Code", "Barcode", "Error or warning"])
        for number, row, columns in workbook_rows(source):
            count += 1
            raw = {field: clean(row[i]) if i < len(row) else "" for field, i in columns.items()}
            code, barcode = raw.get("code", ""), raw.get("barcode", "")
            try:
                if code in ("#N/A", "#VALUE!", "#REF!"):
                    raw["code"] = None
                barcode = normal_barcode(row[columns["barcode"]])
                raw["barcode"] = barcode
                data = ProductInput.model_validate(raw)
                matches = {x for x in (barcodes.get(barcode), codes.get(data.code)) if x}
                if len(matches) > 1:
                    raise ValueError(
                        "Barcode and code match different existing products; resolve identity conflict"
                    )
                if barcode in seen_barcodes or (data.code and data.code in seen_codes):
                    raise ValueError("Duplicate barcode or code in workbook")
                seen_barcodes.add(barcode)
                if data.code:
                    seen_codes.add(data.code)
                warnings = []
                if not data.code:
                    warnings.append("Missing internal code; barcode used as identity")
                if len(barcode) in (8, 12, 13, 14) and not valid_gtin(barcode):
                    warnings.append("Invalid GTIN checksum; will print as Code128")
                if warnings:
                    counts["warnings"] += 1
                    write_error(writer, number, code, barcode, "; ".join(warnings))
                record = data.model_dump(mode="json") | {"row_number": number}
                out.write(json.dumps(record) + "\n")
                counts["valid"] += 1
                counts["updates" if matches else "new"] += 1
                if len(sample) < 20:
                    sample.append(
                        record | {"action": "update" if matches else "insert", "warnings": warnings}
                    )
            except (ValueError, ValidationError) as exc:
                counts["invalid"] += 1
                write_error(writer, number, code, barcode, str(exc))
            if count % 250 == 0 and progress(job.id, count):
                return "cancelled", counts
    progress(job.id, count, count)
    return "preview", counts | {
        "sample": sample,
        "mapping": columns,
        "normalised": target.name,
        "report": errors.name,
    }


def commit_import(job):
    path = settings.data_dir / "uploads" / job.result["normalised"]
    inserted = updated = failed = processed = 0
    errors = settings.data_dir / "uploads" / f"{job.id}-commit-errors.csv"
    with path.open() as source, errors.open("w", newline="") as err:
        writer = csv.writer(err)
        writer.writerow(["Row", "Code", "Barcode", "Error"])
        with SessionLocal() as s:
            for line in source:
                row = json.loads(line)
                number = row.pop("row_number")
                data = ProductInput(**row)
                try:
                    with s.begin_nested():
                        ids = set(
                            s.scalars(
                                select(Product.id).where(
                                    or_(
                                        Product.id.in_(
                                            select(Barcode.product_id).where(Barcode.value == data.barcode)
                                        ),
                                        Product.code == data.code if data.code else false(),
                                    )
                                )
                            )
                        )
                        if len(ids) > 1:
                            raise ValueError("Identity conflict since preview; row was not changed")
                        p = s.get(Product, next(iter(ids))) if ids else None
                        if p:
                            # Blank incoming optional values never erase existing business information.
                            data = data.model_copy(
                                update={
                                    key: getattr(p, key)
                                    for key in ("code", "brand", "weight", "unit")
                                    if not getattr(data, key)
                                }
                            )
                            data = data.model_copy(update={"active": p.active})
                        save_product(s, data, p)
                    if p:
                        updated += 1
                    else:
                        inserted += 1
                except (HTTPException, ValueError, IntegrityError) as exc:
                    # Row savepoints isolate conflicts while preserving valid rows.
                    failed += 1
                    write_error(
                        writer,
                        number,
                        data.code,
                        data.barcode,
                        (
                            str(exc.detail)
                            if isinstance(exc, HTTPException)
                            else str(exc)
                            if isinstance(exc, ValueError)
                            else "Identity conflict; row was not changed"
                        ),
                    )
                processed += 1
                if processed % 250 == 0:
                    s.commit()
                    if progress(
                        job.id,
                        processed,
                        result=job.result
                        | {"inserted": inserted, "updated": updated, "failed": failed, "report": errors.name},
                    ):
                        return "cancelled", job.result | {
                            "inserted": inserted,
                            "updated": updated,
                            "failed": failed,
                            "report": errors.name,
                        }
            s.commit()
    return "partial" if failed else "completed", job.result | {
        "inserted": inserted,
        "updated": updated,
        "failed": failed,
        "report": errors.name,
        "processed": processed,
    }
