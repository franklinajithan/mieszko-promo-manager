import io
import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from openpyxl import load_workbook
from PIL import Image
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from sqlalchemy import Column, Date, ForeignKey, Integer, Numeric, String, create_engine, func, or_, select
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
IMAGES = DATA / "images"
IMAGES.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA / 'promo.db'}")
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine)

class Base(DeclarativeBase):
    pass

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    barcode = Column(String(32), unique=True, index=True, nullable=False)
    code = Column(String(80), index=True)
    brand = Column(String(180))
    unit = Column(String(40))
    weight = Column(String(80))
    name = Column(String(400), nullable=False, index=True)
    image = Column(String(250))

class Promotion(Base):
    __tablename__ = "promotions"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    price = Column(Numeric(12, 2), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)

Base.metadata.create_all(engine)
app = FastAPI(title="Mieszko Promo Manager", version="0.1.0")

def db():
    with SessionLocal() as session:
        yield session

def barcode(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()

def output(product):
    return {"id": product.id, "barcode": product.barcode, "code": product.code, "name": product.name, "brand": product.brand, "unit": product.unit, "weight": product.weight, "image_url": f"/images/{product.image}" if product.image else None}

@app.get("/", response_class=HTMLResponse)
def home():
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/api/stats")
def stats(s: Session = Depends(db)):
    return {"products": s.scalar(select(func.count()).select_from(Product)), "promotions": s.scalar(select(func.count()).select_from(Promotion)), "missing_images": s.scalar(select(func.count()).select_from(Product).where(Product.image.is_(None)))}

@app.get("/api/products")
def list_products(q: str = "", page: int = 1, limit: int = 30, s: Session = Depends(db)):
    page = max(1, page)
    limit = max(1, min(limit, 100))
    query = select(Product)
    if q.strip():
        pattern = "%" + q.strip() + "%"
        query = query.where(or_(Product.barcode.ilike(pattern), Product.name.ilike(pattern), Product.code.ilike(pattern), Product.brand.ilike(pattern)))
    total = s.scalar(select(func.count()).select_from(query.subquery()))
    rows = s.scalars(query.order_by(Product.id).offset((page - 1) * limit).limit(limit))
    return {"total": total, "page": page, "items": [output(p) for p in rows]}

@app.post("/api/products")
def create_product(barcode_value: str = Form(..., alias="barcode"), name: str = Form(...), brand: str = Form(""), weight: str = Form(""), unit: str = Form("EACH"), code: str = Form(""), s: Session = Depends(db)):
    value = barcode(barcode_value)
    if not value or not name.strip():
        raise HTTPException(400, "Barcode and name are required")
    if s.scalar(select(Product.id).where(Product.barcode == value)):
        raise HTTPException(409, "Barcode already exists")
    p = Product(barcode=value, name=name.strip(), brand=brand, weight=weight, unit=unit, code=code)
    s.add(p)
    s.commit()
    s.refresh(p)
    return output(p)

@app.put("/api/products/{product_id}")
def update_product(product_id: int, barcode_value: str = Form(..., alias="barcode"), name: str = Form(...), brand: str = Form(""), weight: str = Form(""), unit: str = Form("EACH"), code: str = Form(""), s: Session = Depends(db)):
    p = s.get(Product, product_id)
    if not p:
        raise HTTPException(404, "Product not found")
    value = barcode(barcode_value)
    if not value or not name.strip():
        raise HTTPException(400, "Barcode and name are required")
    if s.scalar(select(Product.id).where(Product.barcode == value, Product.id != product_id)):
        raise HTTPException(409, "Barcode already exists")
    for key, val in {"barcode": value, "name": name.strip(), "brand": brand, "weight": weight, "unit": unit, "code": code}.items():
        setattr(p, key, val)
    s.commit()
    return output(p)

@app.post("/api/products/{product_id}/image")
async def upload_image(product_id: int, file: UploadFile = File(...), s: Session = Depends(db)):
    p = s.get(Product, product_id)
    if not p:
        raise HTTPException(404, "Product not found")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(400, "Unsupported image extension")
    content = await file.read(8 * 1024 * 1024 + 1)
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(413, "Image exceeds 8 MB")
    try:
        img = Image.open(io.BytesIO(content))
        img.verify()
    except Exception:
        raise HTTPException(400, "Invalid image")
    filename = uuid.uuid4().hex + ext
    (IMAGES / filename).write_bytes(content)
    p.image = filename
    s.commit()
    return output(p)

@app.get("/images/{filename}")
def image(filename: str):
    if filename != Path(filename).name:
        raise HTTPException(400, "Invalid filename")
    path = IMAGES / filename
    if not path.is_file():
        raise HTTPException(404, "Image not found")
    return FileResponse(path)

@app.post("/api/import")
async def import_excel(file: UploadFile = File(...), s: Session = Depends(db)):
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(400, "Expected .xlsx file")
    content = await file.read(30 * 1024 * 1024 + 1)
    if len(content) > 30 * 1024 * 1024:
        raise HTTPException(413, "Workbook exceeds 30 MB")
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:
        raise HTTPException(400, "Invalid XLSX workbook")
    sheet = workbook["Product List"] if "Product List" in workbook.sheetnames else workbook.active
    inserted = updated = skipped = 0
    seen = set()
    for row in sheet.iter_rows(values_only=True):
        if len(row) < 6:
            skipped += 1
            continue
        value = barcode(row[1])
        name = str(row[5] or "").strip()
        if not value.isdigit() or not name or value in seen:
            skipped += 1
            continue
        seen.add(value)
        code = str(row[0] or "").strip()
        if code.startswith("#") or code.startswith("="):
            code = ""
        values = {"code": code, "brand": str(row[2] or "").strip(), "unit": str(row[3] or "").strip(), "weight": str(row[4] or "").strip(), "name": name}
        p = s.scalar(select(Product).where(Product.barcode == value))
        if p:
            for key, val in values.items():
                setattr(p, key, val)
            updated += 1
        else:
            s.add(Product(barcode=value, **values))
            inserted += 1
        if (inserted + updated) % 1000 == 0:
            s.flush()
    s.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped, "sheet": sheet.title}

@app.post("/api/promotions")
def create_promotion(product_id: int = Form(...), price: Decimal = Form(...), start_date: date = Form(...), end_date: date = Form(...), s: Session = Depends(db)):
    if not s.get(Product, product_id):
        raise HTTPException(404, "Product not found")
    if price < 0 or end_date < start_date:
        raise HTTPException(400, "Invalid price or dates")
    pr = Promotion(product_id=product_id, price=price, start_date=start_date, end_date=end_date)
    s.add(pr)
    s.commit()
    s.refresh(pr)
    return {"id": pr.id}

@app.get("/api/promotions")
def list_promotions(s: Session = Depends(db)):
    rows = s.execute(select(Promotion, Product).join(Product, Promotion.product_id == Product.id).order_by(Promotion.id.desc()).limit(100))
    return [{"id": pr.id, "price": str(pr.price), "start_date": str(pr.start_date), "end_date": str(pr.end_date), "product": output(p)} for pr, p in rows]

def render_pdf(rows):
    stream = io.BytesIO()
    c = canvas.Canvas(stream, pagesize=A4)
    w, h = A4
    red = HexColor("#C63830")
    for pr, product in rows:
        c.setStrokeColor(red)
        c.setLineWidth(3)
        c.roundRect(18, 18, w - 36, h - 36, 9, fill=0, stroke=1)
        c.setFillColor(red)
        c.setFont("Helvetica-Bold", 18)
        c.drawString(35, h - 62, "POLSKIE SUPERMARKETY")
        c.setFont("Helvetica-Bold", 35)
        c.drawString(35, h - 104, "MIESZKO")
        c.roundRect(320, h - 115, 245, 78, 12, fill=1, stroke=0)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 17)
        c.drawCentredString(443, h - 67, "Promotion Period")
        c.setFont("Helvetica-Bold", 12)
        c.drawCentredString(443, h - 94, f"{pr.start_date:%d-%m-%Y} - {pr.end_date:%d-%m-%Y}")
        if product.image and (IMAGES / product.image).is_file():
            try:
                c.drawImage(ImageReader(str(IMAGES / product.image)), 42, 230, 290, 385, preserveAspectRatio=True, anchor="c", mask="auto")
            except Exception:
                pass
        c.setFillColor(HexColor("#FFE600"))
        c.setStrokeColor(red)
        c.setLineWidth(4)
        c.circle(454, 439, 104, stroke=1, fill=1)
        c.setFillColor(HexColor("#101010"))
        c.setFont("Helvetica-Bold", 28)
        c.drawString(375, 478, "GBP")
        c.setFont("Helvetica-Bold", 61)
        c.drawCentredString(456, 414, f"{pr.price:.2f}")
        c.setFillColor(red)
        c.roundRect(350, 355, 210, 43, 8, stroke=0, fill=1)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 20)
        c.drawCentredString(455, 368, (product.unit or "EACH")[:15])
        from reportlab.graphics.barcode import code128
        code128.Code128(product.barcode, barHeight=43, barWidth=0.9).drawOn(c, 34, 78)
        c.setFillColor(HexColor("#222222"))
        c.setFont("Helvetica", 10)
        c.drawString(34, 61, product.barcode)
        c.setFont("Helvetica-Bold", 14)
        c.drawRightString(w - 32, 88, (product.name or "")[:50])
        c.setFont("Helvetica", 11)
        c.drawRightString(w - 32, 61, f"{product.weight or ''} / {product.brand or ''}"[:62])
        c.showPage()
    c.save()
    stream.seek(0)
    return stream

@app.get("/api/promotions/pdf/bulk")
def bulk_pdf(ids: str, s: Session = Depends(db)):
    try:
        keys = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "Invalid IDs")
    if not keys or len(keys) > 200:
        raise HTTPException(400, "Choose 1 to 200 IDs")
    rows = s.execute(select(Promotion, Product).join(Product, Promotion.product_id == Product.id).where(Promotion.id.in_(keys)).order_by(Promotion.id)).all()
    return StreamingResponse(render_pdf(rows), media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="mieszko-promotions.pdf"'})

@app.get("/api/promotions/{promotion_id}/pdf")
def promotion_pdf(promotion_id: int, s: Session = Depends(db)):
    row = s.execute(select(Promotion, Product).join(Product, Promotion.product_id == Product.id).where(Promotion.id == promotion_id)).first()
    if not row:
        raise HTTPException(404, "Promotion not found")
    return StreamingResponse(render_pdf([row]), media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="promo-{promotion_id}.pdf"'})
